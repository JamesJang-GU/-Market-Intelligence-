# -*- coding: utf-8 -*-
import sys
import subprocess
import warnings
import re
import sqlite3
warnings.filterwarnings("ignore")

REQUIRED_PACKAGES = {
    'flask': 'flask',
    'pandas': 'pandas',
    'plotly': 'plotly',
    'selenium': 'selenium',
    'lxml': 'lxml',
    'html5lib': 'html5lib',
    'bs4': 'beautifulsoup4'
}

print("[시스템] 필수 라이브러리 검사 중...")
for module_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        __import__(module_name)
    except ImportError:
        print(f"[알림] '{pip_name}' 라이브러리가 없습니다. 자동 설치를 시작합니다...")
        try:
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
            except Exception:
                # uv/PEP668 externally-managed Python 환경에서는 일반 pip 설치가 막힐 수 있어 fallback 처리
                subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"])
            print(f"[완료] '{pip_name}' 설치 성공!")
        except Exception as e:
            print(f"[오류] '{pip_name}' 설치 실패: {e}")
            print("[안내] 권장 해결 방법: 가상환경을 만든 뒤 필요한 라이브러리를 설치하고 실행하세요.")
            print("       py -3.12 -m venv .venv")
            print(r"       .\.venv\Scripts\activate")
            print("       python -m pip install flask pandas plotly selenium lxml html5lib beautifulsoup4")
            sys.exit(1)

from flask import Flask, render_template_string, request, Response
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import pandas as pd
import plotly.graph_objects as go
import socket
import threading
import time
import webbrowser
from pathlib import Path
from datetime import datetime
import urllib.parse

app = Flask(__name__)

# =========================
# 기본 설정
# =========================
TAC_BLUE = "#3498db"
TAC_BLUE_DARK = "#1f3447"
TAC_GREEN = "#27ae60"
TAC_GRID = "#d5dde7"
TAC_TEXT = "#243746"
TAC_RED = "#c0392b"
TAC_PURPLE = "#9b59b6"
TAC_ORANGE = "#f39c12"

BASE_SOURCE_URL = "https://shipandbunker.com/prices"
ROTTERDAM_URL = "https://shipandbunker.com/prices/emea/nwe/nl-rtm-rotterdam#_VLSFO"
FUJAIRAH_URL = "https://shipandbunker.com/prices/emea/me/ae-fjr-fujairah#_VLSFO"
SINGAPORE_URL = "https://shipandbunker.com/prices/apac/sea/sg-sin-singapore#_VLSFO"

URLS = [ROTTERDAM_URL, FUJAIRAH_URL, SINGAPORE_URL]
CSV_FILENAME = "shipandbunker_vlsfo_all.csv"  # 구버전 호환용 이름. v10부터 자동 저장은 DB 사용.
ALL_PORTS = ["Rotterdam", "Fujairah", "Singapore"]

DB_FILENAME = "energy_market_data.db"
PRODUCT_TABLE = "product_vlsfo"
FORECAST_TABLE = "bunker_forecast"
OIL_TABLE = "oil_prices"

DATA_CACHE = {"df": None, "fetched_at": None}

FORECAST_URLS = {
    "Rotterdam": ROTTERDAM_URL,
    "Fujairah": FUJAIRAH_URL,
    "Singapore": SINGAPORE_URL,
}
FORECAST_CSV_FILENAME = "bunker_forecast_all_ports.csv"
FORECAST_CACHE = {"df": None, "fetched_at": None, "message": ""}
FORECAST_PRODUCT_COLUMNS = ["VLSFO", "MGO", "LSMGO", "HSFO"]



# =========================
# SQLite DB 저장/로드 유틸
# =========================
def get_db_path():
    return Path(__file__).resolve().parent / DB_FILENAME


def get_db_conn():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_meta (
            dataset TEXT PRIMARY KEY,
            fetched_at TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn


def db_table_exists(table_name):
    conn = get_db_conn()
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


def set_dataset_meta(dataset, fetched_at=None):
    fetched_at = fetched_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_conn()
    try:
        conn.execute("""
            INSERT INTO data_meta(dataset, fetched_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dataset) DO UPDATE SET
                fetched_at=excluded.fetched_at,
                updated_at=excluded.updated_at
        """, (dataset, fetched_at, updated_at))
        conn.commit()
    finally:
        conn.close()


def get_dataset_meta(dataset):
    conn = get_db_conn()
    try:
        row = conn.execute(
            "SELECT fetched_at FROM data_meta WHERE dataset=?",
            (dataset,)
        ).fetchone()
        return row[0] if row and row[0] else "-"
    finally:
        conn.close()


def save_dataframe_to_db(df, table_name, dataset_name):
    if df is None or df.empty:
        return "-"

    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_conn()
    try:
        out.to_sql(table_name, conn, if_exists="replace", index=False)
        conn.commit()
    finally:
        conn.close()

    set_dataset_meta(dataset_name, fetched_at)
    print(f"[INFO] DB 저장 완료: {get_db_path()} / table={table_name} / rows={len(out)}")
    return fetched_at


def load_dataframe_from_db(table_name):
    if not db_table_exists(table_name):
        return pd.DataFrame()
    conn = get_db_conn()
    try:
        return pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    finally:
        conn.close()


# =========================
# Selenium 크롤링
# =========================
def make_driver(headless=True):
    options = Options()

    # 크롤링용 Chrome 창을 화면에 띄우지 않는 headless 모드
    # Ship & Bunker의 Highcharts 데이터 추출도 headless 모드에서 동작하도록 충분한 창 크기를 지정한다.
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1600,1000")
    else:
        options.add_argument("--start-maximized")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    return webdriver.Chrome(options=options)


def try_click_all_for_vlsfo(driver):
    return driver.execute_script("""
        if (typeof Highcharts === 'undefined' || !Highcharts.charts) return {ok:false};

        for (let i = 0; i < Highcharts.charts.length; i++) {
            const chart = Highcharts.charts[i];
            if (!chart || !chart.series || chart.series.length === 0) continue;

            const hasVLSFO = chart.series.some(s => s && s.name && s.name.includes('VLSFO'));
            if (!hasVLSFO) continue;

            if (!chart.rangeSelector || !chart.rangeSelector.buttons || chart.rangeSelector.buttons.length === 0) {
                return {ok:false};
            }

            const lastIndex = chart.rangeSelector.buttons.length - 1;
            try {
                chart.rangeSelector.clickButton(lastIndex, true);
                return {ok:true, chartIndex:i};
            } catch(e) {
                return {ok:false};
            }
        }
        return {ok:false};
    """)


def extract_vlsfo_full_data(driver):
    return driver.execute_script("""
        if (typeof Highcharts === 'undefined' || !Highcharts.charts) return [];

        function normalizePoint(pt) {
            if (Array.isArray(pt) && pt.length >= 2) return { x: pt[0], y: pt[1] };
            if (pt && typeof pt === 'object' && pt.x !== undefined && pt.y !== undefined) return { x: pt.x, y: pt.y };
            return null;
        }

        let results = [];

        Highcharts.charts.forEach((chart, chartIndex) => {
            if (!chart || !chart.series || chart.series.length === 0) return;

            chart.series.forEach((series, seriesIndex) => {
                if (!series || !series.name) return;
                if (!series.name.includes('VLSFO')) return;

                let rawPoints = [];
                let sourceType = 'none';

                if (series.options && Array.isArray(series.options.data) && series.options.data.length > 0) {
                    rawPoints = series.options.data;
                    sourceType = 'series.options.data';
                } else if (series.userOptions && Array.isArray(series.userOptions.data) && series.userOptions.data.length > 0) {
                    rawPoints = series.userOptions.data;
                    sourceType = 'series.userOptions.data';
                } else if (Array.isArray(series.data) && series.data.length > 0) {
                    rawPoints = series.data;
                    sourceType = 'series.data';
                }

                const points = rawPoints
                    .map(normalizePoint)
                    .filter(p => p && p.x !== undefined && p.y !== undefined && p.y !== null);

                results.push({
                    chartIndex: chartIndex,
                    seriesIndex: seriesIndex,
                    seriesName: series.name,
                    sourceType: sourceType,
                    pointCount: points.length,
                    data: points
                });
            });
        });

        return results;
    """)




def clean_forecast_number(value):
    s = str(value).strip()
    if not s or s.lower() == "nan" or s == "-":
        return None
    s = s.replace(",", "")
    s = re.sub(r"[^0-9.+-]", "", s)
    if not s or s in ["+", "-", "."]:
        return None
    try:
        return float(s)
    except Exception:
        return None


def forecast_period_to_date(period):
    """2026 Q2 같은 분기 forecast를 그래프용 분기말 날짜로 변환한다.
    Full Year 행은 표에는 표시하지만 그래프에는 넣지 않는다.
    """
    s = str(period or "").strip()
    m = re.search(r"(20\d{2})\s*Q([1-4])", s, flags=re.I)
    if not m:
        return pd.NaT
    year = int(m.group(1))
    q = int(m.group(2))
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    return pd.Timestamp(year=year, month=month_day[0], day=month_day[1])


def normalize_forecast_table(raw_df):
    """Ship & Bunker forecast 표 후보를 표준 컬럼으로 정리한다.
    pandas.read_html 결과에서 컬럼 중복/비정상 헤더가 생겨도 동작하도록 위치 기반으로 처리한다.
    """
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()

    df = raw_df.copy()

    # MultiIndex 컬럼 평탄화
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join([str(x) for x in c if str(x) != "nan"]).strip() for c in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]

    # 완전 빈 행/열 제거
    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return pd.DataFrame()

    col_text = " ".join(map(str, df.columns)).upper()
    first_row_text = " ".join(map(str, df.iloc[0].tolist())).upper() if len(df) else ""

    # 헤더가 첫 행에 들어간 경우 첫 행을 컬럼으로 승격
    if not any(c in col_text for c in FORECAST_PRODUCT_COLUMNS) and any(c in first_row_text for c in FORECAST_PRODUCT_COLUMNS):
        df.columns = [str(x).strip() for x in df.iloc[0].tolist()]
        df = df.iloc[1:].reset_index(drop=True)
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            return pd.DataFrame()

    # 컬럼명 중복 방지
    seen = {}
    unique_cols = []
    for c in df.columns:
        base = str(c).strip() or "COL"
        n = seen.get(base, 0)
        unique_cols.append(base if n == 0 else f"{base}_{n}")
        seen[base] = n + 1
    df.columns = unique_cols

    # 제품 컬럼 위치 찾기
    product_positions = {}
    for idx, c in enumerate(df.columns):
        u = str(c).strip().upper()
        for prod in FORECAST_PRODUCT_COLUMNS:
            if u == prod or prod in u.split():
                product_positions[prod] = idx

    # 컬럼명으로 못 찾으면 첫 행/전체 텍스트 기준으로 한 번 더 시도
    if not product_positions:
        for col_idx in range(df.shape[1]):
            sample_text = " ".join(df.iloc[:3, col_idx].astype(str).tolist()).upper()
            for prod in FORECAST_PRODUCT_COLUMNS:
                if prod in sample_text:
                    product_positions[prod] = col_idx

    if not product_positions:
        return pd.DataFrame()

    first_product_pos = min(product_positions.values())
    period_pos = max(0, first_product_pos - 1)

    # 기간 컬럼이 첫 제품 컬럼 바로 앞이 아닌 경우, 기간처럼 보이는 컬럼을 탐색
    for col_idx in range(df.shape[1]):
        if col_idx in product_positions.values():
            continue
        sample = df.iloc[:, col_idx].astype(str)
        if sample.str.contains(r"20\d{2}|\d{2}\s*Q[1-4]|Q[1-4]|Full Year", case=False, regex=True, na=False).any():
            period_pos = col_idx
            break

    out = pd.DataFrame()
    out["Period"] = df.iloc[:, period_pos].astype(str).str.strip()

    for prod in FORECAST_PRODUCT_COLUMNS:
        pos = product_positions.get(prod)
        if pos is None or pos >= df.shape[1]:
            out[prod] = None
        else:
            out[prod] = df.iloc[:, pos].apply(clean_forecast_number)

    out = out[out["Period"].astype(str).str.contains(r"20\d{2}|Q[1-4]|Full Year", case=False, regex=True, na=False)]
    out = out.dropna(subset=FORECAST_PRODUCT_COLUMNS, how="all")
    if out.empty:
        return pd.DataFrame()

    out["graph_date"] = out["Period"].apply(forecast_period_to_date)
    out["source_url"] = ""
    return out[["Period"] + FORECAST_PRODUCT_COLUMNS + ["graph_date", "source_url"]].reset_index(drop=True)

def parse_bunker_forecast_from_html(html, port_name):
    """Ship & Bunker 각 항구 Bunker Price Forecast 표를 HTML에서 직접 추출한다.
    사이트 구조가 바뀔 수 있어 BeautifulSoup 방식과 pandas.read_html 방식을 모두 시도한다.
    """
    from io import StringIO
    errors = []

    # 1) pandas로 전체 테이블 중 forecast 표 후보 선택
    try:
        tables = pd.read_html(StringIO(html))
        best = pd.DataFrame()
        best_score = -1
        for table in tables:
            normalized = normalize_forecast_table(table)
            if normalized.empty:
                continue
            text = " ".join(map(str, table.columns)) + " " + " ".join(map(str, table.head(3).values.ravel()))
            score = 0
            for key in FORECAST_PRODUCT_COLUMNS:
                if key in text.upper():
                    score += 5
            if normalized["Period"].astype(str).str.contains("Full Year", case=False, na=False).any():
                score += 3
            if normalized["graph_date"].notna().any():
                score += 3
            if score > best_score:
                best_score = score
                best = normalized
        if not best.empty:
            best["port"] = port_name
            return best
    except Exception as e:
        errors.append(f"pandas: {e}")

    # 2) BeautifulSoup로 제목 주변 table 직접 탐색
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        heading_patterns = [
            rf"{re.escape(port_name)}\s+Bunker\s+Price\s+Forecast",
            r"Bunker\s+Price\s+Forecast",
        ]
        heading = None
        for pat in heading_patterns:
            heading = soup.find(string=re.compile(pat, re.I))
            if heading:
                break
        search_root = heading.parent if heading else soup
        table = None
        cur = search_root
        for _ in range(10):
            if cur is None:
                break
            table = cur.find_next("table")
            if table:
                break
            cur = cur.parent
        if table is None:
            table = soup.find("table")
        if table:
            rows = []
            for tr in table.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(cells)
            if rows:
                max_len = max(len(r) for r in rows)
                rows = [r + [""] * (max_len - len(r)) for r in rows]
                raw = pd.DataFrame(rows)
                normalized = normalize_forecast_table(raw)
                if not normalized.empty:
                    normalized["port"] = port_name
                    return normalized
    except Exception as e:
        errors.append(f"bs4: {e}")

    raise RuntimeError(f"{port_name} Bunker Price Forecast 표를 찾지 못했습니다. " + " | ".join(errors))


def collect_all_forecast_data(existing_driver=None):
    owns_driver = existing_driver is None
    driver = existing_driver or make_driver()
    frames = []
    errors = []

    try:
        for port_name, url in FORECAST_URLS.items():
            try:
                print(f"[INFO] Forecast 접속: {port_name} - {url}")
                driver.get(url)
                time.sleep(5)
                html = driver.page_source
                forecast_df = parse_bunker_forecast_from_html(html, port_name=port_name)
                forecast_df["port"] = port_name
                forecast_df["source_url"] = url
                frames.append(forecast_df)
            except Exception as e:
                msg = f"{port_name}: {e}"
                print(f"[WARN] Forecast 수집 실패 - {msg}")
                errors.append(msg)
    finally:
        if owns_driver:
            try:
                driver.quit()
            except Exception:
                pass

    if frames:
        out = pd.concat(frames, ignore_index=True, sort=False)
    else:
        raise RuntimeError("모든 항구 Forecast 수집 실패: " + " | ".join(errors))

    save_dataframe_to_db(out, FORECAST_TABLE, FORECAST_TABLE)
    if errors:
        print("[WARN] 일부 Forecast 수집 실패: " + " | ".join(errors))
    return out, " | ".join(errors)


def load_or_collect_forecast_data(force_refresh=False):
    if (not force_refresh) and FORECAST_CACHE["df"] is not None:
        return FORECAST_CACHE["df"].copy(), FORECAST_CACHE["fetched_at"], FORECAST_CACHE.get("message", "")

    if not force_refresh:
        try:
            df = load_dataframe_from_db(FORECAST_TABLE)
            if not df.empty:
                for c in FORECAST_PRODUCT_COLUMNS:
                    if c in df.columns:
                        df[c] = pd.to_numeric(df[c], errors="coerce")
                if "graph_date" in df.columns:
                    df["graph_date"] = pd.to_datetime(df["graph_date"], errors="coerce")
                if "port" not in df.columns:
                    df["port"] = "Rotterdam"
                fetched_at = get_dataset_meta(FORECAST_TABLE)
                FORECAST_CACHE["df"] = df.copy()
                FORECAST_CACHE["fetched_at"] = fetched_at
                FORECAST_CACHE["message"] = ""
                print("[INFO] 기존 DB 로드 완료: bunker_forecast")
                return df, fetched_at, ""
        except Exception as e:
            print(f"[WARN] 기존 DB 로드 실패(bunker_forecast): {e}")

    try:
        df, partial_message = collect_all_forecast_data()
        fetched_at = get_dataset_meta(FORECAST_TABLE)
        if fetched_at == "-":
            fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message = f"일부 Forecast 수집 실패: {partial_message}" if partial_message else ""
        FORECAST_CACHE["df"] = df.copy()
        FORECAST_CACHE["fetched_at"] = fetched_at
        FORECAST_CACHE["message"] = message
        return df, fetched_at, message
    except Exception as e:
        msg = f"Forecast 데이터 수집 실패: {e}"
        print(f"[WARN] {msg}")
        empty = pd.DataFrame(columns=["port", "Period"] + FORECAST_PRODUCT_COLUMNS + ["graph_date", "source_url"])
        FORECAST_CACHE["df"] = empty.copy()
        FORECAST_CACHE["fetched_at"] = "-"
        FORECAST_CACHE["message"] = msg
        return empty, "-", msg

def add_forecast_to_chart_df(df, forecast_df, selected_ports=None, start_date=None, end_date=None):
    """각 항구 VLSFO forecast를 차트 데이터에 추가한다.
    Full Year 행은 표 전용이므로 graph_date가 있는 분기 행만 차트에 반영한다.
    """
    if forecast_df is None or forecast_df.empty:
        return df
    if "VLSFO" not in forecast_df.columns:
        return df

    fc = forecast_df.copy()
    if "port" not in fc.columns:
        fc["port"] = "Rotterdam"
    fc["graph_date"] = pd.to_datetime(fc["graph_date"], errors="coerce")
    fc = fc.dropna(subset=["graph_date", "VLSFO", "port"])
    if selected_ports:
        fc = fc[fc["port"].isin(selected_ports)]
    if fc.empty:
        return df

    if start_date:
        fc = fc[fc["graph_date"] >= pd.to_datetime(start_date)]
    if end_date:
        fc = fc[fc["graph_date"] <= pd.to_datetime(end_date)]
    if fc.empty:
        return df

    rows = pd.DataFrame({
        "port": fc["port"].astype(str),
        "series_name": fc["port"].astype(str) + " VLSFO Forecast",
        "source_type": "forecast",
        "timestamp_ms": (fc["graph_date"].astype("int64") // 10**6),
        "price": fc["VLSFO"].astype(float),
        "source_url": fc.get("source_url", ""),
        "date": fc["graph_date"],
        "date_str": fc["graph_date"].dt.strftime("%Y-%m-%d"),
        "is_forecast": True,
    })

    base = df.copy()
    if "is_forecast" not in base.columns:
        base["is_forecast"] = False
    combined = pd.concat([base, rows], ignore_index=True, sort=False)
    combined = combined.drop_duplicates(subset=["port", "date_str", "price"], keep="first")
    return combined.sort_values(["port", "date"]).reset_index(drop=True)


def build_single_forecast_table_html(port_name, sub_df):
    rows = []
    for _, r in sub_df.iterrows():
        period = str(r.get("Period", ""))
        is_full = "full year" in period.lower()
        row_cls = "forecast-full-year" if is_full else ""
        cells = []
        for c in FORECAST_PRODUCT_COLUMNS:
            v = r.get(c)
            cells.append("-" if pd.isna(v) else f"{float(v):,.0f}")
        rows.append(f"""
        <tr class="{row_cls}">
          <th>{period}</th>
          <td>{cells[0]}</td>
          <td>{cells[1]}</td>
          <td>{cells[2]}</td>
          <td>{cells[3]}</td>
        </tr>
        """)

    return f"""
    <div class="forecast-port-block">
      <div class="forecast-title">{port_name} Bunker Price Forecast</div>
      <div class="forecast-table-wrap">
        <table class="forecast-table">
          <thead>
            <tr>
              <th>Period</th>
              <th>VLSFO</th>
              <th>MGO</th>
              <th>LSMGO</th>
              <th>HSFO</th>
            </tr>
          </thead>
          <tbody>{''.join(rows)}</tbody>
        </table>
      </div>
    </div>
    """


def build_forecast_table_html(forecast_df, fetched_at="-", message=""):
    if forecast_df is None or forecast_df.empty:
        base_msg = message or "Forecast 데이터 없음"
        return f'<div class="empty">{base_msg}</div>'

    work = forecast_df.copy()
    if "port" not in work.columns:
        work["port"] = "Rotterdam"

    blocks = []
    for port_name in ALL_PORTS:
        sub = work[work["port"] == port_name].copy()
        if sub.empty:
            continue
        blocks.append(build_single_forecast_table_html(port_name, sub))

    if not blocks:
        return '<div class="empty">Forecast 데이터 없음</div>'

    msg_html = f'<div class="forecast-warning">{message}</div>' if message else ''
    return f"""
    <div class="forecast-box">
      <div class="forecast-desc">
        Ship &amp; Bunker 웹페이지에서 직접 수집한 항구별 bunker forecast 표입니다.
        단위는 USD/mt이며 참고용 indication입니다. Forecast loaded at: {fetched_at}
      </div>
      {msg_html}
      <div class="forecast-grid">
        {''.join(blocks)}
      </div>
    </div>
    """

def collect_all_data():
    driver = make_driver()
    all_rows = []

    try:
        for url in URLS:
            print(f"[INFO] 접속: {url}")
            driver.get(url)
            time.sleep(5)
            try_click_all_for_vlsfo(driver)
            time.sleep(3)

            extracted = extract_vlsfo_full_data(driver)
            for item in extracted:
                port = item["seriesName"].replace(" VLSFO", "").strip()
                for p in item["data"]:
                    if p is None:
                        continue
                    x = p.get("x")
                    y = p.get("y")
                    if x is None or y is None:
                        continue
                    all_rows.append({
                        "port": port,
                        "series_name": item["seriesName"],
                        "source_type": item["sourceType"],
                        "timestamp_ms": x,
                        "price": y,
                        "source_url": url,
                    })
    finally:
        driver.quit()

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    df["timestamp_ms"] = pd.to_numeric(df["timestamp_ms"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["timestamp_ms", "price"])
    df["date"] = pd.to_datetime(df["timestamp_ms"], unit="ms", errors="coerce")
    df = df.dropna(subset=["date"])
    df["date_str"] = df["date"].dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates(subset=["port", "date_str", "price"])
    df = df.sort_values(["port", "date"]).reset_index(drop=True)

    save_dataframe_to_db(df, PRODUCT_TABLE, PRODUCT_TABLE)
    return df


def load_or_collect_data(force_refresh=False):
    if (not force_refresh) and DATA_CACHE["df"] is not None:
        return DATA_CACHE["df"].copy(), DATA_CACHE["fetched_at"]

    if not force_refresh:
        try:
            df = load_dataframe_from_db(PRODUCT_TABLE)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"])
                if "price" in df.columns:
                    df["price"] = pd.to_numeric(df["price"], errors="coerce")
                if "timestamp_ms" in df.columns:
                    df["timestamp_ms"] = pd.to_numeric(df["timestamp_ms"], errors="coerce")
                fetched_at = get_dataset_meta(PRODUCT_TABLE)
                DATA_CACHE["df"] = df.copy()
                DATA_CACHE["fetched_at"] = fetched_at
                print("[INFO] 기존 DB 로드 완료: product_vlsfo")
                return df, fetched_at
        except Exception as e:
            print(f"[WARN] 기존 DB 로드 실패(product_vlsfo): {e}")

    print("[INFO] 웹에서 최신 데이터 수집 시작")
    df = collect_all_data()
    fetched_at = get_dataset_meta(PRODUCT_TABLE)
    if fetched_at == "-":
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    DATA_CACHE["df"] = df.copy()
    DATA_CACHE["fetched_at"] = fetched_at
    return df, fetched_at

# =========================
# 데이터 처리
# =========================
def filter_df(df, ports=None, start_date=None, end_date=None):
    if df.empty:
        return df.copy()

    out = df.copy()

    if ports:
        out = out[out["port"].isin(ports)]

    if start_date:
        out = out[out["date"] >= pd.to_datetime(start_date)]

    if end_date:
        out = out[out["date"] <= pd.to_datetime(end_date)]

    return out.sort_values(["port", "date"]).reset_index(drop=True)


def add_features(df, ma_mode="quarterly"):
    if df.empty:
        return df.copy()

    out = df.copy().sort_values(["port", "date"])

    window_map = {
        "quarterly": 90,
        "semiannual": 180,
        "annual": 365,
    }

    if ma_mode in window_map:
        w = window_map[ma_mode]
        out["ma"] = out.groupby("port")["price"].transform(lambda s: s.rolling(w, min_periods=1).mean())
    else:
        out["ma"] = pd.NA

    out["daily_change"] = out.groupby("port")["price"].diff()
    return out


# =========================
# 스타일 / 차트 유틸
# =========================
def apply_style(fig, height=460):
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=20, r=20, t=20, b=20),
        paper_bgcolor="white",
        plot_bgcolor="white",
        font=dict(family="Segoe UI, sans-serif", color=TAC_TEXT, size=13),
        legend=dict(
            orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
            bgcolor="rgba(255,255,255,0.88)", bordercolor=TAC_GRID, borderwidth=1
        ),
        xaxis=dict(showgrid=True, gridcolor="#edf2f7", linecolor=TAC_GRID, showline=True),
        yaxis=dict(showgrid=True, gridcolor="#edf2f7", linecolor=TAC_GRID, showline=True),
        hoverlabel=dict(bgcolor="white", bordercolor=TAC_GRID)
    )
    return fig


def port_color(port):
    return {
        "Rotterdam": TAC_BLUE,
        "Fujairah": TAC_GREEN,
        "Singapore": TAC_PURPLE
    }.get(port, TAC_ORANGE)


# =========================
# 차트
# =========================


def split_past_future_for_plot(sub, date_col="date", value_col="price"):
    """오늘 이후 데이터는 점선으로 표시하기 위해 과거/미래 구간으로 분리한다.
    미래 구간은 선이 끊기지 않도록 마지막 과거 포인트를 한 번 포함한다.
    """
    if sub is None or sub.empty:
        return sub, sub

    work = sub.copy().sort_values(date_col)
    work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
    work = work.dropna(subset=[date_col])

    today = pd.Timestamp.today().normalize()
    past = work[work[date_col] <= today].copy()
    future = work[work[date_col] > today].copy()

    if not past.empty and not future.empty:
        future = pd.concat([past.tail(1), future], ignore_index=True)

    return past, future


def add_solid_future_dashed_trace(fig, sub, x_col, y_col, name, color, width=2.6, mode="lines+markers", marker_size=5, showlegend=True, hovertemplate=None):
    """같은 시계열을 오늘 기준으로 실선/점선 trace로 나누어 추가한다."""
    past, future = split_past_future_for_plot(sub, date_col=x_col, value_col=y_col)

    if past is not None and not past.empty:
        fig.add_trace(go.Scatter(
            x=past[x_col],
            y=past[y_col],
            mode=mode,
            name=name,
            line=dict(color=color, width=width),
            marker=dict(size=marker_size) if "markers" in mode else None,
            hovertemplate=hovertemplate,
            showlegend=showlegend
        ))

    if future is not None and not future.empty:
        fig.add_trace(go.Scatter(
            x=future[x_col],
            y=future[y_col],
            mode=mode,
            name=f"{name} Forecast/Future",
            line=dict(color=color, width=width, dash="dot"),
            marker=dict(size=marker_size, symbol="circle-open") if "markers" in mode else None,
            hovertemplate=hovertemplate,
            showlegend=showlegend
        ))

def build_line_chart(df, ma_mode="quarterly"):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'

    fig = go.Figure()

    for port in sorted(df["port"].unique()):
        sub = df[df["port"] == port].sort_values("date")
        color = port_color(port)

        add_solid_future_dashed_trace(
            fig,
            sub,
            x_col="date",
            y_col="price",
            name=f"{port} Price",
            color=color,
            width=2.6,
            mode="lines+markers",
            marker_size=5
        )

        if ma_mode != "off" and "ma" in sub.columns:
            ma_name = {
                "quarterly": "Quarterly MA",
                "semiannual": "Semi-Annual MA",
                "annual": "Annual MA"
            }.get(ma_mode, "MA")

            ma_sub = sub.dropna(subset=["ma"]).copy()
            add_solid_future_dashed_trace(
                fig,
                ma_sub,
                x_col="date",
                y_col="ma",
                name=f"{port} {ma_name}",
                color=color,
                width=2,
                mode="lines",
                marker_size=0
            )

    today_line = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    fig.add_shape(
        type="line",
        x0=today_line,
        x1=today_line,
        y0=0,
        y1=1,
        xref="x",
        yref="paper",
        line=dict(color="#64748b", width=1.5, dash="dash")
    )
    fig.add_annotation(
        x=today_line,
        y=1,
        xref="x",
        yref="paper",
        text="Today",
        showarrow=False,
        xanchor="left",
        yanchor="bottom",
        font=dict(size=11, color="#64748b")
    )

    fig.update_layout(
        xaxis_title="Date",
        yaxis_title="USD/MT",
        xaxis=dict(rangeslider=dict(visible=True), type="date")
    )
    apply_style(fig, height=520)
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def build_latest_bar_chart(df):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'

    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].copy().sort_values("price", ascending=False)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=latest_df["port"],
        y=latest_df["price"],
        text=[f"{v:.1f}" for v in latest_df["price"]],
        textposition="outside",
        marker_color=[port_color(p) for p in latest_df["port"]],
        marker_line=dict(color="white", width=1),
        showlegend=False
    ))
    fig.update_layout(xaxis_title="Port", yaxis_title="USD/MT")
    apply_style(fig, height=420)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_spread_chart(df):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'

    pivot_df = df.pivot_table(index="date", columns="port", values="price", aggfunc="mean").reset_index()
    available = [c for c in ["Rotterdam", "Fujairah", "Singapore"] if c in pivot_df.columns]
    if len(available) < 2:
        return '<div class="empty">스프레드 계산 불가</div>'

    fig = go.Figure()

    if "Rotterdam" in pivot_df.columns and "Singapore" in pivot_df.columns:
        fig.add_trace(go.Scatter(
            x=pivot_df["date"],
            y=pivot_df["Rotterdam"] - pivot_df["Singapore"],
            mode="lines+markers",
            name="Rotterdam - Singapore",
            line=dict(color=TAC_BLUE, width=3),
            marker=dict(size=5)
        ))

    if "Fujairah" in pivot_df.columns and "Singapore" in pivot_df.columns:
        fig.add_trace(go.Scatter(
            x=pivot_df["date"],
            y=pivot_df["Fujairah"] - pivot_df["Singapore"],
            mode="lines+markers",
            name="Fujairah - Singapore",
            line=dict(color=TAC_RED, width=3),
            marker=dict(size=5)
        ))

    if "Rotterdam" in pivot_df.columns and "Fujairah" in pivot_df.columns:
        fig.add_trace(go.Scatter(
            x=pivot_df["date"],
            y=pivot_df["Rotterdam"] - pivot_df["Fujairah"],
            mode="lines+markers",
            name="Rotterdam - Fujairah",
            line=dict(color=TAC_PURPLE, width=3),
            marker=dict(size=5)
        ))

    fig.add_hline(y=0, line_dash="dash", line_color="#95a5a6")
    fig.update_layout(xaxis_title="Date", yaxis_title="Spread")
    apply_style(fig, height=430)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def build_daily_change_chart(df):
    if df.empty or "daily_change" not in df.columns:
        return '<div class="empty">데이터 없음</div>'

    cdf = df.dropna(subset=["daily_change"]).copy()
    if cdf.empty:
        return '<div class="empty">일간 변화량 데이터 부족</div>'

    fig = go.Figure()

    for port in sorted(cdf["port"].unique()):
        sub = cdf[cdf["port"] == port].sort_values("date")
        fig.add_trace(go.Scatter(
            x=sub["date"],
            y=sub["daily_change"],
            mode="lines+markers",
            name=port,
            line=dict(color=port_color(port), width=2.5),
            marker=dict(size=5)
        ))

    fig.add_hline(y=0, line_dash="dash", line_color="#7f8c8d")
    fig.update_layout(xaxis_title="Date", yaxis_title="Daily Change")
    apply_style(fig, height=430)
    return fig.to_html(full_html=False, include_plotlyjs=False)


# =========================
# KPI / 인사이트
# =========================
def build_kpis(df):
    if df.empty:
        return {"latest_date": "-", "avg_price": "-", "row_count": "0", "port_values": {}}

    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].copy()

    port_values = {}
    for port in sorted(df["port"].dropna().unique()):
        sub = latest_df[latest_df["port"] == port]
        port_values[port] = f"{sub['price'].iloc[0]:.1f}" if not sub.empty else "-"

    return {
        "latest_date": latest_date.strftime("%Y-%m-%d"),
        "avg_price": f"{df['price'].mean():.1f}",
        "row_count": f"{len(df):,}",
        "port_values": port_values
    }


def build_insights(df):
    if df.empty:
        return ["데이터 없음"]

    insights = []
    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].copy().sort_values("price", ascending=False)

    if not latest_df.empty:
        top = latest_df.iloc[0]
        insights.append(f"최신일 기준 최고가 항구: {top['port']} ({top['price']:.1f} USD/MT)")

    if len(latest_df) >= 2:
        spread = latest_df["price"].max() - latest_df["price"].min()
        insights.append(f"최신일 기준 항구 간 가격 격차: {spread:.1f} USD/MT")

    for port in sorted(df["port"].unique()):
        sub = df[df["port"] == port].sort_values("date").tail(10)
        if len(sub) >= 2:
            diff = sub["price"].iloc[-1] - sub["price"].iloc[0]
            if diff > 0:
                insights.append(f"{port}: 최근 구간 상승 (+{diff:.1f})")
            elif diff < 0:
                insights.append(f"{port}: 최근 구간 하락 ({diff:.1f})")
            else:
                insights.append(f"{port}: 최근 구간 보합")

    return insights[:6]


def format_implications_html(items):
    if not items:
        return "<p>시사점 없음</p>"
    return '<ul class="implication-list">' + ''.join(f'<li>{x}</li>' for x in items) + '</ul>'


# =========================
# 비교표 / 비교차트 / 기간 KPI
# =========================
def get_period_compare_df(df):
    if df.empty:
        return pd.DataFrame()

    rows = []

    def add_compare(period_name, freq):
        for port in sorted(df["port"].unique()):
            sub = df[df["port"] == port].copy().sort_values("date")
            if sub.empty:
                continue

            temp = sub.set_index("date").resample(freq)["price"].mean().dropna().reset_index()
            if len(temp) < 2:
                continue

            current = temp.iloc[-1]
            previous = temp.iloc[-2]
            change = current["price"] - previous["price"]
            change_pct = (change / previous["price"] * 100) if previous["price"] not in [0, None] else None

            rows.append({
                "port": port,
                "period_type": period_name,
                "current_avg": current["price"],
                "previous_avg": previous["price"],
                "change": change,
                "change_pct": change_pct
            })

    add_compare("Quarterly", "QE")
    add_compare("Semi-Annual", "2QE")
    add_compare("Annual", "YE")

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def build_period_compare_table(df):
    comp = get_period_compare_df(df)
    if comp.empty:
        return '<div class="empty">비교 데이터 없음</div>'

    def trend_badge(v):
        if pd.isna(v):
            return '<span class="trend-flat">N/A</span>'
        if v > 0:
            return '<span class="trend-up">상승</span>'
        if v < 0:
            return '<span class="trend-down">하락</span>'
        return '<span class="trend-flat">보합</span>'

    def value_class(v):
        if pd.isna(v):
            return "neutral-cell"
        if v > 0:
            return "up-cell"
        if v < 0:
            return "down-cell"
        return "neutral-cell"

    html = """
    <div style='overflow-x:auto;'>
      <table class='data-table compare-table'>
        <thead>
          <tr>
            <th>Port</th>
            <th>Period</th>
            <th>Current Avg</th>
            <th>Previous Avg</th>
            <th>Change</th>
            <th>Change %</th>
            <th>Trend</th>
          </tr>
        </thead>
        <tbody>
    """

    for _, r in comp.iterrows():
        change_pct_text = "" if pd.isna(r["change_pct"]) else f"{r['change_pct']:+.2f}%"
        html += f"""
        <tr>
          <td>{r['port']}</td>
          <td>{r['period_type']}</td>
          <td>{r['current_avg']:.1f}</td>
          <td>{r['previous_avg']:.1f}</td>
          <td class="{value_class(r['change'])}">{r['change']:+.1f}</td>
          <td class="{value_class(r['change_pct'])}">{change_pct_text}</td>
          <td>{trend_badge(r['change'])}</td>
        </tr>
        """

    html += "</tbody></table></div>"
    return html


def build_period_compare_chart(df):
    comp = get_period_compare_df(df)
    if comp.empty:
        return '<div class="empty">비교 차트 데이터 없음</div>'

    fig = go.Figure()

    color_map = {
        "Quarterly": "#3498db",
        "Semi-Annual": "#27ae60",
        "Annual": "#9b59b6"
    }

    for period in ["Quarterly", "Semi-Annual", "Annual"]:
        sub = comp[comp["period_type"] == period].copy()
        if sub.empty:
            continue

        fig.add_trace(go.Bar(
            x=sub["port"],
            y=sub["change_pct"],
            name=period,
            text=[("" if pd.isna(v) else f"{v:+.2f}%") for v in sub["change_pct"]],
            textposition="outside",
            marker_color=color_map.get(period, "#3498db"),
            marker_line=dict(color="white", width=1),
            opacity=0.9
        ))

    fig.add_hline(y=0, line_dash="dash", line_color="#7f8c8d")
    fig.update_layout(
        barmode="group",
        xaxis_title="Port",
        yaxis_title="Change %",
    )
    apply_style(fig, height=430)
    return fig.to_html(full_html=False, include_plotlyjs=False)


def compute_period_change_kpis(df):
    if df.empty:
        return []

    result = []
    period_defs = [
        ("QoQ", "QE"),
        ("HoH", "2QE"),
        ("YoY", "YE"),
    ]

    for port in ["Fujairah", "Rotterdam", "Singapore"]:
        sub = df[df["port"] == port].copy().sort_values("date")

        item = {"port": port, "metrics": []}

        if sub.empty:
            for label, _ in period_defs:
                item["metrics"].append({
                    "label": label,
                    "value": "N/A",
                    "class_name": "neutral"
                })
            result.append(item)
            continue

        for label, freq in period_defs:
            temp = sub.set_index("date").resample(freq)["price"].mean().dropna().reset_index()

            if len(temp) < 2:
                item["metrics"].append({
                    "label": label,
                    "value": "N/A",
                    "class_name": "neutral"
                })
                continue

            current = temp.iloc[-1]["price"]
            previous = temp.iloc[-2]["price"]

            if previous in [0, None] or pd.isna(previous):
                item["metrics"].append({
                    "label": label,
                    "value": "N/A",
                    "class_name": "neutral"
                })
                continue

            change_pct = (current - previous) / previous * 100

            if change_pct > 0:
                class_name = "up"
            elif change_pct < 0:
                class_name = "down"
            else:
                class_name = "neutral"

            item["metrics"].append({
                "label": label,
                "value": f"{change_pct:+.2f}%",
                "class_name": class_name
            })

        result.append(item)

    return result


# =========================
# HTML
# =========================
HTML = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ship & Bunker VLSFO Dashboard</title>
<style>
* { box-sizing: border-box; }
body {
  font-family: 'Segoe UI', sans-serif;
  margin: 0;
  padding: 0;
  min-height: 100vh;
  background: linear-gradient(180deg, #f3f6fa 0%, #edf2f7 100%);
  color: #243746;
}
a { text-decoration: none; color: inherit; }
.wrap { width: 100%; }

.header {
  border-bottom: 3px solid #3498db;
  padding-bottom: 18px;
  margin-bottom: 24px;
}
.header h1 {
  margin: 0 0 8px 0;
  color: #1f3447;
  font-size: 28px;
}
.header-meta {
  font-size: 13px;
  color: #5d6d7e;
}
.header-meta a {
  color: #3498db;
  font-weight: 700;
  text-decoration: underline;
}

.controls, .chart-card {
  background: white;
  padding: 24px;
  border-radius: 14px;
  box-shadow: 0 4px 18px rgba(0,0,0,0.05);
  margin-bottom: 24px;
}
.section-title {
  font-weight: 700;
  margin: 0 0 14px 0;
  color: #2c3e50;
  font-size: 18px;
}
.grid {
  display: grid;
  grid-template-columns: 1.1fr 1fr 1fr 1fr auto auto auto;
  gap: 16px;
  align-items: end;
}
label {
  display: block;
  font-weight: 700;
  margin-bottom: 10px;
  color: #34495e;
  font-size: 13px;
}
select, input[type="date"] {
  width: 100%;
  padding: 11px 12px;
  border: 1px solid #d5dde7;
  border-radius: 10px;
  background: white;
  color: #2c3e50;
  font-size: 14px;
  outline: none;
}
button, .btn-link {
  padding: 11px 16px;
  border: 1px solid #3498db;
  background: white;
  color: #3498db;
  border-radius: 10px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
}
button:hover, .btn-link:hover {
  background: #3498db;
  color: white;
}
.btn-green {
  border-color: #27ae60;
  color: #27ae60;
}
.btn-green:hover {
  background: #27ae60;
  color: white;
}
.btn-dark {
  border-color: #2c3e50;
  color: #2c3e50;
}
.btn-dark:hover {
  background: #2c3e50;
  color: white;
}
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(6, 1fr);
  gap: 16px;
}
.kpi {
  background: linear-gradient(180deg, #f9fbfd 0%, #ffffff 100%);
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  padding: 18px;
}
.kpi .label {
  font-size: 12px;
  font-weight: 800;
  color: #64748b;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.kpi .value {
  font-size: 24px;
  font-weight: 800;
  color: #0f172a;
}
.row2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 18px;
}
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th, .data-table td {
  border: 1px solid #e2e8f0;
  padding: 10px 12px;
  text-align: left;
}
.data-table thead th {
  background: #f8fafc;
}
.empty {
  padding: 40px;
  text-align: center;
  color: #7f8c8d;
}
.implication-list {
  margin: 0;
  padding-left: 20px;
}
.implication-list li {
  margin: 10px 0;
  line-height: 1.75;
  color: #475569;
}
.compare-table td.up-cell {
  background: #fff1f0;
  color: #c0392b;
  font-weight: 700;
}
.compare-table td.down-cell {
  background: #eefaf3;
  color: #1e8449;
  font-weight: 700;
}
.compare-table td.neutral-cell {
  background: #f8fafc;
  color: #64748b;
  font-weight: 700;
}
.trend-up, .trend-down, .trend-flat {
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
}
.trend-up {
  background: #fdecea;
  color: #c0392b;
  border: 1px solid #f5c6cb;
}
.trend-down {
  background: #eafaf1;
  color: #1e8449;
  border: 1px solid #b7e4c7;
}
.trend-flat {
  background: #f1f5f9;
  color: #64748b;
  border: 1px solid #dbe4ee;
}

/* ===== 개선된 Period Change KPI ===== */
.port-kpi-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 14px;
  width: 100%;
}
.port-kpi-card {
  border-radius: 14px;
  padding: 14px;
  border: 1px solid #e2e8f0;
  background: linear-gradient(180deg, #fbfdff 0%, #ffffff 100%);
  min-width: 0;
}
.port-kpi-title {
  font-size: 16px;
  font-weight: 800;
  color: #1f3447;
  margin-bottom: 10px;
  line-height: 1.2;
}
.port-kpi-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  min-width: 0;
}
.port-kpi-metric {
  border-radius: 10px;
  padding: 10px 8px;
  border: 1px solid #e2e8f0;
  text-align: center;
  background: #ffffff;
  min-width: 0;
}
.metric-label {
  font-size: 11px;
  font-weight: 700;
  color: #64748b;
  margin-bottom: 6px;
  line-height: 1.1;
}
.metric-value {
  font-size: 16px;
  font-weight: 800;
  line-height: 1.1;
  word-break: keep-all;
}
.port-kpi-metric.up {
  background: #fff5f5;
  border-color: #f5c2c7;
}
.port-kpi-metric.up .metric-value {
  color: #c0392b;
}
.port-kpi-metric.down {
  background: #f0fff4;
  border-color: #b7e4c7;
}
.port-kpi-metric.down .metric-value {
  color: #1e8449;
}
.port-kpi-metric.neutral {
  background: #f8fafc;
  border-color: #dbe4ee;
}
.port-kpi-metric.neutral .metric-value {
  color: #64748b;
}

.chart-note {
  margin: -4px 0 12px 0;
  color: #64748b;
  font-size: 12px;
  font-weight: 700;
}


.forecast-box { width: 100%; }

.forecast-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 22px;
}
.forecast-port-block {
  border: 1px solid #e2e8f0;
  border-radius: 12px;
  overflow: hidden;
  background: #ffffff;
}
.forecast-warning {
  margin: 10px 0 14px 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: #fff8e1;
  color: #7a5a00;
  border: 1px solid #f5d76e;
  font-size: 12px;
  font-weight: 800;
}

.forecast-title {
  background: #2b5785;
  color: #ffffff;
  font-size: 22px;
  font-weight: 900;
  padding: 8px 10px;
  margin-bottom: 14px;
}
.forecast-desc {
  color: #526b85;
  font-size: 13px;
  line-height: 1.7;
  margin-bottom: 14px;
}
.forecast-table-wrap { width: 100%; overflow-x: auto; }
.forecast-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 16px;
}
.forecast-table th,
.forecast-table td {
  padding: 8px 12px;
  text-align: center;
  border: 0;
}
.forecast-table thead th {
  color: #58708a;
  font-weight: 500;
  background: #ffffff;
}
.forecast-table tbody th {
  text-align: left;
  color: #6b7280;
  font-weight: 800;
}
.forecast-table tbody td {
  color: #000000;
  font-weight: 900;
}
.forecast-table tbody tr:nth-child(odd) { background: #f2f4f5; }
.forecast-table tbody tr:nth-child(even) { background: #dfe4e7; }
.forecast-table tbody tr.forecast-full-year { background: #d8dee0; }

.footer {
  text-align: center;
  color: #94a3b8;
  font-size: 12px;
  margin-top: 30px;
}
@media (max-width: 1200px) {
  .grid { grid-template-columns: 1fr 1fr; }
  .kpi-grid { grid-template-columns: repeat(3, 1fr); }
  .port-kpi-grid { grid-template-columns: 1fr; }
  .row2 { grid-template-columns: 1fr; }
}
@media (max-width: 768px) {
  body { padding: 16px; margin: 10px auto; }
  .grid, .kpi-grid { grid-template-columns: 1fr; }
  .port-kpi-metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .metric-value { font-size: 15px; }
  .port-kpi-title { font-size: 15px; }
}


.app-shell {
  max-width: 1680px;
  margin: 0 auto;
  padding: 24px;
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.unified-sidebar {
  position: sticky;
  top: 24px;
  background: rgba(255,255,255,0.96);
  border: 1px solid #dbe4ee;
  border-radius: 18px;
  box-shadow: 0 8px 28px rgba(15,23,42,0.08);
  padding: 20px;
}
.sidebar-brand {
  margin-bottom: 20px;
  padding-bottom: 18px;
  border-bottom: 1px solid #e5edf5;
}
.sidebar-brand-title {
  font-size: 18px;
  font-weight: 900;
  color: #1f3447;
  line-height: 1.25;
}
.sidebar-brand-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #64748b;
  line-height: 1.5;
}
.sidebar-section-title {
  font-size: 12px;
  font-weight: 900;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: .5px;
  margin: 0 0 10px 0;
}
.unified-nav {
  display: grid;
  gap: 10px;
}
.unified-nav a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 52px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 1px solid #d5dde7;
  background: #ffffff;
  color: #1f3447;
  font-weight: 800;
  text-decoration: none;
  transition: all .18s ease;
}
.unified-nav a:hover {
  background: #eff6ff;
  border-color: #93c5fd;
  color: #1d4ed8;
  transform: translateY(-1px);
}
.unified-nav a.active {
  background: #3498db;
  border-color: #3498db;
  color: #ffffff;
  box-shadow: 0 8px 18px rgba(52,152,219,.22);
}
.nav-main-text { display:block; font-size: 14px; }
.nav-sub-text { display:block; font-size: 11px; opacity: .78; margin-top: 2px; font-weight: 700; }
.nav-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  background: #edf2f7;
  color: #475569;
  font-size: 11px;
  font-weight: 900;
}
.unified-nav a.active .nav-pill {
  background: rgba(255,255,255,.22);
  color: white;
}
.sidebar-note {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid #e5edf5;
  color: #64748b;
  font-size: 12px;
  line-height: 1.6;
}
.main-panel { min-width: 0; }
@media (max-width: 1100px) {
  .app-shell { grid-template-columns: 1fr; padding: 16px; }
  .unified-sidebar { position: static; }
}

</style>
</head>
<body>
<div class="app-shell">
  <aside class="unified-sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-brand-title">Energy Market Dashboard</div>
      <div class="sidebar-brand-sub">제품유와 원유 데이터를 하나의 웹 화면에서 전환해 확인합니다.</div>
    </div>
    <div class="sidebar-section-title">Dashboards</div>
    <nav class="unified-nav">
      <a href="/product" class="active">
        <span><span class="nav-main-text">제품유 VLSFO</span><span class="nav-sub-text">Ship &amp; Bunker</span></span>
        <span class="nav-pill">제품</span>
      </a>
      <a href="/oil">
        <span><span class="nav-main-text">원유 가격</span><span class="nav-sub-text">Dubai / Brent / WTI</span></span>
        <span class="nav-pill">원유</span>
      </a>
    </nav>
    <div class="sidebar-note">각 화면의 필터와 다운로드 기능은 기존과 동일하게 사용할 수 있습니다.</div>
  </aside>
  <main class="main-panel">
<div class="wrap">
  <div class="header">
    <h1>Ship & Bunker VLSFO Dashboard</h1>
    <div class="header-meta">
      Source: <a href="{{ base_source_url }}" target="_blank" rel="noopener noreferrer">Ship &amp; Bunker</a>
      &nbsp; | &nbsp;
      Fetched at: {{ fetched_at }}
    </div>
  </div>

  <div class="chart-card">
    <div class="section-title">Market Snapshot</div>
    <div class="kpi-grid">
      <div class="kpi"><div class="label">Latest Date</div><div class="value">{{ kpis.latest_date }}</div></div>
      <div class="kpi"><div class="label">Average Price</div><div class="value">{{ kpis.avg_price }}</div></div>
      <div class="kpi"><div class="label">Rows</div><div class="value">{{ kpis.row_count }}</div></div>
      {% for port, value in kpis.port_values.items() %}
      <div class="kpi"><div class="label">{{ port }}</div><div class="value">{{ value }}</div></div>
      {% endfor %}
    </div>
  </div>

  <div class="chart-card">
    <div class="section-title">Period Change KPI</div>
    <div class="port-kpi-grid">
      {% for port_item in period_change_kpis %}
      <div class="port-kpi-card">
        <div class="port-kpi-title">{{ port_item.port }}</div>
        <div class="port-kpi-metrics">
          {% for metric in port_item.metrics %}
          <div class="port-kpi-metric {{ metric.class_name }}">
            <div class="metric-label">{{ metric.label }}</div>
            <div class="metric-value">{{ metric.value }}</div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="controls">
    <form method="get">
      <div class="grid">
        <div>
          <label>Port</label>
          <select name="ports" multiple size="5">
            {% for p in all_ports %}
              <option value="{{ p }}" {% if p in selected_ports %}selected{% endif %}>{{ p }}</option>
            {% endfor %}
          </select>
        </div>

        <div>
          <label>Moving Average</label>
          <select name="ma_mode">
            <option value="off" {% if ma_mode == 'off' %}selected{% endif %}>Off</option>
            <option value="quarterly" {% if ma_mode == 'quarterly' %}selected{% endif %}>Quarterly</option>
            <option value="semiannual" {% if ma_mode == 'semiannual' %}selected{% endif %}>Semi-Annual</option>
            <option value="annual" {% if ma_mode == 'annual' %}selected{% endif %}>Annual</option>
          </select>
        </div>

        <div>
          <label>Start Date</label>
          <input type="date" name="start_date" value="{{ start_date }}">
        </div>

        <div>
          <label>End Date</label>
          <input type="date" name="end_date" value="{{ end_date }}">
        </div>

        <div>
          <button type="submit">Apply</button>
        </div>

        <div>
          <a class="btn-link btn-green" href="{{ download_url }}">Download CSV</a>
        </div>

        <div>
          <a class="btn-link btn-dark" href="{{ refresh_url }}">Refresh Data</a>
        </div>
      </div>
    </form>
  </div>

  <div class="chart-card">
    <div class="section-title">VLSFO Price Trend</div>
    <div class="chart-note">오늘 이후 날짜의 데이터가 포함된 경우 해당 구간은 점선으로 표시됩니다. Rotterdam, Fujairah, Singapore forecast가 수집되면 각 항구 VLSFO 미래 구간에 자동 반영됩니다.</div>
    {{ line_chart|safe }}
  </div>

  <div class="chart-card">
    {{ forecast_table_html|safe }}
  </div>

  <div class="row2">
    <div class="chart-card">
      <div class="section-title">Latest Price Comparison</div>
      {{ bar_chart|safe }}
    </div>
    <div class="chart-card">
      <div class="section-title">Port Spread Trend</div>
      {{ spread_chart|safe }}
    </div>
  </div>

  <div class="chart-card">
    <div class="section-title">Daily Price Change</div>
    {{ daily_change_chart|safe }}
  </div>

  <div class="chart-card">
    <div class="section-title">분기/반기/연간 변화율 비교</div>
    {{ period_compare_chart|safe }}
  </div>

  <div class="chart-card">
    <div class="section-title">분기/반기/연간 비교표</div>
    {{ compare_table|safe }}
  </div>

  <div class="chart-card">
    <div class="section-title">시사점</div>
    {{ insights_html|safe }}
  </div>

  <div class="footer">Generated locally from Ship &amp; Bunker chart data.</div>
</div>
  </main>
</div>
</body>
</html>
"""




# ============================================================
# 원유(Opinet: Dubai / Brent / WTI) 웹 대시보드 기능
# ============================================================
OIL_SOURCE_URL = "https://www.opinet.co.kr/glopcoilSelect.do"
OIL_CSV_FILENAME = "opinet_oil_prices_web.csv"
OIL_ALL_TYPES = ["Dubai", "Brent", "WTI"]
OIL_CACHE = {"df": None, "fetched_at": None}
OIL_DEFAULT_START = "2008-01-02"


def oil_make_driver(headless=True):
    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1000")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


def oil_wait_for(driver, condition_fn, timeout=20, message="Timeout"):
    end = time.time() + timeout
    last_error = None
    while time.time() < end:
        try:
            if condition_fn(driver):
                return True
        except Exception as e:
            last_error = e
        time.sleep(0.25)
    if last_error:
        raise TimeoutException(f"{message}: {last_error}")
    raise TimeoutException(message)


def oil_wait_for_page_ready(driver, timeout=30):
    oil_wait_for(
        driver,
        lambda d: d.execute_script("return document.readyState") == "complete",
        timeout=timeout,
        message="Page was not ready."
    )


def oil_safe_find(driver, by, value, timeout=20, clickable=False, message=None):
    wait = WebDriverWait(driver, timeout)
    if clickable:
        return wait.until(EC.element_to_be_clickable((by, value)))
    return wait.until(EC.presence_of_element_located((by, value)))


def oil_set_select(driver, name, value):
    el = oil_safe_find(driver, By.NAME, name, timeout=20, message=f"select {name} not found")
    Select(el).select_by_value(str(value))


def oil_set_checkbox(driver, value):
    candidates = [
        f"//input[@type='checkbox' and @value='{value}']",
        f"//input[@value='{value}']",
    ]
    for xp in candidates:
        elems = driver.find_elements(By.XPATH, xp)
        for el in elems:
            try:
                if not el.is_selected():
                    driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                pass
    raise RuntimeError(f"Oil checkbox value '{value}' was not found.")


def oil_normalize_date(x):
    import re
    """Opinet 날짜 표기를 YYYY-MM-DD로 통일한다.
    예: 08년01월02일, 2008-01-02, 20080102, 080102
    """
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return None

    # Korean date: 08년01월02일 / 2008년01월02일
    m = re.search(r"(\d{2,4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", s)
    if m:
        y, mo, d = m.groups()
        y = int(y)
        if y < 100:
            y += 2000 if y < 70 else 1900
        try:
            return datetime(y, int(mo), int(d)).strftime("%Y-%m-%d")
        except Exception:
            return None

    cleaned = re.sub(r"[^0-9./-]", "", s)
    for fmt in [
        "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d",
        "%y-%m-%d", "%y.%m.%d", "%y/%m/%d",
        "%Y%m%d", "%y%m%d"
    ]:
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass

    dt = pd.to_datetime(cleaned, errors="coerce")
    if pd.isna(dt):
        return None
    return dt.strftime("%Y-%m-%d")


def oil_clean_num(x):
    import re
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return None
    s = s.replace(",", "")
    s = re.sub(r"[^0-9.+-]", "", s)
    if not s or s in ["+", "-", "."]:
        return None
    try:
        return float(s)
    except Exception:
        return None


def oil_choose_result_table(tables):
    best = None
    best_score = -1
    for df in tables:
        if df is None or df.empty:
            continue
        text = " ".join(map(str, list(df.columns))) + " " + " ".join(map(str, df.head(5).values.ravel()))
        score = 0
        for key in ["Dubai", "Brent", "WTI", "두바이", "브렌트"]:
            if key.lower() in text.lower():
                score += 10
        if df.shape[1] >= 4:
            score += 3
        if df.shape[0] > 5:
            score += 2
        if score > best_score:
            best_score = score
            best = df
    if best is None:
        raise RuntimeError("원유 결과 테이블을 찾지 못했습니다.")
    return best


def oil_parse_html_with_pandas(html_text_or_file):
    """pandas 버전에 따라 HTML 문자열을 파일 경로로 오인하는 문제를 방지한다."""
    from io import StringIO

    errors = []
    is_html_text = isinstance(html_text_or_file, str) and "<" in html_text_or_file[:200].lower()

    for flavor in [None, "lxml", "bs4"]:
        try:
            source = StringIO(html_text_or_file) if is_html_text else html_text_or_file
            if flavor is None:
                return pd.read_html(source)
            return pd.read_html(source, flavor=flavor)
        except Exception as e:
            errors.append(f"{flavor or 'default'}: {e}")

    raise RuntimeError("pandas.read_html failed. " + " | ".join(errors))


def oil_parse_html(html_text):
    """Opinet 원유 결과 HTML에서 달러 기준($/Bbl) 테이블을 우선 파싱한다.
    Opinet 페이지에는 원화 tbody1과 달러 tbody2가 함께 들어올 수 있어 tbody2를 우선 사용한다.
    """
    # 1) BeautifulSoup로 달러 테이블(tbody2)을 직접 파싱
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        tbody = soup.find("tbody", {"id": "tbody2"}) or soup.find("tbody", {"id": "tbody1"})
        if tbody:
            rows = []
            for tr in tbody.find_all("tr"):
                cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
                if len(cells) >= 4:
                    rows.append(cells[:4])
            if rows:
                df = pd.DataFrame(rows, columns=["DATE", "Dubai", "Brent", "WTI"])
                df["DATE"] = df["DATE"].apply(oil_normalize_date)
                for col in OIL_ALL_TYPES:
                    df[col] = df[col].apply(oil_clean_num)
                df = df.dropna(subset=["DATE"])
                df = df.dropna(subset=OIL_ALL_TYPES, how="all")
                df = df.drop_duplicates(subset=["DATE"])
                df = df.sort_values("DATE").reset_index(drop=True)
                if not df.empty:
                    return df
    except Exception as e:
        print(f"[OIL][WARN] BeautifulSoup 파싱 실패, pandas 파싱으로 재시도: {e}")

    # 2) fallback: pandas.read_html
    tables = oil_parse_html_with_pandas(html_text)
    df = oil_choose_result_table(tables)
    if df.shape[1] < 4:
        raise RuntimeError(f"원유 테이블 구조가 예상과 다릅니다: {df.shape}")
    df = df.iloc[:, :4].copy()
    df.columns = ["DATE", "Dubai", "Brent", "WTI"]
    df["DATE"] = df["DATE"].apply(oil_normalize_date)
    for col in OIL_ALL_TYPES:
        df[col] = df[col].apply(oil_clean_num)
    df = df.dropna(subset=["DATE"])
    df = df.dropna(subset=OIL_ALL_TYPES, how="all")
    df = df.drop_duplicates(subset=["DATE"])
    df = df.sort_values("DATE").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("원유 테이블 파싱 결과가 비어 있습니다.")
    return df


def oil_wait_for_results(driver):
    def has_table(d):
        try:
            html = d.page_source
            return ("Dubai" in html or "Brent" in html or "WTI" in html or "두바이" in html) and len(d.find_elements(By.TAG_NAME, "table")) > 0
        except Exception:
            return False
    oil_wait_for(driver, has_table, timeout=30, message="원유 조회 결과 테이블이 로드되지 않았습니다.")
    time.sleep(1.0)


def oil_collect_data(start_date=None, end_date=None, headless=True):
    if start_date is None:
        start_date = pd.to_datetime(OIL_DEFAULT_START)
    else:
        start_date = pd.to_datetime(start_date)
    if end_date is None:
        end_date = pd.to_datetime(datetime.today().strftime("%Y-%m-%d"))
    else:
        end_date = pd.to_datetime(end_date)
    if start_date > end_date:
        raise ValueError("원유 시작일이 종료일보다 늦습니다.")

    driver = oil_make_driver(headless=headless)
    try:
        print("[OIL] Opinet 접속 중...")
        driver.get(OIL_SOURCE_URL)
        oil_wait_for_page_ready(driver, timeout=30)

        oil_wait_for(driver, lambda d: len(d.find_elements(By.NAME, "TERM")) > 0, timeout=20, message="TERM radio not loaded")
        radios = driver.find_elements(By.NAME, "TERM")
        daily_selected = False
        for r in radios:
            if r.get_attribute("value") == "D":
                driver.execute_script("arguments[0].click();", r)
                daily_selected = True
                break
        if not daily_selected:
            raise RuntimeError("일간 조회 라디오 버튼을 찾지 못했습니다.")

        oil_set_select(driver, "STA_Y", start_date.strftime("%Y"))
        oil_set_select(driver, "STA_M", start_date.strftime("%m"))
        oil_set_select(driver, "STA_D", start_date.strftime("%d"))
        oil_set_select(driver, "END_Y", end_date.strftime("%Y"))
        oil_set_select(driver, "END_M", end_date.strftime("%m"))
        oil_set_select(driver, "END_D", end_date.strftime("%d"))

        # 001 / 002 / 003: Dubai / Brent / WTI
        for val in ["001", "002", "003"]:
            oil_set_checkbox(driver, val)

        search_btn = oil_safe_find(driver, By.ID, "glopcoilSelect", timeout=20, clickable=True)
        driver.execute_script("arguments[0].click();", search_btn)
        oil_wait_for_results(driver)
        df = oil_parse_html(driver.page_source)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    save_dataframe_to_db(df, OIL_TABLE, OIL_TABLE)
    return df


def oil_load_or_collect_data(force_refresh=False, crawl_start_date=None, crawl_end_date=None):
    if (not force_refresh) and OIL_CACHE["df"] is not None:
        return OIL_CACHE["df"].copy(), OIL_CACHE["fetched_at"]

    if not force_refresh:
        try:
            df = load_dataframe_from_db(OIL_TABLE)
            if not df.empty:
                for col in OIL_ALL_TYPES:
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors="coerce")
                df["DATE"] = pd.to_datetime(df["DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
                df = df.dropna(subset=["DATE"])
                fetched_at = get_dataset_meta(OIL_TABLE)
                OIL_CACHE["df"] = df.copy()
                OIL_CACHE["fetched_at"] = fetched_at
                print("[OIL] 기존 DB 로드 완료: oil_prices")
                return df, fetched_at
        except Exception as e:
            print(f"[OIL][WARN] 기존 DB 로드 실패(oil_prices): {e}")

    print("[OIL] 웹에서 최신 데이터 수집 시작")
    df = oil_collect_data(start_date=crawl_start_date, end_date=crawl_end_date, headless=True)
    fetched_at = get_dataset_meta(OIL_TABLE)
    if fetched_at == "-":
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    OIL_CACHE["df"] = df.copy()
    OIL_CACHE["fetched_at"] = fetched_at
    return df, fetched_at

def oil_to_long(df):
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "oil_type", "price"])
    work = df.copy()
    work["DATE"] = pd.to_datetime(work["DATE"], errors="coerce")
    work = work.dropna(subset=["DATE"])
    long_df = work.melt(id_vars=["DATE"], value_vars=OIL_ALL_TYPES, var_name="oil_type", value_name="price")
    long_df["price"] = pd.to_numeric(long_df["price"], errors="coerce")
    long_df = long_df.dropna(subset=["price"])
    long_df = long_df.rename(columns={"DATE": "date"})
    return long_df.sort_values(["oil_type", "date"]).reset_index(drop=True)


def oil_filter_df(long_df, oil_types=None, start_date=None, end_date=None):
    if long_df.empty:
        return long_df.copy()
    out = long_df.copy()
    if oil_types:
        out = out[out["oil_type"].isin(oil_types)]
    if start_date:
        out = out[out["date"] >= pd.to_datetime(start_date)]
    if end_date:
        out = out[out["date"] <= pd.to_datetime(end_date)]
    return out.sort_values(["oil_type", "date"]).reset_index(drop=True)


def oil_color(oil_type):
    return {
        "Dubai": TAC_BLUE,
        "Brent": TAC_GREEN,
        "WTI": TAC_RED,
    }.get(oil_type, TAC_PURPLE)


def oil_build_line_chart(df):
    fig = go.Figure()
    if not df.empty:
        for oil_type in OIL_ALL_TYPES:
            sub = df[df["oil_type"] == oil_type].sort_values("date")
            if sub.empty:
                continue
            add_solid_future_dashed_trace(
                fig,
                sub,
                x_col="date",
                y_col="price",
                name=oil_type,
                color=oil_color(oil_type),
                width=2.5,
                mode="lines",
                marker_size=0,
                hovertemplate="날짜=%{x|%Y-%m-%d}<br>유종=%{fullData.name}<br>가격=%{y:,.2f}<extra></extra>"
            )

    today_line = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    fig.add_shape(
        type="line",
        x0=today_line,
        x1=today_line,
        y0=0,
        y1=1,
        xref="x",
        yref="paper",
        line=dict(color="#64748b", width=1.5, dash="dash")
    )
    fig.add_annotation(
        x=today_line,
        y=1,
        xref="x",
        yref="paper",
        text="Today",
        showarrow=False,
        xanchor="left",
        yanchor="bottom",
        font=dict(size=11, color="#64748b")
    )

    fig.update_layout(
        template="plotly_white",
        height=520,
        margin=dict(l=30, r=20, t=25, b=40),
        hovermode="x unified",
        legend=dict(orientation="h", y=1.05),
        xaxis_title="Date",
        yaxis_title="Price"
    )
    return fig.to_html(full_html=False, include_plotlyjs="cdn")


def oil_build_spread_chart(df):
    if df.empty:
        return oil_build_line_chart(pd.DataFrame(columns=df.columns if hasattr(df, 'columns') else []))
    pivot = df.pivot_table(index="date", columns="oil_type", values="price", aggfunc="mean").sort_index()
    fig = go.Figure()
    if "Brent" in pivot.columns and "Dubai" in pivot.columns:
        fig.add_trace(go.Scatter(x=pivot.index, y=pivot["Brent"] - pivot["Dubai"], mode="lines", name="Brent - Dubai", line=dict(color=TAC_PURPLE, width=2.3)))
    if "WTI" in pivot.columns and "Dubai" in pivot.columns:
        fig.add_trace(go.Scatter(x=pivot.index, y=pivot["WTI"] - pivot["Dubai"], mode="lines", name="WTI - Dubai", line=dict(color=TAC_ORANGE, width=2.3)))
    fig.update_layout(template="plotly_white", height=420, margin=dict(l=30, r=20, t=25, b=40), hovermode="x unified", legend=dict(orientation="h", y=1.05), xaxis_title="Date", yaxis_title="Spread")
    return fig.to_html(full_html=False, include_plotlyjs=False)


def oil_build_kpis(df):
    kpis = []
    for oil_type in OIL_ALL_TYPES:
        sub = df[df["oil_type"] == oil_type].sort_values("date")
        if sub.empty:
            kpis.append({"label": oil_type, "value": "N/A", "sub": "데이터 없음", "class_name": "neutral"})
            continue
        latest = sub.iloc[-1]
        prev = sub.iloc[-2] if len(sub) >= 2 else None
        if prev is not None and prev["price"] not in [0, None] and not pd.isna(prev["price"]):
            diff = latest["price"] - prev["price"]
            pct = diff / prev["price"] * 100
            cls = "up" if diff > 0 else "down" if diff < 0 else "neutral"
            sub_txt = f"전일 대비 {diff:+.2f} ({pct:+.2f}%) | {latest['date'].strftime('%Y-%m-%d')}"
        else:
            cls = "neutral"
            sub_txt = f"{latest['date'].strftime('%Y-%m-%d')}"
        kpis.append({"label": oil_type, "value": f"{latest['price']:,.2f}", "sub": sub_txt, "class_name": cls})
    return kpis


OIL_HTML = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opinet Oil Price Dashboard</title>
<style>
* { box-sizing: border-box; }
body { font-family: 'Segoe UI', sans-serif; margin: 0; padding: 0; min-height: 100vh; background: linear-gradient(180deg, #f3f6fa 0%, #edf2f7 100%); color: #243746; }
a { text-decoration: none; color: inherit; }
.nav { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
.nav a { padding:10px 14px; border-radius:10px; border:1px solid #d5dde7; background:white; color:#1f3447; font-weight:700; }
.nav a.active { background:#3498db; color:white; border-color:#3498db; }
.header { border-bottom: 3px solid #3498db; padding-bottom: 18px; margin-bottom: 24px; }
.header h1 { margin: 0 0 8px 0; color: #1f3447; font-size: 28px; }
.header-meta { font-size: 13px; color: #5d6d7e; }
.header-meta a { color: #3498db; font-weight: 700; text-decoration: underline; }
.controls, .chart-card { background: white; padding: 24px; border-radius: 14px; box-shadow: 0 4px 18px rgba(0,0,0,0.05); margin-bottom: 24px; }
.section-title { font-weight: 700; margin: 0 0 14px 0; color: #2c3e50; font-size: 18px; }
.grid { display: grid; grid-template-columns: 1.4fr 1fr 1fr auto auto auto; gap: 16px; align-items: end; }
label { display: block; font-weight: 700; margin-bottom: 10px; color: #34495e; font-size: 13px; }
select, input[type="date"] { width: 100%; padding: 11px 12px; border: 1px solid #d5dde7; border-radius: 10px; background: white; color: #2c3e50; font-size: 14px; outline: none; }
.btn { display:inline-flex; align-items:center; justify-content:center; height:42px; padding:0 16px; border-radius:10px; border:1px solid #3498db; background:#3498db; color:white; font-weight:700; cursor:pointer; white-space:nowrap; }
.btn.secondary { background:white; color:#3498db; }
.btn.danger { background:#c0392b; border-color:#c0392b; }
.kpi-grid { display:grid; grid-template-columns: repeat(3, 1fr); gap:16px; margin-bottom:24px; }
.kpi { background:white; padding:20px; border-radius:14px; box-shadow: 0 4px 18px rgba(0,0,0,0.05); border-left:5px solid #3498db; }
.kpi-title { color:#5d6d7e; font-size:13px; font-weight:700; margin-bottom:8px; }
.kpi-value { font-size:28px; font-weight:800; color:#1f3447; margin-bottom:8px; }
.kpi-sub { font-size:12px; color:#5d6d7e; }
.kpi.up { border-left-color:#c0392b; }
.kpi.down { border-left-color:#27ae60; }
.chart-note {
  margin: -4px 0 12px 0;
  color: #64748b;
  font-size: 12px;
  font-weight: 700;
}

.alert { background:#fff8e1; border:1px solid #f5d76e; color:#7a5a00; padding:12px 14px; border-radius:12px; margin-bottom:18px; font-weight:700; }
@media (max-width: 900px) { .grid, .kpi-grid { grid-template-columns:1fr; } body { padding:14px; } }


.app-shell {
  max-width: 1680px;
  margin: 0 auto;
  padding: 24px;
  display: grid;
  grid-template-columns: 280px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.unified-sidebar {
  position: sticky;
  top: 24px;
  background: rgba(255,255,255,0.96);
  border: 1px solid #dbe4ee;
  border-radius: 18px;
  box-shadow: 0 8px 28px rgba(15,23,42,0.08);
  padding: 20px;
}
.sidebar-brand {
  margin-bottom: 20px;
  padding-bottom: 18px;
  border-bottom: 1px solid #e5edf5;
}
.sidebar-brand-title {
  font-size: 18px;
  font-weight: 900;
  color: #1f3447;
  line-height: 1.25;
}
.sidebar-brand-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #64748b;
  line-height: 1.5;
}
.sidebar-section-title {
  font-size: 12px;
  font-weight: 900;
  color: #64748b;
  text-transform: uppercase;
  letter-spacing: .5px;
  margin: 0 0 10px 0;
}
.unified-nav {
  display: grid;
  gap: 10px;
}
.unified-nav a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 52px;
  padding: 12px 14px;
  border-radius: 14px;
  border: 1px solid #d5dde7;
  background: #ffffff;
  color: #1f3447;
  font-weight: 800;
  text-decoration: none;
  transition: all .18s ease;
}
.unified-nav a:hover {
  background: #eff6ff;
  border-color: #93c5fd;
  color: #1d4ed8;
  transform: translateY(-1px);
}
.unified-nav a.active {
  background: #3498db;
  border-color: #3498db;
  color: #ffffff;
  box-shadow: 0 8px 18px rgba(52,152,219,.22);
}
.nav-main-text { display:block; font-size: 14px; }
.nav-sub-text { display:block; font-size: 11px; opacity: .78; margin-top: 2px; font-weight: 700; }
.nav-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 42px;
  height: 24px;
  padding: 0 8px;
  border-radius: 999px;
  background: #edf2f7;
  color: #475569;
  font-size: 11px;
  font-weight: 900;
}
.unified-nav a.active .nav-pill {
  background: rgba(255,255,255,.22);
  color: white;
}
.sidebar-note {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid #e5edf5;
  color: #64748b;
  font-size: 12px;
  line-height: 1.6;
}
.main-panel { min-width: 0; }
@media (max-width: 1100px) {
  .app-shell { grid-template-columns: 1fr; padding: 16px; }
  .unified-sidebar { position: static; }
}

</style>
</head>
<body>
<div class="app-shell">
  <aside class="unified-sidebar">
    <div class="sidebar-brand">
      <div class="sidebar-brand-title">Energy Market Dashboard</div>
      <div class="sidebar-brand-sub">제품유와 원유 데이터를 하나의 웹 화면에서 전환해 확인합니다.</div>
    </div>
    <div class="sidebar-section-title">Dashboards</div>
    <nav class="unified-nav">
      <a href="/product">
        <span><span class="nav-main-text">제품유 VLSFO</span><span class="nav-sub-text">Ship &amp; Bunker</span></span>
        <span class="nav-pill">제품</span>
      </a>
      <a href="/oil" class="active">
        <span><span class="nav-main-text">원유 가격</span><span class="nav-sub-text">Dubai / Brent / WTI</span></span>
        <span class="nav-pill">원유</span>
      </a>
    </nav>
    <div class="sidebar-note">Refresh 버튼을 누르면 해당 화면의 데이터를 다시 수집합니다.</div>
  </aside>
  <main class="main-panel">
<div class="header">
  <h1>Opinet International Oil Price Dashboard</h1>
  <div class="header-meta">
    Source: <a href="{{ source_url }}" target="_blank" rel="noopener noreferrer">Opinet</a>
    &nbsp; | &nbsp; Fetched at: {{ fetched_at }}
    &nbsp; | &nbsp; Rows: {{ row_count }}
  </div>
</div>
{% if message %}<div class="alert">{{ message }}</div>{% endif %}
<div class="controls">
  <form method="get" action="/oil">
    <div class="section-title">Filters</div>
    <div class="grid">
      <div>
        <label>유종 복수 선택</label>
        <select name="oil_types" multiple size="3">
          {% for oil in all_oil_types %}
            <option value="{{ oil }}" {% if oil in selected_oil_types %}selected{% endif %}>{{ oil }}</option>
          {% endfor %}
        </select>
      </div>
      <div><label>시작일</label><input type="date" name="start_date" value="{{ start_date }}"></div>
      <div><label>종료일</label><input type="date" name="end_date" value="{{ end_date }}"></div>
      <button class="btn" type="submit">Apply</button>
      <a class="btn secondary" href="/oil">Reset</a>
      <a class="btn danger" href="{{ refresh_url }}">Refresh</a>
    </div>
  </form>
</div>
<div class="kpi-grid">
{% for k in kpis %}
  <div class="kpi {{ k.class_name }}">
    <div class="kpi-title">{{ k.label }}</div>
    <div class="kpi-value">{{ k.value }}</div>
    <div class="kpi-sub">{{ k.sub }}</div>
  </div>
{% endfor %}
</div>
<div class="chart-card">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px;">
    <div>
      <div class="section-title" style="margin:0;">Dubai / Brent / WTI Price Trend</div>
      <div class="chart-note">오늘 이후 날짜의 데이터가 포함된 경우 해당 구간은 점선으로 표시됩니다.</div>
    </div>
    <a class="btn secondary" href="{{ download_url }}">CSV 다운로드</a>
  </div>
  {{ line_chart|safe }}
</div>
<div class="chart-card">
  <div class="section-title">Spread Trend</div>
  {{ spread_chart|safe }}
</div>
  </main>
</div>
</body>
</html>
"""


@app.route("/oil")
def oil_index():
    force_refresh = request.args.get("refresh", "0") == "1"
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    selected_oil_types = request.args.getlist("oil_types")
    if not selected_oil_types:
        selected_oil_types = OIL_ALL_TYPES.copy()
    selected_oil_types = [x for x in selected_oil_types if x in OIL_ALL_TYPES]

    message = ""
    try:
        df_wide, fetched_at = oil_load_or_collect_data(
            force_refresh=force_refresh,
            crawl_start_date=start_date or None,
            crawl_end_date=end_date or None
        )
    except Exception as e:
        df_wide = pd.DataFrame(columns=["DATE"] + OIL_ALL_TYPES)
        fetched_at = "-"
        message = f"원유 데이터 로드/수집 실패: {e}"

    long_df = oil_to_long(df_wide)
    filtered = oil_filter_df(long_df, oil_types=selected_oil_types, start_date=start_date or None, end_date=end_date or None)
    kpis = oil_build_kpis(filtered)
    line_chart = oil_build_line_chart(filtered)
    spread_chart = oil_build_spread_chart(filtered)

    oil_query = ''.join([f"&oil_types={urllib.parse.quote(x)}" for x in selected_oil_types])
    date_query = f"&start_date={urllib.parse.quote(start_date)}&end_date={urllib.parse.quote(end_date)}"
    download_url = f"/oil/download_csv?{oil_query}{date_query}"
    refresh_url = f"/oil?refresh=1{oil_query}{date_query}"

    return render_template_string(
        OIL_HTML,
        source_url=OIL_SOURCE_URL,
        fetched_at=fetched_at,
        row_count=len(df_wide),
        message=message,
        all_oil_types=OIL_ALL_TYPES,
        selected_oil_types=selected_oil_types,
        start_date=start_date,
        end_date=end_date,
        kpis=kpis,
        line_chart=line_chart,
        spread_chart=spread_chart,
        download_url=download_url,
        refresh_url=refresh_url,
    )


@app.route("/oil/download_csv")
def oil_download_csv():
    df_wide, _ = oil_load_or_collect_data(force_refresh=False)
    long_df = oil_to_long(df_wide)
    selected_oil_types = request.args.getlist("oil_types")
    if not selected_oil_types:
        selected_oil_types = OIL_ALL_TYPES.copy()
    selected_oil_types = [x for x in selected_oil_types if x in OIL_ALL_TYPES]
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    filtered = oil_filter_df(long_df, oil_types=selected_oil_types, start_date=start_date or None, end_date=end_date or None)
    export_df = filtered.copy()
    if not export_df.empty:
        export_df["date"] = export_df["date"].dt.strftime("%Y-%m-%d")
    csv_data = export_df.to_csv(index=False, encoding="utf-8-sig")
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=opinet_oil_prices_filtered.csv"}
    )


# =========================
# Flask 라우트
# =========================
@app.route("/")
@app.route("/product")
def index():
    force_refresh = request.args.get("refresh", "0") == "1"
    df, fetched_at = load_or_collect_data(force_refresh=force_refresh)
    forecast_df, forecast_fetched_at, forecast_message = load_or_collect_forecast_data(force_refresh=force_refresh)

    selected_ports = request.args.getlist("ports")
    if not selected_ports:
        selected_ports = ALL_PORTS.copy()
    selected_ports = [p for p in selected_ports if p in ALL_PORTS]

    ma_mode = request.args.get("ma_mode", "quarterly")
    if ma_mode not in ["off", "quarterly", "semiannual", "annual"]:
        ma_mode = "quarterly"

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    filtered_df = filter_df(df, ports=selected_ports, start_date=start_date or None, end_date=end_date or None)
    filtered_df = add_features(filtered_df, ma_mode=ma_mode)
    chart_df = add_forecast_to_chart_df(filtered_df, forecast_df, selected_ports=selected_ports, start_date=start_date or None, end_date=end_date or None)

    kpis = build_kpis(filtered_df)
    line_chart = build_line_chart(chart_df, ma_mode=ma_mode)
    forecast_table_html = build_forecast_table_html(forecast_df, fetched_at=forecast_fetched_at, message=forecast_message)
    bar_chart = build_latest_bar_chart(filtered_df)
    spread_chart = build_spread_chart(filtered_df)
    daily_change_chart = build_daily_change_chart(filtered_df)
    period_compare_chart = build_period_compare_chart(filtered_df)
    compare_table = build_period_compare_table(filtered_df)
    period_change_kpis = compute_period_change_kpis(filtered_df)
    insights_html = format_implications_html(build_insights(filtered_df))

    ports_query = ''.join([f"&ports={urllib.parse.quote(p)}" for p in selected_ports])
    date_query = f"&start_date={urllib.parse.quote(start_date)}&end_date={urllib.parse.quote(end_date)}"
    download_url = f"/download_csv?ma_mode={ma_mode}{ports_query}{date_query}"
    refresh_url = f"/?ma_mode={ma_mode}{ports_query}{date_query}&refresh=1"

    return render_template_string(
        HTML,
        fetched_at=fetched_at,
        all_ports=ALL_PORTS,
        selected_ports=selected_ports,
        ma_mode=ma_mode,
        start_date=start_date,
        end_date=end_date,
        kpis=kpis,
        line_chart=line_chart,
        bar_chart=bar_chart,
        spread_chart=spread_chart,
        daily_change_chart=daily_change_chart,
        period_compare_chart=period_compare_chart,
        compare_table=compare_table,
        period_change_kpis=period_change_kpis,
        insights_html=insights_html,
        forecast_table_html=forecast_table_html,
        base_source_url=BASE_SOURCE_URL,
        download_url=download_url,
        refresh_url=refresh_url
    )


@app.route("/download_csv")
def download_csv():
    df, _ = load_or_collect_data(force_refresh=False)

    selected_ports = request.args.getlist("ports")
    if not selected_ports:
        selected_ports = ALL_PORTS.copy()
    selected_ports = [p for p in selected_ports if p in ALL_PORTS]

    ma_mode = request.args.get("ma_mode", "quarterly")
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    filtered_df = filter_df(df, ports=selected_ports, start_date=start_date or None, end_date=end_date or None)
    filtered_df = add_features(filtered_df, ma_mode=ma_mode)

    export_df = filtered_df.copy()
    if not export_df.empty:
        export_df["date"] = export_df["date"].dt.strftime("%Y-%m-%d")

    csv_data = export_df.to_csv(index=False, encoding="utf-8-sig")
    filename = "shipandbunker_vlsfo.csv"

    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# =========================
# 실행 유틸
# =========================
def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(host='127.0.0.1'):
    for candidate in [8080, 8000, 5000, 8501, 7860, 3000]:
        if is_port_available(host, candidate):
            return candidate
    for candidate in range(8000, 8101):
        if is_port_available(host, candidate):
            return candidate
    raise RuntimeError('사용 가능한 포트를 찾지 못했습니다.')


def wait_and_open_browser(url, port, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                webbrowser.open_new(url)
                return
        except Exception:
            time.sleep(0.4)


if __name__ == '__main__':
    host = '127.0.0.1'
    port = find_available_port(host)
    local_url = f'http://127.0.0.1:{port}'
    print(f"[INFO] Flask 서버 시작: {local_url}")
    print(f"[INFO] 데이터 저장 DB: {get_db_path()}")
    threading.Thread(target=wait_and_open_browser, args=(local_url, port), daemon=True).start()
    app.run(host=host, port=port, debug=False, use_reloader=False)