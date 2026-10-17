# -*- coding: utf-8 -*-
"""
SCFI / BDI Shipping Index Static Dashboard
===========================================

로컬 실행:
    python ShippingIndex/shipping_index.py --local

GitHub Actions 실행:
    python ShippingIndex/shipping_index.py --cron

필수 환경변수:
    KSG_ID
    KSG_PW

생성 파일:
    - index_history.db
    - shipping_index_data.json
    - index.html
    - index_crawl.log

권장 패키지 설치:
    pip install pandas selenium webdriver-manager
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    NoAlertPresentException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

try:
    from webdriver_manager.chrome import ChromeDriverManager

    HAS_WEBDRIVER_MANAGER = True
except Exception:
    HAS_WEBDRIVER_MANAGER = False

# =========================================================
# 1. Configuration
# =========================================================
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "index_history.db"
JSON_PATH = BASE_DIR / "shipping_index_data.json"
HTML_PATH = BASE_DIR / "index.html"
LOG_PATH = BASE_DIR / "index_crawl.log"

BASE_URL = "https://www.ksg.co.kr/"

SUPPORTED_INDEXES = ["SCFI", "BDI"]
DEFAULT_INDEX_NAME = "SCFI"

# 최근 데이터 정정 가능성에 대비하여 최신 DB 날짜보다
# 14일 이전부터 다시 수집 후 UPSERT한다.
OVERLAP_DAYS = 14

BDI_REGION_PRIORITY = [
    "BDI",
    "BCI",
    "BPI",
    "BSI",
    "BHSI",
    "BDTI",
    "BCTI",
]

BDI_LABEL_MAP = {
    "BDI": "BDI (Baltic Exchange Dry Index)",
    "BCI": "BCI (Baltic Capesize Index)",
    "BPI": "BPI (Baltic Panamax Index)",
    "BSI": "BSI (Baltic Supramax Index)",
    "BHSI": "BHSI (Baltic Handysize Index)",
    "BDTI": "BDTI (Baltic Dirty Tanker Index)",
    "BCTI": "BCTI (Baltic Clean Tanker Index)",
}

INVALID_REGION_NAMES = {
    "",
    "일자",
    "날짜",
    "날 자",
    "DATE",
    "Date",
    "date",
    "YEAR",
    "Year",
    "year",
    "INDEX_NAME",
    "CRAWL_PAGE",
}

crawl_lock = Lock()

# =========================================================
# 2. Logging
# =========================================================
logger = logging.getLogger("shipping_index")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s"
)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)

def activate_file_logging():
    """로컬 및 GitHub Actions 실행 로그를 파일에도 기록한다."""
    if any(
        isinstance(handler, logging.FileHandler)
        for handler in logger.handlers
    ):
        return

    try:
        file_handler = logging.FileHandler(
            LOG_PATH,
            mode="a",
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass

def log(message):
    logger.info(message)

# =========================================================
# 3. Common Utilities
# =========================================================
def normalize_index_name(index_name):
    index_name = str(
        index_name or DEFAULT_INDEX_NAME
    ).strip().upper()

    return (
        index_name
        if index_name in SUPPORTED_INDEXES
        else DEFAULT_INDEX_NAME
    )

def normalize_palette_name(name):
    allowed = ["Auto", "Grey", "Red", "Blue"]
    name = str(name or "Auto").strip()

    return name if name in allowed else "Auto"

def normalize_region_name(value):
    """헤더/항목명 공백 및 개행을 정리한다."""
    return " ".join(str(value or "").strip().split())

def is_valid_region_for_index(region, index_name):
    """
    테이블의 날짜 열 및 불필요한 컬럼이 지수/항로로 저장되는 것을 방지한다.
    """
    index_name = normalize_index_name(index_name)
    region = normalize_region_name(region)

    if not region:
        return False

    if region in INVALID_REGION_NAMES:
        return False

    if index_name == "BDI":
        return region in BDI_REGION_PRIORITY

    return True

def display_region_label(region, index_name):
    region = normalize_region_name(region)
    index_name = normalize_index_name(index_name)

    if index_name == "BDI":
        return BDI_LABEL_MAP.get(region, region)

    return region

def sort_regions_for_display(regions, index_name):
    index_name = normalize_index_name(index_name)

    cleaned = sorted(
        {
            normalize_region_name(region)
            for region in regions
            if is_valid_region_for_index(region, index_name)
        }
    )

    if index_name == "BDI":
        priority = {
            region: position
            for position, region in enumerate(
                BDI_REGION_PRIORITY
            )
        }

        return sorted(
            cleaned,
            key=lambda region: (
                priority.get(region, 999),
                region,
            ),
        )

    return cleaned

def parse_date(value):
    """KSG 테이블의 다양한 날짜 형식을 datetime.date로 정리한다."""
    text = str(value or "").strip()

    if not text:
        return None

    text = re.sub(r"\s+", "", text)

    formats = [
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%Y/%m/%d",
        "%Y%m%d",
        "%y-%m-%d",
        "%y.%m.%d",
        "%y/%m/%d",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(
                text,
                date_format,
            ).date()
        except ValueError:
            continue

    return None

def clean_number(value):
    """
    쉼표·공백·통화기호 등이 포함된 문자열을 float으로 정리한다.
    괄호형 음수도 지원한다.
    """
    text = str(value or "").strip()

    if not text:
        return None

    if text.lower() in {
        "-",
        "n/a",
        "na",
        "none",
        "null",
    }:
        return None

    is_negative = (
        text.startswith("-")
        or (
            text.startswith("(")
            and text.endswith(")")
        )
    )

    cleaned = re.sub(
        r"[^0-9.]",
        "",
        text.replace(",", ""),
    )

    if not cleaned:
        return None

    try:
        number = float(cleaned)
        return -number if is_negative else number
    except ValueError:
        return None

def atomic_write_text(path, text):
    """
    GitHub Pages 배포 시 저장 도중 파일이 손상되는 것을 방지한다.
    임시 파일 작성 후 원자적으로 교체한다.
    """
    temporary_path = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary_path.write_text(
        text,
        encoding="utf-8",
    )

    temporary_path.replace(path)

# =========================================================
# 4. SQLite Database
# =========================================================
def get_db_conn():
    """SQLite 연결 및 테이블 생성."""
    connection = sqlite3.connect(
        DB_PATH,
        timeout=30,
    )

    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=30000")

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS index_raw (
            dt TEXT NOT NULL,
            region TEXT NOT NULL,
            value REAL NOT NULL,
            index_name TEXT NOT NULL,
            source_file TEXT,
            created_at TEXT,
            PRIMARY KEY (dt, region, index_name)
        )
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_audit (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            status TEXT,
            message TEXT,
            rows_added INTEGER,
            total_rows INTEGER,
            index_name TEXT
        )
        """
    )

    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_index_raw_name_date
        ON index_raw (index_name, dt)
        """
    )

    connection.commit()

    return connection

def cleanup_invalid_regions():
    """
    기존 DB의 날짜·헤더명 등 잘못 저장된 region 데이터를 정리한다.
    """
    connection = get_db_conn()

    try:
        invalid_names = list(INVALID_REGION_NAMES)

        placeholders = ",".join(
            ["?"] * len(invalid_names)
        )

        connection.execute(
            f"""
            DELETE FROM index_raw
            WHERE TRIM(region) IN ({placeholders})
            """,
            invalid_names,
        )

        bdi_placeholders = ",".join(
            ["?"] * len(BDI_REGION_PRIORITY)
        )

        connection.execute(
            f"""
            DELETE FROM index_raw
            WHERE index_name = 'BDI'
              AND TRIM(region) NOT IN ({bdi_placeholders})
            """,
            BDI_REGION_PRIORITY,
        )

        connection.commit()
        log("[DB] 유효하지 않은 region 데이터 정리 완료")

    finally:
        connection.close()

def get_total_row_count(index_name):
    index_name = normalize_index_name(index_name)

    connection = get_db_conn()

    try:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM index_raw
            WHERE index_name = ?
            """,
            (index_name,),
        ).fetchone()

        return int(row[0] if row else 0)

    finally:
        connection.close()

def get_latest_date(index_name):
    """DB에 저장된 해당 지수의 최신 날짜를 반환한다."""
    index_name = normalize_index_name(index_name)

    connection = get_db_conn()

    try:
        row = connection.execute(
            """
            SELECT MAX(dt)
            FROM index_raw
            WHERE index_name = ?
            """,
            (index_name,),
        ).fetchone()

        if not row or not row[0]:
            return None

        return pd.to_datetime(
            row[0],
            errors="coerce",
        ).date()

    finally:
        connection.close()

def load_long_df_from_db(index_name):
    """DB 데이터를 Long Format DataFrame으로 읽는다."""
    index_name = normalize_index_name(index_name)

    connection = get_db_conn()

    try:
        dataframe = pd.read_sql_query(
            """
            SELECT
                dt AS DATE,
                region AS REGION,
                value AS VALUE,
                index_name AS INDEX_NAME
            FROM index_raw
            WHERE index_name = ?
            ORDER BY dt, region
            """,
            connection,
            params=[index_name],
        )
    finally:
        connection.close()

    if dataframe.empty:
        return pd.DataFrame(
            columns=[
                "DATE",
                "YEAR",
                "REGION",
                "VALUE",
                "INDEX_NAME",
            ]
        )

    dataframe["DATE"] = pd.to_datetime(
        dataframe["DATE"],
        errors="coerce",
    )

    dataframe["VALUE"] = pd.to_numeric(
        dataframe["VALUE"],
        errors="coerce",
    )

    dataframe["REGION"] = dataframe["REGION"].apply(
        normalize_region_name
    )

    dataframe = dataframe.dropna(
        subset=["DATE", "VALUE"],
    )

    dataframe = dataframe[
        dataframe["REGION"].apply(
            lambda region: is_valid_region_for_index(
                region,
                index_name,
            )
        )
    ]

    dataframe["YEAR"] = dataframe["DATE"].dt.year

    return dataframe[
        [
            "DATE",
            "YEAR",
            "REGION",
            "VALUE",
            "INDEX_NAME",
        ]
    ].reset_index(drop=True)

def upsert_long_df_to_db(dataframe, index_name):
    """
    날짜·항목·지수별 데이터를 누적 저장한다.

    동일 키가 이미 있으면 최근 수집된 값으로 업데이트한다.
    """
    index_name = normalize_index_name(index_name)

    if dataframe is None or dataframe.empty:
        return 0, get_total_row_count(index_name)

    connection = get_db_conn()
    cursor = connection.cursor()

    now = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    processed_rows = 0

    try:
        for _, row in dataframe.iterrows():
            date_value = pd.to_datetime(
                row.get("DATE"),
                errors="coerce",
            )

            region = normalize_region_name(
                row.get("REGION")
            )

            numeric_value = pd.to_numeric(
                row.get("VALUE"),
                errors="coerce",
            )

            if pd.isna(date_value):
                continue

            if not is_valid_region_for_index(
                region,
                index_name,
            ):
                continue

            if pd.isna(numeric_value):
                continue

            cursor.execute(
                """
                INSERT INTO index_raw (
                    dt,
                    region,
                    value,
                    index_name,
                    source_file,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(dt, region, index_name)
                DO UPDATE SET
                    value = excluded.value,
                    source_file = excluded.source_file,
                    created_at = excluded.created_at
                """,
                (
                    date_value.strftime("%Y-%m-%d"),
                    region,
                    float(numeric_value),
                    index_name,
                    "Korea Shipping Gazette",
                    now,
                ),
            )

            processed_rows += 1

        connection.commit()

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

    total_rows = get_total_row_count(index_name)

    return processed_rows, total_rows

def save_crawl_audit(
    index_name,
    started_at,
    finished_at,
    status,
    message,
    rows_added,
    total_rows,
):
    """크롤링 실행 이력을 저장한다."""
    connection = get_db_conn()

    try:
        connection.execute(
            """
            INSERT INTO crawl_audit (
                started_at,
                finished_at,
                status,
                message,
                rows_added,
                total_rows,
                index_name
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                started_at,
                finished_at,
                status,
                str(message)[:1000],
                int(rows_added or 0),
                int(total_rows or 0),
                normalize_index_name(index_name),
            ),
        )

        connection.commit()

    finally:
        connection.close()

def get_last_sync_meta(index_name):
    """대시보드 표시용 최근 동기화 정보를 조회한다."""
    index_name = normalize_index_name(index_name)

    connection = get_db_conn()

    try:
        row = connection.execute(
            """
            SELECT
                started_at,
                finished_at,
                status,
                message,
                rows_added,
                total_rows
            FROM crawl_audit
            WHERE index_name = ?
            ORDER BY audit_id DESC
            LIMIT 1
            """,
            (index_name,),
        ).fetchone()

        if not row:
            return {}

        return {
            "started_at": row[0],
            "finished_at": row[1],
            "status": row[2],
            "message": row[3],
            "rows_added": row[4],
            "total_rows": row[5],
        }

    finally:
        connection.close()

# =========================================================
# 5. Selenium Crawler
# =========================================================
def get_driver(driver_path="", headless=True):
    """Chrome Selenium Driver를 생성한다."""
    options = Options()

    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ko-KR")
    options.add_argument("--window-size=1600,1000")

    if headless:
        options.add_argument("--headless=new")

    chrome_binary = os.getenv("CHROME_BINARY", "").strip()

    if chrome_binary:
        options.binary_location = chrome_binary

    if driver_path:
        return webdriver.Chrome(
            service=Service(driver_path),
            options=options,
        )

    try:
        return webdriver.Chrome(options=options)

    except WebDriverException as first_error:
        if not HAS_WEBDRIVER_MANAGER:
            raise first_error

        log(
            "[DRIVER] Selenium Manager 실행 실패. "
            "webdriver-manager를 사용합니다."
        )

        installed_driver = ChromeDriverManager().install()

        return webdriver.Chrome(
            service=Service(installed_driver),
            options=options,
        )

def handle_login_alerts(
    driver,
    timeout=1.0,
    max_alerts=3,
):
    """로그인 직후 표시될 수 있는 Alert를 처리한다."""
    for _ in range(max_alerts):
        try:
            WebDriverWait(driver, timeout).until(
                EC.alert_is_present()
            )

            alert = driver.switch_to.alert
            log(f"[ALERT] {alert.text}")
            alert.accept()

            time.sleep(0.2)

        except (
            TimeoutException,
            NoAlertPresentException,
        ):
            return

def login(driver, user_id, password):
    """KSG 로그인."""
    if not user_id or not password:
        raise RuntimeError(
            "KSG_ID 또는 KSG_PW 환경변수가 설정되지 않았습니다."
        )

    driver.get(BASE_URL)

    wait = WebDriverWait(driver, 25)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, "로그인")
        )
    ).click()

    id_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginId")
        )
    )

    password_input = wait.until(
        EC.presence_of_element_located(
            (By.ID, "loginPass")
        )
    )

    id_input.clear()
    id_input.send_keys(user_id)

    password_input.clear()
    password_input.send_keys(password)

    driver.execute_script(
        """
        if (typeof loginSubmitForm === 'function') {
            loginSubmitForm(document.form1);
            return;
        }

        const form = document.querySelector('form');
        if (form) {
            form.submit();
            return;
        }

        throw new Error('로그인 제출 함수를 찾지 못했습니다.');
        """
    )

    handle_login_alerts(driver)

    time.sleep(1.2)

    log("[LOGIN] KSG 로그인 완료")

def go_to_index_page(driver, index_name):
    """SCFI 또는 BDI 메뉴로 이동한다."""
    index_name = normalize_index_name(index_name)

    wait = WebDriverWait(driver, 25)

    wait.until(
        EC.element_to_be_clickable(
            (By.LINK_TEXT, index_name)
        )
    ).click()

    try:
        wait.until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "table")
            )
        )
    except TimeoutException:
        pass

    time.sleep(0.8)

    log(f"[CRAWL] {index_name} 페이지 진입 완료")

def parse_current_table(driver, index_name):
    """
    현재 페이지의 후보 테이블을 점수화하여 가장 적합한 테이블을 추출한다.
    """
    index_name = normalize_index_name(index_name)

    script = f"""
    const indexName = "{index_name}";
    const tables = Array.from(document.querySelectorAll("table"));

    function scoreTable(table) {{
        const text = (table.innerText || "").toUpperCase();
        const headers = Array.from(
            table.querySelectorAll("thead th, thead td")
        ).map(cell => (cell.innerText || "").trim().toUpperCase());

        const rowCount = table.querySelectorAll("tbody tr").length;

        let score = 0;

        if (rowCount > 0) score += 10;
        if (rowCount >= 5) score += 10;
        if (text.includes(indexName)) score += 30;
        if (text.includes("일자") || text.includes("DATE")) score += 20;

        if (indexName === "BDI") {{
            if (text.includes("BDI")) score += 10;
            if (text.includes("BCI")) score += 5;
            if (text.includes("BPI")) score += 5;
        }}

        if (headers.length >= 2) score += 5;

        return score;
    }}

    let target = null;
    let bestScore = -1;

    for (const table of tables) {{
        const score = scoreTable(table);

        if (score > bestScore) {{
            bestScore = score;
            target = table;
        }}
    }}

    if (!target || bestScore < 10) {{
        return {{
            headerRows: [],
            rows: [],
            score: bestScore
        }};
    }}

    const headerRows = Array.from(
        target.querySelectorAll("thead tr")
    ).map(row => {{
        return Array.from(
            row.querySelectorAll("th, td")
        ).map(cell => (cell.innerText || "").trim());
    }});

    const rows = Array.from(
        target.querySelectorAll("tbody tr")
    ).map(row => {{
        return Array.from(
            row.querySelectorAll("th, td")
        ).map(cell => (cell.innerText || "").trim());
    }}).filter(row => row.length > 0);

    return {{
        headerRows: headerRows,
        rows: rows,
        score: bestScore
    }};
    """

    result = driver.execute_script(script)

    header_rows = result.get("headerRows", [])
    rows = result.get("rows", [])

    if not rows:
        return pd.DataFrame()

    max_length = max(len(row) for row in rows)

    headers = []

    for column_index in range(max_length):
        header_value = ""

        for header_row in reversed(header_rows):
            if column_index >= len(header_row):
                continue

            candidate = normalize_region_name(
                header_row[column_index]
            )

            if candidate:
                header_value = candidate
                break

        if not header_value:
            header_value = f"COL_{column_index + 1}"

        headers.append(header_value)

    normalized_rows = []

    for row in rows:
        padded_row = list(row) + [""] * (
            max_length - len(row)
        )

        output = {}

        for column_index, raw_value in enumerate(
            padded_row
        ):
            column_name = headers[column_index]

            if column_index == 0:
                output[column_name] = parse_date(raw_value)
            else:
                output[column_name] = clean_number(raw_value)

        normalized_rows.append(output)

    dataframe = pd.DataFrame(normalized_rows)

    if dataframe.empty:
        return dataframe

    first_column = dataframe.columns[0]

    dataframe = dataframe.dropna(
        subset=[first_column]
    )

    return dataframe.reset_index(drop=True)

def get_table_signature(driver):
    """페이지 변경 여부 확인을 위한 첫 행 시그니처."""
    script = """
    const tables = Array.from(
        document.querySelectorAll("table")
    );

    if (!tables.length) return "";

    const table = tables.sort((a, b) => {
        return b.querySelectorAll("tbody tr").length
            - a.querySelectorAll("tbody tr").length;
    })[0];

    const firstRow = table.querySelector("tbody tr");

    if (!firstRow) return "";

    return Array.from(
        firstRow.querySelectorAll("th, td")
    ).slice(0, 6).map(
        cell => (cell.innerText || "").trim()
    ).join("|");
    """

    try:
        return str(
            driver.execute_script(script) or ""
        ).strip()
    except Exception:
        return ""

def click_next_page(driver):
    """다음 페이지 버튼 또는 goPage JavaScript를 이용해 이동한다."""
    current_page_script = """
    const selectors = [
        ".paginate strong",
        ".paging strong",
        ".pagination .active",
        ".pagination strong"
    ];

    for (const selector of selectors) {
        const element = document.querySelector(selector);

        if (
            element
            && /^\\d+$/.test(
                (element.innerText || "").trim()
            )
        ) {
            return Number(
                (element.innerText || "").trim()
            );
        }
    }

    return null;
    """

    try:
        current_page = driver.execute_script(
            current_page_script
        )

        if current_page:
            has_go_page = driver.execute_script(
                "return typeof goPage === 'function';"
            )

            if has_go_page:
                driver.execute_script(
                    f"goPage({int(current_page) + 1});"
                )
                return True
    except Exception:
        pass

    selectors = [
        (By.CSS_SELECTOR, "a.next"),
        (By.CSS_SELECTOR, ".paginate a.next"),
        (By.CSS_SELECTOR, ".paging a.next"),
        (By.LINK_TEXT, "다음"),
        (By.XPATH, "//a[contains(normalize-space(), '다음')]"),
    ]

    for by, selector in selectors:
        try:
            elements = driver.find_elements(by, selector)

            for element in elements:
                if not element.is_displayed():
                    continue

                classes = (
                    element.get_attribute("class") or ""
                ).lower()

                if "disabled" in classes:
                    continue

                driver.execute_script(
                    """
                    arguments[0].scrollIntoView(
                        {block: 'center'}
                    );
                    """,
                    element,
                )

                driver.execute_script(
                    "arguments[0].click();",
                    element,
                )

                return True

        except Exception:
            continue

    return False

def wait_for_table_change(
    driver,
    previous_signature,
    timeout=12,
):
    """다음 페이지의 테이블 렌더링까지 대기한다."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        signature = get_table_signature(driver)

        if signature and signature != previous_signature:
            return True

        time.sleep(0.3)

    return False

def melt_to_long(dataframe, index_name):
    """크롤링된 Wide 테이블을 DB 저장용 Long Format으로 변환한다."""
    index_name = normalize_index_name(index_name)

    empty_dataframe = pd.DataFrame(
        columns=[
            "DATE",
            "REGION",
            "VALUE",
            "INDEX_NAME",
        ]
    )

    if dataframe is None or dataframe.empty:
        return empty_dataframe

    working = dataframe.copy()

    date_column = working.columns[0]

    working[date_column] = pd.to_datetime(
        working[date_column],
        errors="coerce",
    )

    working = working.dropna(subset=[date_column])

    if working.empty:
        return empty_dataframe

    value_columns = [
        column
        for column in working.columns
        if (
            column != date_column
            and is_valid_region_for_index(
                column,
                index_name,
            )
        )
    ]

    numeric_columns = []

    for column in value_columns:
        working[column] = pd.to_numeric(
            working[column],
            errors="coerce",
        )

        if working[column].notna().any():
            numeric_columns.append(column)

    if not numeric_columns:
        return empty_dataframe

    melted = working.melt(
        id_vars=[date_column],
        value_vars=numeric_columns,
        var_name="REGION",
        value_name="VALUE",
    )

    melted = melted.rename(
        columns={date_column: "DATE"}
    )

    melted["REGION"] = melted["REGION"].apply(
        normalize_region_name
    )

    melted["VALUE"] = pd.to_numeric(
        melted["VALUE"],
        errors="coerce",
    )

    melted = melted.dropna(
        subset=["DATE", "REGION", "VALUE"]
    )

    melted = melted[
        melted["REGION"].apply(
            lambda region: is_valid_region_for_index(
                region,
                index_name,
            )
        )
    ]

    melted["INDEX_NAME"] = index_name

    return melted[
        [
            "DATE",
            "REGION",
            "VALUE",
            "INDEX_NAME",
        ]
    ].drop_duplicates().reset_index(drop=True)

def crawl_incremental(
    driver,
    index_name,
    stop_date=None,
    max_pages=2000,
    pause=0.4,
):
    """
    최신 페이지부터 순차 탐색한다.

    stop_date가 존재하면 해당 날짜보다 같거나 과거인 행이
    나타나는 시점에서 종료한다.

    단, 최신 데이터 수정 가능성에 대응하기 위해
    DB 최신일 - OVERLAP_DAYS 이후 데이터를 재수집한다.
    """
    index_name = normalize_index_name(index_name)

    frames = []
    seen_signatures = set()

    for page_number in range(1, max_pages + 1):
        dataframe = parse_current_table(
            driver,
            index_name,
        )

        if dataframe.empty:
            log(
                f"[CRAWL] {index_name} "
                f"{page_number}페이지 데이터 없음"
            )
            break

        date_column = dataframe.columns[0]

        parsed_dates = pd.to_datetime(
            dataframe[date_column],
            errors="coerce",
        ).dt.date

        dataframe = dataframe[
            parsed_dates.notna()
        ].copy()

        parsed_dates = pd.to_datetime(
            dataframe[date_column],
            errors="coerce",
        ).dt.date

        if dataframe.empty:
            log(
                f"[CRAWL] {index_name} "
                f"{page_number}페이지 날짜 데이터 없음"
            )
            break

        if stop_date is not None:
            recent_rows = dataframe[
                parsed_dates > stop_date
            ].copy()

            if not recent_rows.empty:
                frames.append(recent_rows)

            if (parsed_dates <= stop_date).any():
                log(
                    f"[CRAWL] {index_name} "
                    f"{page_number}페이지에서 "
                    f"중단 기준일 {stop_date} 도달"
                )
                break
        else:
            frames.append(dataframe)

        signature = get_table_signature(driver)

        if signature in seen_signatures:
            log("[CRAWL] 반복 페이지 감지로 종료")
            break

        seen_signatures.add(signature)

        log(
            f"[CRAWL] {index_name} "
            f"{page_number}페이지 수집: "
            f"{len(dataframe)}행"
        )

        if not click_next_page(driver):
            log("[CRAWL] 다음 페이지 없음")
            break

        if not wait_for_table_change(
            driver,
            signature,
        ):
            log(
                "[CRAWL] 다음 페이지 테이블 "
                "갱신 시간 초과"
            )
            break

        time.sleep(pause)

    if not frames:
        return pd.DataFrame()

    return pd.concat(
        frames,
        ignore_index=True,
    )

def run_incremental_sync(
    index_name,
    user_id,
    password,
    driver_path="",
    headless=True,
    max_pages=2000,
):
    """SCFI 또는 BDI 데이터를 증분 수집하고 DB에 누적 저장한다."""
    index_name = normalize_index_name(index_name)

    with crawl_lock:
        started_at = datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        driver = None

        try:
            latest_date = get_latest_date(index_name)

            if latest_date:
                stop_date = latest_date - timedelta(
                    days=OVERLAP_DAYS
                )

                log(
                    f"[DB] {index_name} "
                    f"최신 DB 일자: {latest_date} / "
                    f"재수집 기준일: {stop_date}"
                )
            else:
                stop_date = None

                log(
                    f"[DB] {index_name} "
                    "기존 데이터 없음 - 전체 수집 시작"
                )

            driver = get_driver(
                driver_path=driver_path,
                headless=headless,
            )

            login(driver, user_id, password)

            go_to_index_page(driver, index_name)

            crawled_wide = crawl_incremental(
                driver=driver,
                index_name=index_name,
                stop_date=stop_date,
                max_pages=max_pages,
            )

            crawled_long = melt_to_long(
                crawled_wide,
                index_name,
            )

            rows_processed, total_rows = upsert_long_df_to_db(
                crawled_long,
                index_name,
            )

            cleanup_invalid_regions()

            finished_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            save_crawl_audit(
                index_name=index_name,
                started_at=started_at,
                finished_at=finished_at,
                status="SUCCESS",
                message="OK",
                rows_added=rows_processed,
                total_rows=total_rows,
            )

            log(
                f"[SUCCESS] {index_name} "
                f"처리행={rows_processed:,}, "
                f"DB 누적행={total_rows:,}"
            )

            return {
                "ok": True,
                "index_name": index_name,
                "rows_added": rows_processed,
                "total_rows": total_rows,
            }

        except Exception as error:
            activate_file_logging()

            logger.exception(
                f"[ERROR] {index_name} 수집 실패: {error}"
            )

            finished_at = datetime.now().strftime(
                "%Y-%m-%d %H:%M:%S"
            )

            total_rows = get_total_row_count(index_name)

            try:
                save_crawl_audit(
                    index_name=index_name,
                    started_at=started_at,
                    finished_at=finished_at,
                    status="ERROR",
                    message=str(error),
                    rows_added=0,
                    total_rows=total_rows,
                )
            except Exception as audit_error:
                logger.error(
                    f"[ERROR] 실패 이력 저장 실패: "
                    f"{audit_error}"
                )

            return {
                "ok": False,
                "index_name": index_name,
                "error": str(error),
                "total_rows": total_rows,
            }

        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass

# =========================================================
# 6. Static Data Export
# =========================================================
def dataframe_to_records(dataframe):
    """DataFrame을 JSON 저장용 구조로 변환한다."""
    if dataframe is None or dataframe.empty:
        return []

    output = dataframe.copy()

    if "DATE" in output.columns:
        output["DATE"] = pd.to_datetime(
            output["DATE"],
            errors="coerce",
        ).dt.strftime("%Y-%m-%d")

    output = output.replace(
        [float("inf"), float("-inf")],
        pd.NA,
    )

    output = output.where(
        pd.notnull(output),
        None,
    )

    return json.loads(
        output.to_json(
            orient="records",
            force_ascii=False,
            date_format="iso",
        )
    )

def build_static_json():
    """누적 SQLite 데이터를 GitHub Pages용 JSON 파일로 생성한다."""
    payload = {
        "generated_at": datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "source": "Korea Shipping Gazette",
        "indexes": {},
    }

    for index_name in SUPPORTED_INDEXES:
        dataframe = load_long_df_from_db(index_name)

        regions = sort_regions_for_display(
            dataframe["REGION"].unique().tolist()
            if not dataframe.empty
            else [],
            index_name,
        )

        payload["indexes"][index_name] = {
            "records": dataframe_to_records(dataframe),
            "regions": regions,
            "last_sync": get_last_sync_meta(index_name),
            "total_points": int(len(dataframe)),
        }

    atomic_write_text(
        JSON_PATH,
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
    )

    log(f"[STATIC] JSON 생성: {JSON_PATH.name}")

# =========================================================
# 7. GitHub Pages Static HTML
# =========================================================
STATIC_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shipping Index Dashboard</title>

<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

<style>
:root{
  --bg:#eef3f8;
  --surface:#ffffff;
  --border:#dbe4ee;
  --text:#1f2937;
  --muted:#64748b;
  --primary:#2563eb;
  --shadow:0 8px 28px rgba(15,23,42,.08);
}
*{box-sizing:border-box}
body{
  margin:0;
  font-family:"Segoe UI",Arial,sans-serif;
  background:linear-gradient(180deg,#f7fafc 0%,var(--bg) 100%);
  color:var(--text);
}
.page-wrap{max-width:1600px;margin:auto;padding:24px}
.layout{
  display:grid;
  grid-template-columns:330px minmax(0,1fr);
  gap:22px;
  align-items:start;
}
.sidebar,.card,.main-header{
  background:var(--surface);
  border:1px solid var(--border);
  border-radius:18px;
  box-shadow:var(--shadow);
}
.sidebar{padding:20px;position:sticky;top:20px}
.main-header{padding:22px 24px;margin-bottom:20px}
.card{padding:20px;margin-bottom:20px}
.sidebar-section{
  border-bottom:1px solid #e5edf5;
  padding-bottom:18px;
  margin-bottom:18px;
}
.sidebar-section:last-child{border:0;padding:0;margin:0}
.sidebar-label{
  font-size:12px;
  font-weight:800;
  letter-spacing:.5px;
  color:#475569;
  text-transform:uppercase;
  margin-bottom:10px;
}
.main-title{margin:0 0 10px;font-size:30px;color:#0f172a}
.meta{font-size:13px;line-height:1.8;color:var(--muted)}
.meta a{color:var(--primary);font-weight:700;text-decoration:none}
.index-tabs{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.index-tab,.btn{
  border:1px solid #cbd5e1;
  background:#fff;
  border-radius:10px;
  padding:10px;
  min-height:40px;
  text-align:center;
  cursor:pointer;
  color:#334155;
  font-size:13px;
  font-weight:800;
}
.index-tab.active,.btn.primary{
  color:#fff;
  border-color:var(--primary);
  background:var(--primary);
}
.field-block{margin-bottom:14px}
.field-label{
  display:block;
  margin-bottom:6px;
  font-size:12px;
  font-weight:700;
  color:#334155;
}
select,input[type="date"],input[type="number"]{
  width:100%;
  min-height:40px;
  padding:9px 10px;
  border:1px solid #cbd5e1;
  border-radius:10px;
  background:#fff;
}
select[multiple]{min-height:230px}
input[type="color"]{
  width:45px;
  height:32px;
  padding:2px;
  border:1px solid #cbd5e1;
  border-radius:7px;
}
.actions{display:flex;gap:8px;flex-wrap:wrap}
.metrics-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
  gap:14px;
}
.metric{
  background:#f8fafc;
  border:1px solid #e5edf5;
  border-radius:14px;
  padding:16px;
}
.metric-label{
  color:var(--muted);
  font-size:12px;
  font-weight:800;
  text-transform:uppercase;
}
.metric-value{
  margin-top:7px;
  font-size:24px;
  font-weight:800;
  color:#0f172a;
}
.section-title{margin:0 0 14px;font-size:18px;color:#0f172a}
.toolbar{
  display:flex;
  gap:12px;
  align-items:center;
  justify-content:space-between;
  flex-wrap:wrap;
  margin-bottom:10px;
}
.chart{min-height:570px}
.table-wrap{overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{
  padding:10px 12px;
  text-align:left;
  white-space:nowrap;
  border:1px solid #e5e7eb;
}
th{background:#f1f5f9;color:#334155}
.peak{color:#b91c1c;font-weight:800}
.trough{color:#1d4ed8;font-weight:800}
.spread{color:#7c3aed;font-weight:800}
.color-row{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:8px;
  margin:7px 0;
}
.color-row label{
  max-width:230px;
  font-size:12px;
  overflow:hidden;
  white-space:nowrap;
  text-overflow:ellipsis;
}
.loading,.empty{
  padding:48px;
  text-align:center;
  color:var(--muted);
  font-weight:700;
}
@media(max-width:1100px){
  .layout{grid-template-columns:1fr}
  .sidebar{position:static}
}
@media(max-width:650px){
  .page-wrap{padding:14px}
  .main-title{font-size:24px}
}
</style>
</head>

<body>
<div class="page-wrap">
  <div class="layout">

    <aside class="sidebar">
      <div class="sidebar-section">
        <div class="sidebar-label">Index</div>
        <div class="index-tabs">
          <button id="tabSCFI"
                  class="index-tab active"
                  onclick="changeIndex('SCFI')">
            SCFI
          </button>
          <button id="tabBDI"
                  class="index-tab"
                  onclick="changeIndex('BDI')">
            BDI
          </button>
        </div>
      </div>

      <div class="sidebar-section">
        <div class="sidebar-label">Date Range</div>

        <div class="field-block">
          <label class="field-label" for="startDate">시작일</label>
          <input id="startDate"
                 type="date"
                 onchange="renderDashboard()">
        </div>

        <div class="field-block">
          <label class="field-label" for="endDate">종료일</label>
          <input id="endDate"
                 type="date"
                 onchange="renderDashboard()">
        </div>

        <div class="field-block">
          <label class="field-label" for="yearRange">연도 범위</label>
          <select id="yearRange" onchange="renderDashboard()">
            <option value="0">전체 기간</option>
            <option value="1">최근 1년</option>
            <option value="3">최근 3년</option>
            <option value="5">최근 5년</option>
            <option value="10">최근 10년</option>
          </select>
        </div>
      </div>

      <div class="sidebar-section">
        <div class="sidebar-label">Routes</div>

        <div class="field-block">
          <label class="field-label" for="regionSelect">
            항목 복수 선택
          </label>
          <select id="regionSelect"
                  multiple
                  onchange="onRegionChange()">
          </select>
        </div>

        <div class="actions">
          <button class="btn primary" onclick="selectAllRegions()">
            전체 선택
          </button>
          <button class="btn" onclick="clearRegions()">
            선택 해제
          </button>
        </div>
      </div>

      <div class="sidebar-section">
        <div class="sidebar-label">Chart Style</div>

        <div class="field-block">
          <label class="field-label" for="lineWidth">선 두께</label>
          <input id="lineWidth"
                 type="number"
                 min="0.5"
                 max="10"
                 step="0.5"
                 value="2.5"
                 onchange="renderDashboard()">
        </div>

        <div class="field-block">
          <label class="field-label" for="palette">색상 테마</label>
          <select id="palette" onchange="resetPaletteAndRender()">
            <option value="Auto">Auto</option>
            <option value="Grey">Grey</option>
            <option value="Red">Red</option>
            <option value="Blue">Blue</option>
          </select>
        </div>

        <div class="field-block">
          <label class="field-label">항목별 색상</label>
          <div id="colorPanel"></div>
        </div>
      </div>
    </aside>

    <main>
      <div class="main-header">
        <h1 class="main-title">Shipping Index Dashboard</h1>

        <div class="meta">
          <strong>Source:</strong>
          <a href="https://www.ksg.co.kr/"
             target="_blank"
             rel="noopener noreferrer">
            Korea Shipping Gazette
          </a>
          &nbsp;|&nbsp;
          <strong>Current Index:</strong>
          <span id="currentIndex">SCFI</span>
          &nbsp;|&nbsp;
          <strong>Last sync:</strong>
          <span id="lastSync">-</span>
          &nbsp;|&nbsp;
          <strong>GitHub Actions generated:</strong>
          <span id="generatedAt">-</span>
        </div>
      </div>

      <div id="loading" class="card loading">
        데이터를 불러오는 중입니다.
      </div>

      <div id="dashboard" style="display:none;">

        <div class="card">
          <div class="section-title">Current Highlights</div>

          <div class="metrics-grid">
            <div class="metric">
              <div class="metric-label">조회 시작일</div>
              <div id="shownStart" class="metric-value">-</div>
            </div>

            <div class="metric">
              <div class="metric-label">조회 종료일</div>
              <div id="shownEnd" class="metric-value">-</div>
            </div>

            <div class="metric">
              <div class="metric-label">선택 항목 수</div>
              <div id="regionCount" class="metric-value">-</div>
            </div>

            <div class="metric">
              <div class="metric-label">표시 데이터 수</div>
              <div id="shownRows" class="metric-value">-</div>
            </div>
          </div>
        </div>

        <div class="card">
          <div class="toolbar">
            <div>
              <div class="section-title" style="margin-bottom:4px;">
                항목별 비교 차트
              </div>
              <div class="meta">
                선택한 복수 항목을 한 화면에서 비교합니다.
              </div>
            </div>

            <div class="actions">
              <button class="btn primary" onclick="downloadCsv()">
                CSV 다운로드
              </button>
              <button class="btn" onclick="downloadChartHtml()">
                차트 HTML
              </button>
              <button class="btn" onclick="downloadChartPng()">
                PNG
              </button>
            </div>
          </div>

          <div id="chart" class="chart"></div>
        </div>

        <div class="card">
          <div class="toolbar">
            <div class="section-title" style="margin:0;">
              항목별 최고점 / 최저점 / 스프레드
            </div>

            <select id="sortMode"
                    style="width:190px;"
                    onchange="renderHighlights()">
              <option value="region">항목명</option>
              <option value="peak_desc">최고값 내림차순</option>
              <option value="trough_asc">최저값 오름차순</option>
              <option value="spread_desc">스프레드 내림차순</option>
            </select>
          </div>

          <div id="highlightTable" class="table-wrap"></div>
        </div>
      </div>
    </main>
  </div>
</div>

<script>
let dashboardData = null;
let selectedIndex = "SCFI";
let filteredRecords = [];
let currentRegions = [];
let regionColors = {};

const BDI_LABEL_MAP = {
  "BDI":"BDI (Baltic Exchange Dry Index)",
  "BCI":"BCI (Baltic Capesize Index)",
  "BPI":"BPI (Baltic Panamax Index)",
  "BSI":"BSI (Baltic Supramax Index)",
  "BHSI":"BHSI (Baltic Handysize Index)",
  "BDTI":"BDTI (Baltic Dirty Tanker Index)",
  "BCTI":"BCTI (Baltic Clean Tanker Index)"
};

function regionLabel(region) {
  return selectedIndex === "BDI"
    ? (BDI_LABEL_MAP[region] || region)
    : region;
}

function getPalette(name) {
  const palettes = {
    Auto:[
      "#e53935","#fb8c00","#fdd835","#43a047",
      "#00acc1","#1e88e5","#3949ab","#8e24aa",
      "#d81b60","#6d4c41","#00897b","#7cb342"
    ],
    Grey:["#111827","#374151","#6b7280","#9ca3af","#d1d5db"],
    Red:["#7f1d1d","#b91c1c","#dc2626","#ef4444","#f87171","#ec4899"],
    Blue:["#0f172a","#1d4ed8","#2563eb","#3b82f6","#60a5fa","#0ea5e9"]
  };

  return palettes[name] || palettes.Auto;
}

function selectedRegions() {
  const select = document.getElementById("regionSelect");

  return Array.from(select.selectedOptions).map(
    option => option.value
  );
}

function selectAllRegions() {
  Array.from(document.getElementById("regionSelect").options)
    .forEach(option => option.selected = true);

  renderColorPanel();
  renderDashboard();
}

function clearRegions() {
  Array.from(document.getElementById("regionSelect").options)
    .forEach(option => option.selected = false);

  renderColorPanel();
  renderDashboard();
}

function onRegionChange() {
  renderColorPanel();
  renderDashboard();
}

function resetPaletteAndRender() {
  regionColors = {};
  renderColorPanel();
  renderDashboard();
}

function changeIndex(indexName) {
  selectedIndex = indexName;
  regionColors = {};

  document.getElementById("tabSCFI").classList.toggle(
    "active",
    indexName === "SCFI"
  );

  document.getElementById("tabBDI").classList.toggle(
    "active",
    indexName === "BDI"
  );

  document.getElementById("currentIndex").textContent =
    indexName;

  populateRegions(true);
  setDateBounds();
  renderDashboard();
}

function populateRegions(selectAll) {
  const select = document.getElementById("regionSelect");
  const indexData = dashboardData.indexes[selectedIndex] || {};
  const regions = indexData.regions || [];

  select.innerHTML = "";

  regions.forEach(region => {
    const option = new Option(
      regionLabel(region),
      region
    );

    option.selected = selectAll;
    select.add(option);
  });

  renderColorPanel();
}

function renderColorPanel() {
  const panel = document.getElementById("colorPanel");
  const regions = selectedRegions();
  const colors = getPalette(
    document.getElementById("palette").value
  );

  panel.innerHTML = "";

  regions.forEach((region, index) => {
    if (!regionColors[region]) {
      regionColors[region] =
        colors[index % colors.length];
    }

    const wrapper = document.createElement("div");
    wrapper.className = "color-row";

    const label = document.createElement("label");
    label.textContent = regionLabel(region);

    const color = document.createElement("input");
    color.type = "color";
    color.value = regionColors[region];

    color.addEventListener("change", event => {
      regionColors[region] = event.target.value;
      renderDashboard();
    });

    wrapper.appendChild(label);
    wrapper.appendChild(color);
    panel.appendChild(wrapper);
  });
}

function getColor(region, index) {
  if (regionColors[region]) {
    return regionColors[region];
  }

  const colors = getPalette(
    document.getElementById("palette").value
  );

  return colors[index % colors.length];
}

function setDateBounds() {
  const indexData = dashboardData.indexes[selectedIndex] || {};
  const records = indexData.records || [];

  if (!records.length) {
    return;
  }

  const dates = records
    .map(record => record.DATE)
    .filter(Boolean)
    .sort();

  if (!dates.length) {
    return;
  }

  const start = document.getElementById("startDate");
  const end = document.getElementById("endDate");

  start.min = dates[0];
  start.max = dates[dates.length - 1];
  end.min = dates[0];
  end.max = dates[dates.length - 1];

  start.value = dates[0];
  end.value = dates[dates.length - 1];
}

function currentRecords() {
  const indexData = dashboardData.indexes[selectedIndex] || {};
  const records = indexData.records || [];
  const regions = selectedRegions();

  const startDate = document.getElementById("startDate").value;
  const endDate = document.getElementById("endDate").value;

  const rangeYears = Number(
    document.getElementById("yearRange").value
  );

  let output = records.filter(record => {
    return regions.includes(record.REGION);
  });

  if (startDate) {
    output = output.filter(
      record => record.DATE >= startDate
    );
  }

  if (endDate) {
    output = output.filter(
      record => record.DATE <= endDate
    );
  }

  if (rangeYears > 0 && output.length > 0) {
    const maxYear = Math.max(
      ...output.map(record => Number(record.YEAR))
    );

    const minYear = maxYear - rangeYears + 1;

    output = output.filter(
      record => Number(record.YEAR) >= minYear
    );
  }

  return output.sort((a, b) => {
    if (a.DATE === b.DATE) {
      return String(a.REGION).localeCompare(
        String(b.REGION)
      );
    }

    return String(a.DATE).localeCompare(
      String(b.DATE)
    );
  });
}

function renderDashboard() {
  if (!dashboardData) {
    return;
  }

  filteredRecords = currentRecords();
  currentRegions = selectedRegions();

  const indexData = dashboardData.indexes[selectedIndex] || {};
  const sync = indexData.last_sync || {};

  document.getElementById("lastSync").textContent =
    sync.finished_at || "-";

  document.getElementById("regionCount").textContent =
    currentRegions.length.toLocaleString();

  document.getElementById("shownRows").textContent =
    filteredRecords.length.toLocaleString();

  document.getElementById("shownStart").textContent =
    filteredRecords.length
      ? filteredRecords[0].DATE
      : "-";

  document.getElementById("shownEnd").textContent =
    filteredRecords.length
      ? filteredRecords[filteredRecords.length - 1].DATE
      : "-";

  renderChart();
  renderHighlights();
}

function renderChart() {
  const chart = document.getElementById("chart");

  if (!filteredRecords.length) {
    chart.innerHTML = "";

    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent =
      "선택 조건에 해당하는 데이터가 없습니다.";

    chart.appendChild(empty);
    return;
  }

  const width = Number(
    document.getElementById("lineWidth").value
  ) || 2.5;

  const traces = currentRegions.map(
    (region, index) => {
      const rows = filteredRecords.filter(
        record => record.REGION === region
      );

      return {
        x: rows.map(row => row.DATE),
        y: rows.map(row => Number(row.VALUE)),
        type: "scatter",
        mode: "lines",
        name: regionLabel(region),
        line: {
          width: width,
          color: getColor(region, index)
        },
        hovertemplate:
          "날짜=%{x}<br>"
          + "항목=%{fullData.name}<br>"
          + "지수=%{y:,.2f}<extra></extra>"
      };
    }
  );

  Plotly.react(
    "chart",
    traces,
    {
      template: "plotly_white",
      height: 600,
      margin: {l:65,r:25,t:45,b:55},
      hovermode: "x unified",
      xaxis: {title: "Date"},
      yaxis: {
        title: "Index",
        tickformat: ","
      },
      legend: {
        orientation: "h",
        yanchor: "bottom",
        y: 1.02,
        x: 0
      }
    },
    {
      responsive: true,
      displaylogo: false
    }
  );
}

function numberText(value) {
  return Number(value).toLocaleString(
    undefined,
    {
      maximumFractionDigits: 2
    }
  );
}

function appendCell(row, value, className = "") {
  const cell = document.createElement("td");

  if (className) {
    cell.className = className;
  }

  cell.textContent = value;
  row.appendChild(cell);
}

function renderHighlights() {
  const container = document.getElementById(
    "highlightTable"
  );

  container.innerHTML = "";

  if (!filteredRecords.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "표시할 데이터가 없습니다.";
    container.appendChild(empty);
    return;
  }

  const rows = currentRegions.map(region => {
    const records = filteredRecords.filter(
      record => record.REGION === region
    );

    if (!records.length) {
      return null;
    }

    const sorted = [...records].sort(
      (a, b) => Number(a.VALUE) - Number(b.VALUE)
    );

    const trough = sorted[0];
    const peak = sorted[sorted.length - 1];

    return {
      region: region,
      peakDate: peak.DATE,
      peakValue: Number(peak.VALUE),
      troughDate: trough.DATE,
      troughValue: Number(trough.VALUE),
      spread: Number(peak.VALUE) - Number(trough.VALUE)
    };
  }).filter(Boolean);

  const mode = document.getElementById("sortMode").value;

  rows.sort((a, b) => {
    if (mode === "peak_desc") {
      return b.peakValue - a.peakValue;
    }

    if (mode === "trough_asc") {
      return a.troughValue - b.troughValue;
    }

    if (mode === "spread_desc") {
      return b.spread - a.spread;
    }

    return a.region.localeCompare(b.region);
  });

  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");

  [
    "항목",
    "최고 일자",
    "최고 값",
    "최저 일자",
    "최저 값",
    "스프레드"
  ].forEach(label => {
    const th = document.createElement("th");
    th.textContent = label;
    headRow.appendChild(th);
  });

  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  rows.forEach(item => {
    const row = document.createElement("tr");

    appendCell(row, regionLabel(item.region));
    appendCell(row, item.peakDate);
    appendCell(row, numberText(item.peakValue), "peak");
    appendCell(row, item.troughDate);
    appendCell(row, numberText(item.troughValue), "trough");
    appendCell(row, numberText(item.spread), "spread");

    tbody.appendChild(row);
  });

  table.appendChild(tbody);
  container.appendChild(table);
}

function downloadCsv() {
  if (!filteredRecords.length) {
    alert("다운로드할 데이터가 없습니다.");
    return;
  }

  const headers = [
    "DATE",
    "YEAR",
    "REGION",
    "VALUE",
    "INDEX_NAME"
  ];

  const csv = [
    headers.join(","),
    ...filteredRecords.map(row => {
      return headers.map(key => {
        const text = String(row[key] ?? "");

        return '"' + text.replaceAll('"', '""') + '"';
      }).join(",");
    })
  ].join("\n");

  const blob = new Blob(
    ["\ufeff" + csv],
    {
      type: "text/csv;charset=utf-8;"
    }
  );

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = selectedIndex + "_Filtered.csv";

  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);

  URL.revokeObjectURL(url);
}

function downloadChartHtml() {
  const chart = document.getElementById("chart");

  if (!chart.data || !chart.layout) {
    alert("다운로드할 차트가 없습니다.");
    return;
  }

  const html = `
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
</head>
<body>
<div id="chart" style="width:100%;height:95vh;"></div>
<script>
const data = ${JSON.stringify(chart.data)};
const layout = ${JSON.stringify(chart.layout)};
Plotly.newPlot("chart", data, layout, {responsive:true});
</script>
</body>
</html>`;

  const blob = new Blob(
    [html],
    {
      type: "text/html;charset=utf-8;"
    }
  );

  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");

  anchor.href = url;
  anchor.download = selectedIndex + "_Chart.html";

  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);

  URL.revokeObjectURL(url);
}

function downloadChartPng() {
  const chart = document.getElementById("chart");

  if (!chart.data) {
    alert("다운로드할 차트가 없습니다.");
    return;
  }

  Plotly.downloadImage(chart, {
    format: "png",
    width: 1600,
    height: 900,
    filename: selectedIndex + "_Chart"
  });
}

async function initializeDashboard() {
  try {
    const response = await fetch(
      "./shipping_index_data.json",
      {
        cache: "no-store"
      }
    );

    if (!response.ok) {
      throw new Error(
        "shipping_index_data.json을 읽을 수 없습니다."
      );
    }

    dashboardData = await response.json();

    document.getElementById("generatedAt").textContent =
      dashboardData.generated_at || "-";

    populateRegions(true);
    setDateBounds();

    document.getElementById("loading").style.display =
      "none";

    document.getElementById("dashboard").style.display =
      "block";

    renderDashboard();

  } catch (error) {
    const loading = document.getElementById("loading");

    loading.innerHTML = "";

    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent =
      "데이터 로드에 실패했습니다. "
      + "GitHub Actions 실행 및 배포 파일을 확인하세요.";

    loading.appendChild(empty);

    console.error(error);
  }
}

window.addEventListener(
  "DOMContentLoaded",
  initializeDashboard
);
</script>
</body>
</html>
"""

def generate_static_html():
    """GitHub Pages용 index.html을 생성한다."""
    atomic_write_text(
        HTML_PATH,
        STATIC_HTML,
    )

    log(f"[STATIC] HTML 생성: {HTML_PATH.name}")

# =========================================================
# 8. Execution Modes
# =========================================================
def has_any_stored_data():
    """SCFI 또는 BDI 중 하나라도 기존 DB 데이터가 있는지 확인한다."""
    return any(
        get_total_row_count(index_name) > 0
        for index_name in SUPPORTED_INDEXES
    )

def run_cron_job(args):
    """
    GitHub Actions 및 로컬 실행 공통 모드.

    SCFI와 BDI를 순차 수집하고,
    누적 DB와 정적 JSON/HTML 파일을 갱신한다.
    """
    log("=" * 60)
    log("[CRON] Shipping Index Dashboard 갱신 시작")
    log("=" * 60)

    cleanup_invalid_regions()

    results = []

    for index_name in SUPPORTED_INDEXES:
        result = run_incremental_sync(
            index_name=index_name,
            user_id=args.user,
            password=args.password,
            driver_path=args.driver_path,
            headless=not args.show_browser,
            max_pages=args.max_pages,
        )

        results.append(result)

    # 일부 지수 수집이 실패해도 기존 DB 데이터로
    # GitHub Pages 정적 파일을 생성한다.
    build_static_json()
    generate_static_html()

    failed_results = [
        result
        for result in results
        if not result.get("ok")
    ]

    if failed_results:
        errors = " | ".join(
            f"{result['index_name']}: "
            f"{result.get('error', '')}"
            for result in failed_results
        )

        log(f"[WARN] 일부 지수 수집 실패: {errors}")

    log("[CRON] Shipping Index Dashboard 갱신 완료")
    log("=" * 60)

    return results

def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "SCFI / BDI Crawler + "
            "GitHub Pages Dashboard"
        )
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help=(
            "GitHub Actions용 실행 모드. "
            "SCFI/BDI 수집 후 정적 파일을 생성하고 종료"
        ),
    )

    parser.add_argument(
        "--local",
        action="store_true",
        help=(
            "로컬에서 크롤링 및 정적 파일 생성만 수행"
        ),
    )

    parser.add_argument(
        "--user",
        default=os.getenv("KSG_ID", ""),
        help="KSG 로그인 ID. 기본값: 환경변수 KSG_ID",
    )

    parser.add_argument(
        "--password",
        default=os.getenv("KSG_PW", ""),
        help="KSG 로그인 비밀번호. 기본값: 환경변수 KSG_PW",
    )

    parser.add_argument(
        "--driver-path",
        default=os.getenv("CHROMEDRIVER_PATH", ""),
        help="ChromeDriver 경로",
    )

    parser.add_argument(
        "--max-pages",
        type=int,
        default=2000,
        help="각 지수별 최대 페이지 탐색 수",
    )

    parser.add_argument(
        "--show-browser",
        action="store_true",
        help=(
            "로컬 디버깅용 Chrome 브라우저 표시. "
            "기본값은 Headless 실행"
        ),
    )

    return parser.parse_args()

def main():
    args = parse_args()

    if not args.cron and not args.local:
        args.local = True

    activate_file_logging()
    get_db_conn().close()

    if not args.user or not args.password:
        raise RuntimeError(
            "KSG 로그인 정보가 없습니다. "
            "환경변수 KSG_ID 및 KSG_PW를 설정하거나 "
            "--user 및 --password 옵션을 사용하세요."
        )

    results = run_cron_job(args)

    all_failed = all(
        not result.get("ok")
        for result in results
    )

    # 기존 데이터도 없고 모든 수집이 실패한 경우에만 실패 종료한다.
    if all_failed and not has_any_stored_data():
        log(
            "[ERROR] 모든 수집이 실패했고 "
            "기존 DB 데이터도 없습니다."
        )
        sys.exit(1)

    sys.exit(0)

if __name__ == "__main__":
    main()
