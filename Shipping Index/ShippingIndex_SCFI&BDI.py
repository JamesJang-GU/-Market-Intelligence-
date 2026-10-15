# -*- coding: utf-8 -*-
import sys
import subprocess
import os
import re
import time
import logging
import warnings
import sqlite3
import webbrowser
import argparse
from threading import Timer, Lock
from io import BytesIO
from datetime import datetime, date
from urllib.parse import urlencode

REQUIRED_PACKAGES = {
    'flask': 'flask',
    'pandas': 'pandas',
    'plotly': 'plotly',
    'selenium': 'selenium',
    'openpyxl': 'openpyxl',
    'webdriver_manager': 'webdriver-manager',
    'kaleido': 'kaleido'
}

print('[System] Checking required packages...')
for module_name, pip_name in REQUIRED_PACKAGES.items():
    try:
        __import__(module_name)
    except ImportError:
        print(f'[Notice] Installing missing package: {pip_name}')
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', pip_name, '--break-system-packages'])

warnings.filterwarnings('ignore')

import pandas as pd
from flask import Flask, render_template_string, Response, request, redirect, url_for
import plotly.graph_objects as go
import plotly.io as pio
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import TimeoutException, NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

try:
    from webdriver_manager.chrome import ChromeDriverManager
    HAS_WDM = True
except Exception:
    HAS_WDM = False


BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in globals() else os.getcwd()
LOG_FILE = os.path.join(BASE_DIR, 'index_crawl.log')
DB_FILE = os.path.join(BASE_DIR, 'index_history.db')
BASE_URL = 'https://www.ksg.co.kr/'

DEFAULT_INDEX_NAME = 'SCFI'
SUPPORTED_INDEXES = ['SCFI', 'BDI']
PALETTE_OPTIONS = ['Auto', 'Grey', 'Red', 'Blue']

BDI_LABEL_MAP = {
    'BDI': 'BDI (Baltic Exchange Dry Index)',
    'BCI': 'BCI (Baltic Capesize Index)',
    'BPI': 'BPI (Baltic Panamax Index)',
    'BSI': 'BSI (Baltic Supramax Index)',
    'BHSI': 'BHSI (Baltic Handysize Index)',
    'BDTI': 'BDTI (Baltic Dirty Tanker Index)',
    'BCTI': 'BCTI (Baltic Clean Tanker Index)'
}

BDI_REGION_PRIORITY = ['BDI', 'BCI', 'BPI', 'BSI', 'BHSI', 'BDTI', 'BCTI']

INVALID_REGION_NAMES = {
    '',
    '일자',
    '날짜',
    '날 자',
    'DATE',
    'Date',
    'date',
    'YEAR',
    'Year',
    'year',
    'INDEX_NAME',
    'CRAWL_PAGE'
}

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

crawl_lock = Lock()
RUNTIME_CONFIG = None

logger = logging.getLogger('index_web')
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(formatter)
logger.addHandler(sh)


def activate_file_logging():
    if any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        return
    try:
        fh = logging.FileHandler(LOG_FILE, mode='a', encoding='utf-8')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
    except Exception:
        pass


def log(msg):
    logger.info(msg)


def parse_args():
    parser = argparse.ArgumentParser(description='Shipping Index Dashboard + Incremental Crawler')
    parser.add_argument('--user', default=os.getenv('KSG_ID', '30979'))
    parser.add_argument('--password', default=os.getenv('KSG_PW', 'a30979'))
    parser.add_argument('--driver-path', default=os.getenv('CHROMEDRIVER_PATH', ''))
    parser.add_argument('--host', default=os.getenv('SCFI_WEB_HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.getenv('SCFI_WEB_PORT', '9000')))
    parser.add_argument('--open-browser', dest='open_browser', action='store_true', default=True)
    parser.add_argument('--no-open-browser', dest='open_browser', action='store_false')
    parser.add_argument('--headless', dest='headless', action='store_true', default=True)
    parser.add_argument('--no-headless', dest='headless', action='store_false')
    parser.add_argument('--incremental', dest='incremental', action='store_true', default=True)
    parser.add_argument('--no-incremental', dest='incremental', action='store_false')
    parser.add_argument('--max-pages', type=int, default=100000)
    parser.add_argument('--pause', type=float, default=0.2)
    parser.add_argument('--init-refresh', dest='init_refresh', action='store_true', default=False)
    parser.add_argument('--no-init-refresh', dest='init_refresh', action='store_false')
    return parser.parse_args()


def normalize_index_name(index_name):
    idx = str(index_name or DEFAULT_INDEX_NAME).strip().upper()
    return idx if idx in SUPPORTED_INDEXES else DEFAULT_INDEX_NAME


def normalize_palette_name(name):
    val = str(name or 'Auto').strip()
    return val if val in PALETTE_OPTIONS else 'Auto'


def is_valid_region_for_index(region, index_name):
    """
    화면의 항로/지수 선택 목록 및 DB 저장 대상에서 제외해야 할 컬럼명을 걸러낸다.
    특히 BDI에서는 '일자' 같은 날짜 컬럼이 지수처럼 노출되는 문제를 방지한다.
    """
    index_name = normalize_index_name(index_name)
    region = str(region or '').strip()

    if region in INVALID_REGION_NAMES:
        return False

    if index_name == 'BDI':
        return region in BDI_REGION_PRIORITY

    return True


def display_region_label(region, selected_index):
    region = str(region)
    selected_index = normalize_index_name(selected_index)
    if selected_index == 'BDI':
        return BDI_LABEL_MAP.get(region, region)
    return region


def sort_regions_for_display(regions, selected_index):
    selected_index = normalize_index_name(selected_index)

    regions = [
        str(r).strip()
        for r in regions
        if is_valid_region_for_index(r, selected_index)
    ]

    regions = sorted(set(regions))

    if selected_index == 'BDI':
        priority_map = {name: i for i, name in enumerate(BDI_REGION_PRIORITY)}
        return sorted(regions, key=lambda x: (priority_map.get(x, 999), x))

    return regions


def parse_date(date_text):
    date_text = str(date_text).strip()
    fmts = ['%Y-%m-%d', '%Y.%m.%d', '%Y/%m/%d', '%y-%m-%d', '%y.%m.%d', '%y/%m/%d']
    for fmt in fmts:
        try:
            return datetime.strptime(date_text, fmt).date()
        except ValueError:
            pass
    return date_text


def clean_number(text):
    text = str(text).strip().replace(',', '')
    if text == '':
        return None
    if re.fullmatch(r'[+-]?\d+(\.\d+)?', text):
        try:
            return float(text) if '.' in text else int(text)
        except Exception:
            return text
    return text


def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS index_raw (
            dt TEXT NOT NULL,
            region TEXT NOT NULL,
            value REAL,
            index_name TEXT NOT NULL,
            source_file TEXT,
            created_at TEXT,
            PRIMARY KEY (dt, region, index_name)
        )
    """)
    conn.execute("""
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
    """)
    conn.commit()
    return conn


def cleanup_invalid_regions():
    """
    기존 DB에 잘못 저장된 region 값을 정리한다.
    예: BDI 화면에 '일자'가 지수처럼 표시되는 문제 제거.
    """
    try:
        conn = get_db_conn()
        cur = conn.cursor()

        invalid_names = sorted(INVALID_REGION_NAMES)
        placeholders = ','.join(['?'] * len(invalid_names))

        cur.execute(
            f"""
            DELETE FROM index_raw
            WHERE TRIM(region) IN ({placeholders})
            """,
            invalid_names
        )

        allowed_bdi = BDI_REGION_PRIORITY
        bdi_placeholders = ','.join(['?'] * len(allowed_bdi))

        cur.execute(
            f"""
            DELETE FROM index_raw
            WHERE index_name = 'BDI'
              AND TRIM(region) NOT IN ({bdi_placeholders})
            """,
            allowed_bdi
        )

        conn.commit()
        conn.close()
        log('[DB] 잘못된 region 데이터 정리 완료')
    except Exception as e:
        log(f'[DB] 잘못된 region 데이터 정리 실패: {e}')


def get_last_date_from_df(df):
    if df is None or df.empty:
        return None
    first_col = df.columns[0]
    dates = pd.to_datetime(df[first_col], errors='coerce').dropna()
    if dates.empty:
        return None
    return dates.max().date()


def get_first_date_value(df):
    if df is None or df.empty:
        return None
    first_col = df.columns[0]
    vals = df[first_col].dropna().tolist()
    for v in vals:
        if isinstance(v, (datetime, date)):
            return pd.to_datetime(v).strftime('%Y-%m-%d')
        s = str(v).strip()
        if s:
            return s
    return None


def get_driver(driver_path='', headless=True):
    options = webdriver.ChromeOptions()
    options.add_argument('--disable-popup-blocking')
    options.add_argument('--disable-notifications')
    options.add_argument('--disable-gpu')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--lang=ko-KR')
    options.add_argument('--window-size=1600,1000')
    options.page_load_strategy = 'normal'

    if headless:
        options.add_argument('--headless=new')

    if driver_path:
        log(f'[드라이버] 지정 경로 사용: {driver_path}')
        return webdriver.Chrome(service=Service(driver_path), options=options)

    if HAS_WDM:
        try:
            log('[드라이버] webdriver_manager 자동 설치 사용')
            installed = ChromeDriverManager().install()
            exe_name = 'chromedriver.exe' if os.name == 'nt' else 'chromedriver'
            if os.path.basename(installed) != exe_name:
                folder = os.path.dirname(installed)
                candidate = os.path.join(folder, exe_name)
                if os.path.exists(candidate):
                    installed = candidate
            return webdriver.Chrome(service=Service(installed), options=options)
        except Exception as e:
            log(f'[경고] webdriver_manager 사용 실패: {e}')

    return webdriver.Chrome(options=options)


def handle_login_alerts(driver, timeout=1.0, max_alerts=3):
    for _ in range(max_alerts):
        try:
            WebDriverWait(driver, timeout).until(EC.alert_is_present())
            alert = driver.switch_to.alert
            log(f'Alert detected: {alert.text}')
            alert.accept()
            time.sleep(0.1)
        except (TimeoutException, NoAlertPresentException):
            return


def login(driver, user_id, password):
    driver.get(BASE_URL)
    wait = WebDriverWait(driver, 15)
    wait.until(EC.element_to_be_clickable((By.LINK_TEXT, '로그인'))).click()

    id_input = wait.until(EC.presence_of_element_located((By.ID, 'loginId')))
    pw_input = wait.until(EC.presence_of_element_located((By.ID, 'loginPass')))

    id_input.clear()
    id_input.send_keys(user_id)
    pw_input.clear()
    pw_input.send_keys(password)

    driver.execute_script('loginSubmitForm(document.form1);')
    handle_login_alerts(driver)
    time.sleep(0.5)
    log('로그인이 완료되었습니다.')


def go_to_index_page(driver, index_name):
    index_name = normalize_index_name(index_name)
    wait = WebDriverWait(driver, 15)
    wait.until(EC.element_to_be_clickable((By.LINK_TEXT, index_name))).click()
    time.sleep(0.8)
    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'table, .paginate, .paging')))
    except Exception:
        pass
    log(f'{index_name} 지수 메인 페이지 진입 완료')


def parse_current_table_fast(driver, index_name):
    index_name = normalize_index_name(index_name)
    script = f"""
    const INDEX_NAME = '{index_name}';
    const tables = Array.from(document.querySelectorAll('table'));
    let target = null;

    for (const t of tables) {{
      const cap = (t.querySelector('caption')?.innerText || '').toUpperCase();
      const txt = (t.innerText || '').toUpperCase();
      if ((cap.includes(INDEX_NAME) || txt.includes(INDEX_NAME) || txt.includes('일자')) && t.querySelector('tbody tr')) {{
        target = t;
        break;
      }}
    }}

    if (!target) {{
      let maxRows = 0;
      for (const t of tables) {{
        const rowsCount = t.querySelectorAll('tbody tr').length;
        if (rowsCount > maxRows) {{
          maxRows = rowsCount;
          target = t;
        }}
      }}
    }}

    if (!target) return {{headers: [], rows: []}};

    const headerRows = Array.from(target.querySelectorAll('thead tr'));
    let headers = [];
    if (headerRows.length) {{
      headers = Array.from(headerRows[headerRows.length - 1].querySelectorAll('th,td')).map(x => (x.innerText || '').trim());
    }}

    if (!headers.length) {{
      const firstBodyRow = target.querySelector('tbody tr');
      const n = firstBodyRow ? firstBodyRow.querySelectorAll('th,td').length : 0;
      headers = Array.from({{length: n}}, (_, i) => 'COL_' + (i + 1));
    }}

    const rows = Array.from(target.querySelectorAll('tbody tr')).map(tr =>
      Array.from(tr.querySelectorAll('th,td')).map(td => (td.innerText || '').trim())
    ).filter(r => r.length > 0);

    return {{headers, rows}};
    """
    data = driver.execute_script(script)
    headers = data.get('headers', []) if isinstance(data, dict) else []
    rows = data.get('rows', []) if isinstance(data, dict) else []

    if not rows:
        return pd.DataFrame()

    max_len = max(len(r) for r in rows)
    if len(headers) < max_len:
        headers = headers + [f'COL_{i}' for i in range(len(headers) + 1, max_len + 1)]

    normalized_rows = []
    for r in rows:
        r = list(r) + [''] * (len(headers) - len(r))
        row = {}
        for i, val in enumerate(r):
            col = headers[i] if i < len(headers) and headers[i] else f'COL_{i+1}'
            row[col] = parse_date(val) if i == 0 else clean_number(val)
        row['INDEX_NAME'] = index_name
        normalized_rows.append(row)

    return pd.DataFrame(normalized_rows)


def get_table_quick_signature(driver, index_name):
    index_name = normalize_index_name(index_name)
    try:
        script = f"""
        const INDEX_NAME = '{index_name}';
        const tables = Array.from(document.querySelectorAll('table'));
        let target = null;
        for (const t of tables) {{
          const cap = (t.querySelector('caption')?.innerText || '').toUpperCase();
          const txt = (t.innerText || '').toUpperCase();
          if ((cap.includes(INDEX_NAME) || txt.includes(INDEX_NAME) || txt.includes('일자')) && t.querySelector('tbody tr')) {{
            target = t;
            break;
          }}
        }}
        if (!target && tables.length) {{
          let maxRows = 0;
          for (const t of tables) {{
            if (t.querySelectorAll('tbody tr').length > maxRows) {{
              maxRows = t.querySelectorAll('tbody tr').length;
              target = t;
            }}
          }}
        }}
        const currentPage = () => {{
          const sels = ['.paginate strong', '.paging strong', '.pagination .active a', '.pagination strong', '.paginate .on'];
          for (const s of sels) {{
            const el = document.querySelector(s);
            if (el && /^\\d+$/.test((el.innerText || '').trim())) return (el.innerText || '').trim();
          }}
          return '';
        }};
        let rowSig = '';
        if (target) {{
          const row = target.querySelector('tbody tr');
          const cells = row ? Array.from(row.querySelectorAll('th,td')).slice(0, 4) : [];
          rowSig = cells.map(c => (c.innerText || '').trim()).join('|');
        }}
        return currentPage() + '||' + rowSig;
        """
        sig = driver.execute_script(script)
        return (sig or '').strip()
    except Exception:
        return ''


def wait_for_table_change(driver, prev_signature, index_name, timeout=8):
    start = time.time()
    while time.time() - start < timeout:
        try:
            current_sig = get_table_quick_signature(driver, index_name)
            if current_sig and current_sig != prev_signature:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _try_click(driver, elem):
    try:
        if not elem.is_displayed():
            return False
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        driver.execute_script('arguments[0].click();', elem)
        return True
    except Exception:
        return False


def _get_current_page_number(driver):
    selectors = [
        (By.CSS_SELECTOR, '.paginate strong'),
        (By.CSS_SELECTOR, '.paging strong'),
        (By.CSS_SELECTOR, '.pagination .active a'),
        (By.CSS_SELECTOR, '.pagination strong'),
        (By.CSS_SELECTOR, '.paginate .on'),
    ]
    for by, sel in selectors:
        try:
            elems = driver.find_elements(by, sel)
            for e in elems:
                txt = (e.text or '').strip()
                if txt.isdigit():
                    return int(txt)
        except Exception:
            continue
    return None


def move_to_next_page(driver):
    current_page = _get_current_page_number(driver)
    if current_page:
        target_page = current_page + 1
        try:
            if driver.execute_script("return typeof goPage === 'function';"):
                driver.execute_script(f"goPage({target_page});")
                return True
        except Exception:
            pass

        for xp in [
            f"//a[normalize-space(text())='{target_page}']",
            f"//span[normalize-space(text())='{target_page}']/parent::a"
        ]:
            try:
                for elem in driver.find_elements(By.XPATH, xp):
                    if _try_click(driver, elem):
                        return True
            except Exception:
                pass

    candidates = [
        (By.CSS_SELECTOR, 'a.next'),
        (By.CSS_SELECTOR, '.paginate a.next'),
        (By.CSS_SELECTOR, '.paging a.next'),
        (By.LINK_TEXT, '다음'),
        (By.XPATH, "//a[contains(text(), '다음')]")
    ]
    for by, sel in candidates:
        try:
            for elem in driver.find_elements(by, sel):
                if _try_click(driver, elem):
                    return True
        except Exception:
            continue
    return False


def crawl_all_pages(driver, index_name, max_pages=100000, pause=0.2, stop_date=None):
    index_name = normalize_index_name(index_name)
    log(f'{index_name} 수집 시작 (증분 기준점: {stop_date})')
    all_frames = []
    seen_signatures = set()

    for page_no in range(1, max_pages + 1):
        df = None
        for _ in range(4):
            try:
                df = parse_current_table_fast(driver, index_name)
                if df is not None and not df.empty:
                    break
            except Exception:
                pass
            time.sleep(0.4)

        if df is None or df.empty:
            log(f'{index_name} {page_no}페이지: 수집 데이터 없음')
            break

        if stop_date is not None:
            first_col = df.columns[0]
            df_dates = pd.to_datetime(df[first_col], errors='coerce').dt.date
            past_records = df[df_dates <= stop_date]
            if not past_records.empty:
                df = df[df_dates > stop_date]
                if not df.empty:
                    df['CRAWL_PAGE'] = page_no
                    all_frames.append(df)
                log(f'[{index_name}] {page_no}페이지에서 기준일 발견, 조기 종료')
                break

        sig = get_table_quick_signature(driver, index_name) or get_first_date_value(df)
        if sig in seen_signatures:
            break
        seen_signatures.add(sig)

        df['CRAWL_PAGE'] = page_no
        all_frames.append(df)
        log(f'[진행] {index_name} {page_no}페이지 수집 완료: +{len(df)}행')

        moved = move_to_next_page(driver)
        if not moved:
            break

        if not wait_for_table_change(driver, sig, index_name=index_name, timeout=8):
            break

        if pause > 0:
            time.sleep(pause)

    if not all_frames:
        return pd.DataFrame()

    return pd.concat(all_frames, ignore_index=True)


def merge_with_existing(existing_df, new_df):
    if existing_df is None or existing_df.empty:
        return new_df
    if new_df is None or new_df.empty:
        return existing_df

    combined = pd.concat([new_df, existing_df], ignore_index=True)
    first_col = combined.columns[0]
    combined['__sort_dt'] = pd.to_datetime(combined[first_col], errors='coerce')
    combined = combined.drop_duplicates(subset=[first_col], keep='first')
    combined = combined.sort_values(by='__sort_dt', ascending=True).drop(columns=['__sort_dt'])
    return combined.reset_index(drop=True)


def _melt_for_pivot(df, index_name):
    index_name = normalize_index_name(index_name)

    if df is None or df.empty:
        return pd.DataFrame(columns=['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME'])

    work = df.copy()
    date_col = work.columns[0]
    work[date_col] = pd.to_datetime(work[date_col], errors='coerce')
    work = work.dropna(subset=[date_col])

    exclude_cols = {date_col, 'INDEX_NAME', 'CRAWL_PAGE'}

    value_cols = [
        c for c in work.columns
        if c not in exclude_cols and is_valid_region_for_index(c, index_name)
    ]

    numeric_cols = []
    for c in value_cols:
        coerced = pd.to_numeric(work[c], errors='coerce')
        if coerced.notna().any():
            work[c] = coerced
            numeric_cols.append(c)

    if not numeric_cols:
        return pd.DataFrame(columns=['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME'])

    melted = work.melt(
        id_vars=[date_col],
        value_vars=numeric_cols,
        var_name='REGION',
        value_name='VALUE'
    )

    melted = melted.rename(columns={date_col: 'DATE'})
    melted['YEAR'] = melted['DATE'].dt.year
    melted['INDEX_NAME'] = index_name
    melted = melted.dropna(subset=['VALUE'])
    melted['DATE'] = pd.to_datetime(melted['DATE']).dt.normalize()

    melted = melted[
        melted['REGION'].apply(lambda r: is_valid_region_for_index(r, index_name))
    ]

    return melted[['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME']].sort_values(
        ['DATE', 'REGION']
    ).reset_index(drop=True)


def get_total_row_count(index_name=None):
    conn = get_db_conn()
    if index_name:
        n = int(pd.read_sql_query(
            'SELECT COUNT(*) cnt FROM index_raw WHERE index_name = ?',
            conn,
            params=[normalize_index_name(index_name)]
        )['cnt'].iloc[0])
    else:
        n = int(pd.read_sql_query('SELECT COUNT(*) cnt FROM index_raw', conn)['cnt'].iloc[0])
    conn.close()
    return n


def upsert_long_df_to_db(df_long, index_name, source_file=''):
    index_name = normalize_index_name(index_name)

    if df_long is None or df_long.empty:
        return 0, get_total_row_count(index_name)

    conn = get_db_conn()
    cur = conn.cursor()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    inserted = 0
    for _, r in df_long.iterrows():
        dt = pd.to_datetime(r['DATE'], errors='coerce')
        if pd.isna(dt):
            continue

        region = str(r['REGION']).strip()
        if not is_valid_region_for_index(region, index_name):
            continue

        value = pd.to_numeric(r['VALUE'], errors='coerce')

        cur.execute("""
            INSERT INTO index_raw (dt, region, value, index_name, source_file, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(dt, region, index_name) DO UPDATE SET
                value=excluded.value,
                source_file=excluded.source_file,
                created_at=excluded.created_at
        """, (
            dt.strftime('%Y-%m-%d'),
            region,
            None if pd.isna(value) else float(value),
            index_name,
            source_file,
            now
        ))
        inserted += 1

    conn.commit()
    total_rows = int(pd.read_sql_query(
        'SELECT COUNT(*) cnt FROM index_raw WHERE index_name = ?',
        conn,
        params=[index_name]
    )['cnt'].iloc[0])
    conn.close()
    return inserted, total_rows
def load_long_df_from_db(index_name):
    index_name = normalize_index_name(index_name)
    conn = get_db_conn()
    df = pd.read_sql_query(
        '''
        SELECT dt as DATE, region as REGION, value as VALUE, index_name as INDEX_NAME
        FROM index_raw
        WHERE index_name = ?
        ORDER BY dt, region
        ''',
        conn,
        params=[index_name]
    )
    conn.close()

    if df.empty:
        return pd.DataFrame(columns=['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME'])

    df['DATE'] = pd.to_datetime(df['DATE'], errors='coerce')
    df = df.dropna(subset=['DATE'])

    df['REGION'] = df['REGION'].astype(str).str.strip()
    df = df[
        df['REGION'].apply(lambda r: is_valid_region_for_index(r, index_name))
    ]

    if df.empty:
        return pd.DataFrame(columns=['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME'])

    df['YEAR'] = df['DATE'].dt.year
    return df[['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME']]


def load_wide_df_from_db(index_name):
    long_df = load_long_df_from_db(index_name=index_name)
    if long_df.empty:
        return pd.DataFrame()
    pivot_df = long_df.pivot_table(
        index='DATE',
        columns='REGION',
        values='VALUE',
        aggfunc='mean'
    ).sort_index().reset_index()
    pivot_df.columns.name = None
    return pivot_df


def get_last_update_meta(index_name):
    index_name = normalize_index_name(index_name)
    conn = get_db_conn()
    df = pd.read_sql_query(
        'SELECT * FROM crawl_audit WHERE index_name = ? ORDER BY audit_id DESC LIMIT 1',
        conn,
        params=[index_name]
    )
    conn.close()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def run_incremental_sync(config, index_name, force_full=False):
    index_name = normalize_index_name(index_name)

    with crawl_lock:
        started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = get_db_conn()
        cur = conn.cursor()
        cur.execute(
            'INSERT INTO crawl_audit (started_at, status, message, rows_added, total_rows, index_name) VALUES (?, ?, ?, ?, ?, ?)',
            (started_at, 'RUNNING', '', 0, get_total_row_count(index_name), index_name)
        )
        audit_id = cur.lastrowid
        conn.commit()
        conn.close()

        driver = None
        try:
            existing_df = None
            stop_date = None

            # Refresh의 증분 기준과 기존 데이터는 XLSX가 아니라 DB만 사용한다.
            if config.incremental and not force_full:
                wide_df = load_wide_df_from_db(index_name=index_name)
                if not wide_df.empty:
                    existing_df = wide_df.rename(columns={'DATE': '일자'})
                    stop_date = get_last_date_from_df(existing_df)
                    if stop_date:
                        log(f'[{index_name}] DB 기준 최종일: {stop_date}')

            driver = get_driver(driver_path=config.driver_path, headless=config.headless)
            login(driver, config.user, config.password)
            go_to_index_page(driver, index_name=index_name)

            new_df = crawl_all_pages(
                driver,
                index_name=index_name,
                max_pages=config.max_pages,
                pause=config.pause,
                stop_date=stop_date
            )

            if (new_df is None or new_df.empty) and (existing_df is not None and not existing_df.empty):
                final_df = existing_df
            else:
                final_df = merge_with_existing(existing_df, new_df)

            # Refresh에서는 XLSX를 만들지 않고 DB만 갱신한다.
            long_df = _melt_for_pivot(final_df, index_name=index_name)
            rows_added, total_rows = upsert_long_df_to_db(
                long_df,
                index_name=index_name,
                source_file=''
            )

            cleanup_invalid_regions()

            conn = get_db_conn()
            conn.execute(
                'UPDATE crawl_audit SET finished_at=?, status=?, message=?, rows_added=?, total_rows=? WHERE audit_id=?',
                (
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'SUCCESS',
                    'OK',
                    rows_added,
                    total_rows,
                    audit_id
                )
            )
            conn.commit()
            conn.close()

            return {
                'ok': True,
                'rows_added': rows_added,
                'total_rows': total_rows,
                'output_file': ''
            }

        except Exception as e:
            activate_file_logging()
            logger.exception(f'[{index_name}] 크롤러 실행 예외: {e}')

            conn = get_db_conn()
            conn.execute(
                'UPDATE crawl_audit SET finished_at=?, status=?, message=?, rows_added=?, total_rows=? WHERE audit_id=?',
                (
                    datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'ERROR',
                    str(e)[:1000],
                    0,
                    get_total_row_count(index_name),
                    audit_id
                )
            )
            conn.commit()
            conn.close()
            return {'ok': False, 'error': str(e)}

        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass


def filter_long_df(df_long, start_date=None, end_date=None, years=None, regions=None):
    if df_long is None or df_long.empty:
        return df_long

    work = df_long.copy()
    work['DATE'] = pd.to_datetime(work['DATE'], errors='coerce')
    work = work.dropna(subset=['DATE'])

    if 'REGION' in work.columns and 'INDEX_NAME' in work.columns and not work.empty:
        work['REGION'] = work['REGION'].astype(str).str.strip()
        work = work[
            work.apply(
                lambda r: is_valid_region_for_index(r['REGION'], r['INDEX_NAME']),
                axis=1
            )
        ]

    if start_date:
        work = work[work['DATE'] >= pd.to_datetime(start_date)]
    if end_date:
        work = work[work['DATE'] <= pd.to_datetime(end_date)]
    if years:
        years = sorted({int(y) for y in years if str(y).strip().isdigit()})
        if years:
            work = work[work['YEAR'].isin(years)]
    if regions:
        regions = [
            str(r).strip()
            for r in regions
            if r is not None and str(r).strip()
        ]
        work = work[work['REGION'].isin(regions)]

    return work.sort_values(['DATE', 'REGION']).reset_index(drop=True)


def get_palette_colors(name):
    name = normalize_palette_name(name)
    palettes = {
        'Auto': [
            '#e53935', '#fb8c00', '#fdd835', '#43a047', '#00acc1',
            '#1e88e5', '#3949ab', '#8e24aa', '#d81b60', '#6d4c41',
            '#00897b', '#7cb342'
        ],
        'Grey': [
            '#111827', '#1f2937', '#374151', '#4b5563',
            '#6b7280', '#9ca3af', '#d1d5db', '#e5e7eb'
        ],
        'Red': [
            '#7f1d1d', '#991b1b', '#b91c1c', '#dc2626',
            '#ef4444', '#f87171', '#fb7185', '#ec4899', '#f97316'
        ],
        'Blue': [
            '#0f172a', '#1d4ed8', '#2563eb', '#3b82f6',
            '#60a5fa', '#0ea5e9', '#38bdf8', '#0284c7', '#1e3a8a'
        ]
    }
    return palettes.get(name, palettes['Auto'])


def build_auto_color_map(regions, palette_name='Auto'):
    palette_name = normalize_palette_name(palette_name)
    colors = get_palette_colors(palette_name)
    return {region: colors[i % len(colors)] for i, region in enumerate(regions)}


def safe_color_key(region):
    return 'color_' + re.sub(r'[^a-zA-Z0-9가-힣_]+', '_', str(region))


def extract_user_color_map(regions, req):
    out = {}
    for region in regions:
        key = safe_color_key(region)
        val = req.args.get(key, '').strip()
        if re.fullmatch(r'#[0-9a-fA-F]{6}', val):
            out[region] = val
    return out


def build_final_color_map(regions, palette_name, user_color_map):
    auto_map = build_auto_color_map(regions, palette_name)
    final_map = {}
    for region in regions:
        final_map[region] = user_color_map.get(region) or auto_map.get(region)
    return final_map


def build_peak_trough_html(df_long, selected_index, selected_regions=None, sort_by='region'):
    if df_long is None or df_long.empty:
        return '-'

    regions = selected_regions if selected_regions else sort_regions_for_display(
        df_long['REGION'].dropna().astype(str).unique().tolist(),
        selected_index
    )

    regions = [
        r for r in regions
        if is_valid_region_for_index(r, selected_index)
    ]

    records = []
    for region in regions:
        sub = df_long[df_long['REGION'] == region].copy()
        sub['VALUE'] = pd.to_numeric(sub['VALUE'], errors='coerce')
        sub = sub.dropna(subset=['DATE', 'VALUE'])
        if sub.empty:
            continue

        peak_row = sub.loc[sub['VALUE'].idxmax()]
        trough_row = sub.loc[sub['VALUE'].idxmin()]

        peak_date = pd.to_datetime(peak_row['DATE']).strftime('%Y-%m-%d')
        peak_value = float(peak_row['VALUE'])
        trough_date = pd.to_datetime(trough_row['DATE']).strftime('%Y-%m-%d')
        trough_value = float(trough_row['VALUE'])
        spread_value = peak_value - trough_value

        records.append({
            'region': str(region),
            'region_label': display_region_label(region, selected_index),
            'peak_date': peak_date,
            'peak_value': peak_value,
            'trough_date': trough_date,
            'trough_value': trough_value,
            'spread_value': spread_value
        })

    if not records:
        return '-'

    if sort_by == 'peak_desc':
        records = sorted(records, key=lambda x: (-x['peak_value'], x['region']))
    elif sort_by == 'trough_asc':
        records = sorted(records, key=lambda x: (x['trough_value'], x['region']))
    elif sort_by == 'spread_desc':
        records = sorted(records, key=lambda x: (-x['spread_value'], x['region']))
    else:
        if normalize_index_name(selected_index) == 'BDI':
            priority_map = {name: i for i, name in enumerate(BDI_REGION_PRIORITY)}
            records = sorted(records, key=lambda x: (priority_map.get(x['region'], 999), x['region']))
        else:
            records = sorted(records, key=lambda x: x['region'])

    rows = []
    for r in records:
        rows.append(f"""
        <tr>
          <td><span class="route-badge">{r['region_label']}</span></td>
          <td><span class="mini-badge peak">최고</span><span class="metric-date">{r['peak_date']}</span></td>
          <td class="metric-num peak-text">{r['peak_value']:,.2f}</td>
          <td><span class="mini-badge trough">최저</span><span class="metric-date">{r['trough_date']}</span></td>
          <td class="metric-num trough-text">{r['trough_value']:,.2f}</td>
          <td class="metric-num spread-text">{r['spread_value']:,.2f}</td>
        </tr>
        """)

    return f"""
    <div class="peaktrough-wrap">
      <table class="peaktrough-table">
        <thead>
          <tr>
            <th>항로</th>
            <th>최고 일자</th>
            <th>최고 값</th>
            <th>최저 일자</th>
            <th>최저 값</th>
            <th>스프레드</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
    </div>
    """


def make_plot(df_long, selected_regions, line_width=2.5, palette='Auto', user_color_map=None):
    palette = normalize_palette_name(palette)
    fig = go.Figure()

    if df_long is None or df_long.empty:
        fig.update_layout(
            template='plotly_white',
            height=560,
            margin=dict(l=20, r=20, t=20, b=40),
            xaxis=dict(tickfont=dict(size=15)),
            yaxis=dict(tickfont=dict(size=15), tickformat=',')
        )
        return fig

    index_name = df_long['INDEX_NAME'].iloc[0] if 'INDEX_NAME' in df_long.columns and not df_long.empty else DEFAULT_INDEX_NAME

    regions = selected_regions if selected_regions else sorted(
        df_long['REGION'].dropna().astype(str).unique().tolist()
    )

    regions = [
        r for r in regions
        if is_valid_region_for_index(r, index_name)
    ]

    user_color_map = user_color_map or {}
    color_map = build_final_color_map(regions, palette, user_color_map)

    for region in regions:
        sub = df_long[df_long['REGION'] == region].sort_values('DATE')
        if sub.empty:
            continue
        color = color_map.get(region)
        fig.add_trace(go.Scatter(
            x=sub['DATE'],
            y=sub['VALUE'],
            mode='lines',
            name=str(region),
            line=dict(color=color, width=line_width),
            hovertemplate='날짜=%{x|%Y-%m-%d}<br>항로=%{fullData.name}<br>지수=%{y:,.2f}<extra></extra>'
        ))

    fig.update_layout(
        template='plotly_white',
        height=620,
        margin=dict(l=20, r=20, t=20, b=40),
        xaxis_title='Date',
        yaxis_title='Index',
        hovermode='x unified',
        legend=dict(orientation='h', yanchor='bottom', y=1.02, x=0, font=dict(size=12)),
        xaxis=dict(tickfont=dict(size=15), title_font=dict(size=16)),
        yaxis=dict(tickfont=dict(size=15), title_font=dict(size=16), tickformat=',')
    )
    return fig


def make_download_response(df, filename, kind='csv', sheet_name='INDEX'):
    if kind == 'excel':
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name=sheet_name[:31])
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    return Response(
        df.to_csv(index=False, encoding='utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


HTML = r"""
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Shipping Index Dashboard</title>
<style>
:root {
  --bg: #eef3f8;
  --surface: #ffffff;
  --surface-2: #f8fafc;
  --border: #dbe4ee;
  --text: #1f2937;
  --muted: #64748b;
  --primary: #2563eb;
  --success: #16a34a;
  --shadow: 0 8px 28px rgba(15, 23, 42, 0.08);
}
* { box-sizing: border-box; }
body {
  font-family: 'Segoe UI', Arial, sans-serif;
  margin: 0;
  background: linear-gradient(180deg, #f7fafc 0%, var(--bg) 100%);
  color: var(--text);
}
.page-wrap {
  max-width: 1600px;
  margin: 0 auto;
  padding: 24px;
}
.layout {
  display: grid;
  grid-template-columns: 340px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.sidebar {
  position: sticky;
  top: 20px;
  background: var(--surface);
  border-radius: 20px;
  box-shadow: var(--shadow);
  padding: 22px 20px;
  border: 1px solid var(--border);
}
.main {
  min-width: 0;
}
.sidebar-section {
  padding-bottom: 18px;
  margin-bottom: 18px;
  border-bottom: 1px solid #e5edf5;
}
.sidebar-section:last-child {
  margin-bottom: 0;
  padding-bottom: 0;
  border-bottom: 0;
}
.sidebar-label {
  font-size: 12px;
  font-weight: 800;
  text-transform: uppercase;
  letter-spacing: .5px;
  color: #475569;
  margin-bottom: 10px;
}
.main-header {
  background: var(--surface);
  border-radius: 20px;
  box-shadow: var(--shadow);
  padding: 22px 24px;
  border: 1px solid var(--border);
  margin-bottom: 20px;
}
.main-title {
  margin: 0 0 10px 0;
  font-size: 32px;
  line-height: 1.15;
  color: #0f172a;
}
.meta {
  color: var(--muted);
  font-size: 13px;
  line-height: 1.8;
}
.meta strong { color: #334155; }
.meta a {
  color: var(--primary);
  text-decoration: none;
  font-weight: 700;
}
.meta a:hover { text-decoration: underline; }
.card {
  background: var(--surface);
  padding: 20px;
  border-radius: 18px;
  box-shadow: var(--shadow);
  border: 1px solid var(--border);
  margin-bottom: 20px;
}
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
}
.insight-card {
  background: var(--surface-2);
  border: 1px solid #e5edf5;
  border-radius: 14px;
  padding: 16px;
  min-height: 120px;
}
.insight-label {
  font-size: 12px;
  font-weight: 800;
  letter-spacing: .4px;
  color: #64748b;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.insight-value {
  font-size: 28px;
  font-weight: 800;
  color: #0f172a;
  margin-bottom: 6px;
}
.insight-desc {
  font-size: 13px;
  color: #475569;
  line-height: 1.5;
}
.section-title {
  font-size: 18px;
  font-weight: 800;
  color: #0f172a;
  margin: 0 0 14px 0;
}
.chart-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.toolbar-title {
  font-size: 16px;
  font-weight: 800;
  color: #334155;
}
.toolbar-subtitle {
  font-size: 12px;
  color: var(--muted);
  margin-top: 4px;
}
.download-icon-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.icon-btn {
  width: 38px;
  height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid #cbd5e1;
  background: white;
  color: #334155;
  border-radius: 10px;
  cursor: pointer;
  text-decoration: none;
  transition: all .18s ease;
}
.icon-btn:hover {
  background: var(--primary);
  color: white;
  border-color: var(--primary);
}
.icon-btn svg {
  width: 18px;
  height: 18px;
}
.action-group {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.btn {
  padding: 9px 14px;
  min-height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--primary);
  background: white;
  color: var(--primary);
  border-radius: 10px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
  text-decoration: none;
  transition: all .18s ease;
}
.btn:hover {
  background: var(--primary);
  color: white;
}
.btn.success {
  border-color: var(--success);
  color: var(--success);
}
.btn.success:hover {
  background: var(--success);
  color: white;
}
.btn.secondary {
  border-color: #cbd5e1;
  color: #475569;
}
.btn.secondary:hover {
  background: #475569;
  color: white;
  border-color: #475569;
}
input[type="date"],
input[type="number"],
select {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid #cbd5e1;
  border-radius: 10px;
  min-height: 40px;
  background: white;
  color: #0f172a;
}
input[type="color"] {
  padding: 2px;
  border: 1px solid #cbd5e1;
  border-radius: 8px;
  min-height: 34px;
  background: white;
  width: 52px;
}
.field-block {
  margin-bottom: 14px;
}
.field-label {
  display: block;
  margin-bottom: 6px;
  font-size: 12px;
  font-weight: 700;
  color: #334155;
}
.year-range {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.range-values {
  font-size: 12px;
  color: #64748b;
}
.color-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  max-height: 260px;
  overflow: auto;
  border: 1px solid #e2e8f0;
  padding: 10px;
  border-radius: 10px;
  background: #f8fafc;
}
.color-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.color-item label {
  font-size: 12px;
  color: #334155;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 78%;
}
.index-tabs {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.index-tab {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 42px;
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid #cbd5e1;
  background: #ffffff;
  color: #334155;
  font-size: 14px;
  font-weight: 800;
  text-decoration: none;
  transition: all .18s ease;
}
.index-tab:hover {
  background: #eff6ff;
  border-color: #93c5fd;
  color: #1d4ed8;
}
.index-tab.active {
  background: var(--primary);
  border-color: var(--primary);
  color: #ffffff;
  box-shadow: 0 6px 16px rgba(37, 99, 235, 0.24);
}
.peaktrough-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.peaktrough-wrap { width: 100%; overflow: auto; margin-top: 8px; }
.peaktrough-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 12px; }
.peaktrough-table th,
.peaktrough-table td {
  border: 1px solid #e5e7eb;
  padding: 10px 12px;
  white-space: nowrap;
  vertical-align: middle;
}
.peaktrough-table th {
  background: #f1f5f9;
  color: #334155;
  font-weight: 700;
  text-align: left;
}
.peaktrough-table td {
  background: #ffffff;
  color: #334155;
}
.route-badge {
  display: inline-flex;
  align-items: center;
  padding: 4px 10px;
  border-radius: 999px;
  background: #e0f2fe;
  color: #075985;
  font-weight: 700;
  font-size: 12px;
}
.mini-badge {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  margin-right: 8px;
}
.mini-badge.peak { background: #fee2e2; color: #b91c1c; }
.mini-badge.trough { background: #dbeafe; color: #1d4ed8; }
.metric-date { color: #475569; font-size: 12px; }
.metric-num { text-align: right; font-weight: 800; font-variant-numeric: tabular-nums; }
.peak-text { color: #b91c1c; }
.trough-text { color: #1d4ed8; }
.spread-text { color: #7c3aed; }
.notice-box {
  padding: 12px 14px;
  border-radius: 12px;
  background: #eff6ff;
  color: #1e3a8a;
  border: 1px solid #bfdbfe;
  font-size: 13px;
  font-weight: 700;
}
@media (max-width: 1100px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .sidebar {
    position: static;
  }
}
@media (max-width: 768px) {
  .page-wrap {
    padding: 16px;
  }
  .main-title {
    font-size: 26px;
  }
}
</style>
</head>
<body>
<div class="page-wrap">
  <div class="layout">
    <aside class="sidebar">
      <form method="get">
        <input type="hidden" name="index_name" value="{{ selected_index }}">
        <input type="hidden" name="prev_palette" value="{{ palette }}">

        <div class="sidebar-section">
          <div class="sidebar-label">Index</div>
          <div class="index-tabs">
            <a href="/?index_name=SCFI" class="index-tab {% if selected_index == 'SCFI' %}active{% endif %}">SCFI</a>
            <a href="/?index_name=BDI" class="index-tab {% if selected_index == 'BDI' %}active{% endif %}">BDI</a>
          </div>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-label">Actions</div>
          <div class="action-group">
            <button class="btn" type="submit">Apply</button>
            <a class="btn secondary" href="/?index_name={{ selected_index }}">Reset</a>
            <a class="btn success" href="{{ refresh_url }}">Refresh</a>
          </div>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-label">Date Range</div>

          <div class="field-block">
            <label class="field-label">시작일</label>
            <input type="date" name="start_date" value="{{ start_date or '' }}">
          </div>

          <div class="field-block">
            <label class="field-label">종료일</label>
            <input type="date" name="end_date" value="{{ end_date or '' }}">
          </div>

          <div class="field-block">
            <label class="field-label">연도 범위</label>
            <div class="year-range">
              <input type="range" id="year_start" min="{{ year_min }}" max="{{ year_max }}" value="{{ year_start }}" oninput="syncYears()">
              <input type="range" id="year_end" min="{{ year_min }}" max="{{ year_max }}" value="{{ year_end }}" oninput="syncYears()">
            </div>
            <div class="range-values">선택 연도: <span id="yearLabel">{{ year_start }} ~ {{ year_end }}</span></div>
            <input type="hidden" id="year_start_hidden" name="year_start" value="{{ year_start }}">
            <input type="hidden" id="year_end_hidden" name="year_end" value="{{ year_end }}">
          </div>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-label">Routes</div>
          <div class="field-block">
            <label class="field-label">항로 복수 선택</label>
            <select name="regions" multiple size="10" style="min-height:220px;">
              {% for region in all_regions %}
                <option value="{{ region }}" {% if region in selected_regions %}selected{% endif %}>{{ region_display_map.get(region, region) }}</option>
              {% endfor %}
            </select>
          </div>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-label">Chart Style</div>

          <div class="field-block">
            <label class="field-label">선 두께</label>
            <input type="number" name="line_width" value="{{ line_width }}" step="0.1" min="0.1" max="10">
          </div>

          <div class="field-block">
            <label class="field-label">자동 색상 테마</label>
            <select name="palette">
              {% for p in palette_options %}
              <option value="{{ p }}" {% if palette == p %}selected{% endif %}>{{ p }}</option>
              {% endfor %}
            </select>
          </div>

          <div class="field-block">
            <label class="field-label">항로별 색상</label>
            <div class="color-grid">
              {% for region in selected_regions_for_color %}
              <div class="color-item">
                <label title="{{ region_display_map.get(region, region) }}">{{ region_display_map.get(region, region) }}</label>
                <input type="color" name="{{ color_keys[region] }}" value="{{ color_values.get(region, auto_color_map.get(region, '#636EFA')) }}">
              </div>
              {% endfor %}
            </div>
          </div>
        </div>

        <div class="sidebar-section">
          <div class="sidebar-label">Highlight Sorting</div>
          <div class="field-block">
            <label class="field-label">최고/최저 표 정렬</label>
            <select name="peak_sort">
              <option value="region" {% if peak_sort == 'region' %}selected{% endif %}>항로명</option>
              <option value="peak_desc" {% if peak_sort == 'peak_desc' %}selected{% endif %}>최고값 내림차순</option>
              <option value="trough_asc" {% if peak_sort == 'trough_asc' %}selected{% endif %}>최저값 오름차순</option>
              <option value="spread_desc" {% if peak_sort == 'spread_desc' %}selected{% endif %}>스프레드 내림차순</option>
            </select>
          </div>
        </div>
      </form>
    </aside>

    <main class="main">
      <div class="main-header">
        <h1 class="main-title">Shipping Index Dashboard</h1>
        <div class="meta">
          <strong>Source:</strong>
          <a href="https://www.ksg.co.kr/" target="_blank" rel="noopener noreferrer">Korea Shipping Gazette</a>
          &nbsp;|&nbsp;
          <strong>Current Index:</strong> {{ selected_index }}
          &nbsp;|&nbsp;
          <strong>Last sync:</strong> {{ last_sync }}
          &nbsp;|&nbsp;
          <strong>Total points:</strong> {{ total_points }}
        </div>
      </div>

      {% if refresh_message %}
      <div class="card">
        <div class="notice-box">{{ refresh_message }}</div>
      </div>
      {% endif %}

      <div class="card">
        <div class="section-title">Current Highlights</div>
        <div class="metrics-grid">
          <div class="insight-card">
            <div class="insight-label">조회 시작일</div>
            <div class="insight-value">{{ shown_start }}</div>
            <div class="insight-desc">필터링 후 표시되는 데이터의 시작일입니다.</div>
          </div>
          <div class="insight-card">
            <div class="insight-label">조회 종료일</div>
            <div class="insight-value">{{ shown_end }}</div>
            <div class="insight-desc">필터링 후 표시되는 데이터의 종료일입니다.</div>
          </div>
          <div class="insight-card">
            <div class="insight-label">선택 항로 수</div>
            <div class="insight-value">{{ region_count }}</div>
            <div class="insight-desc">복수 선택된 항로 수입니다.</div>
          </div>
          <div class="insight-card">
            <div class="insight-label">표시 데이터 수</div>
            <div class="insight-value">{{ shown_rows }}</div>
            <div class="insight-desc">차트와 하이라이트에 반영된 데이터 수입니다.</div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="chart-toolbar">
          <div>
            <div class="toolbar-title">항로별 비교 차트</div>
            <div class="toolbar-subtitle">선택한 복수 항로를 한 화면에서 비교합니다.</div>
          </div>
          <div class="download-icon-group">
            <a class="icon-btn" href="{{ download_csv_url }}" title="CSV 다운로드" aria-label="CSV 다운로드">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M12 3v12"></path>
                <path d="M7 10l5 5 5-5"></path>
                <path d="M5 21h14"></path>
              </svg>
            </a>
            <a class="icon-btn" href="{{ download_xlsx_url }}" title="XLSX 다운로드" aria-label="XLSX 다운로드">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="4" y="3" width="16" height="18" rx="2"></rect>
                <path d="M9 9l6 6"></path>
                <path d="M15 9l-6 6"></path>
              </svg>
            </a>
            <a class="icon-btn" href="{{ download_chart_html_url }}" title="차트 HTML 다운로드" aria-label="차트 HTML 다운로드">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M8 9l-4 3 4 3"></path>
                <path d="M16 9l4 3-4 3"></path>
              </svg>
            </a>
            <a class="icon-btn" href="{{ download_chart_png_url }}" title="차트 PNG 다운로드" aria-label="차트 PNG 다운로드">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <rect x="3" y="5" width="18" height="14" rx="2"></rect>
                <circle cx="8.5" cy="10" r="1.5"></circle>
                <path d="M21 15l-5-5L5 19"></path>
              </svg>
            </a>
          </div>
        </div>
        {{ chart_html|safe }}
      </div>

      <div class="card">
        <div class="peaktrough-toolbar">
          <div class="section-title" style="margin:0;">항로별 최고점 / 최저점 / 스프레드</div>
          <div class="toolbar-subtitle">선택 정렬: {{ peak_sort_label }}</div>
        </div>
        <div class="insight-desc">{{ peak_summary|safe }}</div>
      </div>
    </main>
  </div>
</div>

<script>
function syncYears() {
  let s = parseInt(document.getElementById('year_start').value);
  let e = parseInt(document.getElementById('year_end').value);
  if (s > e) {
    const t = s; s = e; e = t;
  }
  document.getElementById('year_start_hidden').value = s;
  document.getElementById('year_end_hidden').value = e;
  document.getElementById('yearLabel').innerText = s + ' ~ ' + e;
}
syncYears();
</script>
</body>
</html>
"""


def current_filter_params():
    selected_index = normalize_index_name(request.args.get('index_name', DEFAULT_INDEX_NAME))
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    year_start = request.args.get('year_start', '').strip()
    year_end = request.args.get('year_end', '').strip()

    regions = request.args.getlist('regions')
    regions = [
        str(r).strip()
        for r in regions
        if is_valid_region_for_index(r, selected_index)
    ]

    line_width = request.args.get('line_width', '1.2').strip()
    palette = normalize_palette_name(request.args.get('palette', 'Auto'))
    prev_palette = normalize_palette_name(request.args.get('prev_palette', palette))
    peak_sort = request.args.get('peak_sort', 'region').strip() or 'region'

    years = []
    if year_start.isdigit() and year_end.isdigit():
        a, b = int(year_start), int(year_end)
        if a > b:
            a, b = b, a
        years = list(range(a, b + 1))

    return selected_index, start_date, end_date, year_start, year_end, regions, years, line_width, palette, prev_palette, peak_sort


def build_query_string(selected_index, start_date, end_date, year_start, year_end, regions, line_width, palette, color_map, peak_sort='region'):
    params = [('index_name', selected_index)]

    if start_date:
        params.append(('start_date', start_date))
    if end_date:
        params.append(('end_date', end_date))
    if str(year_start):
        params.append(('year_start', str(year_start)))
    if str(year_end):
        params.append(('year_end', str(year_end)))

    valid_regions = [
        r for r in regions
        if is_valid_region_for_index(r, selected_index)
    ]

    for r in valid_regions:
        params.append(('regions', r))

    if line_width:
        params.append(('line_width', str(line_width)))
    if palette:
        params.append(('palette', palette))

    params.append(('prev_palette', palette))

    if peak_sort:
        params.append(('peak_sort', peak_sort))

    for region, color in color_map.items():
        if color and is_valid_region_for_index(region, selected_index):
            params.append((safe_color_key(region), color))

    return urlencode(params, doseq=True)


@app.route('/refresh')
def refresh():
    selected_index = normalize_index_name(request.args.get('index_name', DEFAULT_INDEX_NAME))
    result = run_incremental_sync(RUNTIME_CONFIG, index_name=selected_index)

    if result.get('ok'):
        return redirect(url_for('index', msg='동기화 성공', index_name=selected_index))

    return redirect(url_for('index', msg=f"동기화 실패: {result.get('error', '')}", index_name=selected_index))


@app.route('/download')
def download():
    fmt = request.args.get('format', 'csv')
    mode = request.args.get('mode', 'data')

    selected_index, start_date, end_date, year_start, year_end, regions, years, line_width, palette, prev_palette, peak_sort = current_filter_params()

    long_df = load_long_df_from_db(index_name=selected_index)
    filtered = filter_long_df(
        long_df,
        start_date=start_date or None,
        end_date=end_date or None,
        years=years,
        regions=regions or None
    )

    selected_regions_for_color = regions if regions else sort_regions_for_display(
        long_df['REGION'].dropna().astype(str).unique().tolist(),
        selected_index
    ) if not long_df.empty else []

    selected_regions_for_color = [
        r for r in selected_regions_for_color
        if is_valid_region_for_index(r, selected_index)
    ]

    user_color_map = extract_user_color_map(selected_regions_for_color, request)
    if palette != prev_palette:
        user_color_map = {}

    today = datetime.now().strftime('%Y%m%d')

    try:
        lw = max(0.1, float(line_width))
    except Exception:
        lw = 1.2

    if mode == 'chart_html':
        fig = make_plot(filtered, regions, line_width=lw, palette=palette, user_color_map=user_color_map)
        html = fig.to_html(full_html=True, include_plotlyjs='cdn')
        return Response(
            html,
            mimetype='text/html; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename={selected_index}_Chart_{today}.html'}
        )

    if mode == 'chart_png':
        fig = make_plot(filtered, regions, line_width=lw, palette=palette, user_color_map=user_color_map)
        try:
            img = pio.to_image(fig, format='png', width=1600, height=900, scale=2)
            return Response(
                img,
                mimetype='image/png',
                headers={'Content-Disposition': f'attachment; filename={selected_index}_Chart_{today}.png'}
            )
        except Exception as e:
            return Response(f'PNG export failed: {e}', mimetype='text/plain; charset=utf-8')

    if filtered.empty:
        filtered = pd.DataFrame(columns=['DATE', 'YEAR', 'REGION', 'VALUE', 'INDEX_NAME'])

    out = filtered.copy()
    out['DATE'] = pd.to_datetime(out['DATE'], errors='coerce').dt.strftime('%Y-%m-%d')

    if fmt == 'excel':
        return make_download_response(out, f'{selected_index}_Filtered_{today}.xlsx', 'excel', sheet_name='Filtered')

    return make_download_response(out, f'{selected_index}_Filtered_{today}.csv', 'csv', sheet_name='Filtered')


@app.route('/')
def index():
    selected_index, start_date, end_date, year_start_raw, year_end_raw, selected_regions, years, line_width, palette, prev_palette, peak_sort = current_filter_params()

    meta = get_last_update_meta(index_name=selected_index)
    long_df = load_long_df_from_db(index_name=selected_index)
    refresh_message = request.args.get('msg', '')

    if long_df.empty and RUNTIME_CONFIG and RUNTIME_CONFIG.init_refresh:
        result = run_incremental_sync(RUNTIME_CONFIG, index_name=selected_index)
        long_df = load_long_df_from_db(index_name=selected_index)
        refresh_message = '초기 동기화 완료' if result.get('ok') else f"초기 동기화 실패: {result.get('error', '')}"

    if long_df.empty:
        year_min = datetime.now().year
        year_max = datetime.now().year
        year_start = year_min
        year_end = year_max
        line_width = '1.2'
        palette = 'Auto'
        peak_sort = 'region'
        peak_sort_label = '항로명'
        fig = make_plot(pd.DataFrame(), [], line_width=1.2, palette=palette, user_color_map={})

        refresh_qs = urlencode({'index_name': selected_index})

        return render_template_string(
            HTML,
            selected_index=selected_index,
            last_sync='-',
            total_points=0,
            refresh_message=refresh_message,
            refresh_url=f'/refresh?{refresh_qs}',
            start_date='',
            end_date='',
            year_min=year_min,
            year_max=year_max,
            year_start=year_start,
            year_end=year_end,
            all_regions=[],
            region_display_map={},
            selected_regions=[],
            selected_regions_for_color=[],
            color_keys={},
            color_values={},
            auto_color_map={},
            line_width=line_width,
            palette=palette,
            palette_options=PALETTE_OPTIONS,
            peak_sort=peak_sort,
            peak_sort_label=peak_sort_label,
            shown_start='-',
            shown_end='-',
            region_count=0,
            shown_rows=0,
            peak_summary='-',
            download_csv_url=f'/download?index_name={selected_index}&format=csv',
            download_xlsx_url=f'/download?index_name={selected_index}&format=excel',
            download_chart_html_url=f'/download?index_name={selected_index}&mode=chart_html',
            download_chart_png_url=f'/download?index_name={selected_index}&mode=chart_png',
            chart_html=fig.to_html(full_html=False, include_plotlyjs='cdn')
        )

    all_regions = sort_regions_for_display(
        long_df['REGION'].dropna().astype(str).unique().tolist(),
        selected_index
    )

    selected_regions = [
        r for r in selected_regions
        if is_valid_region_for_index(r, selected_index)
    ]

    year_min = int(long_df['YEAR'].min())
    year_max = int(long_df['YEAR'].max())

    year_start = int(year_start_raw) if str(year_start_raw).isdigit() else year_min
    year_end = int(year_end_raw) if str(year_end_raw).isdigit() else year_max

    if year_start > year_end:
        year_start, year_end = year_end, year_start

    years = list(range(year_start, year_end + 1))

    filtered = filter_long_df(
        long_df,
        start_date=start_date or None,
        end_date=end_date or None,
        years=years,
        regions=selected_regions or None
    )

    selected_regions_for_color = selected_regions if selected_regions else all_regions
    selected_regions_for_color = [
        r for r in selected_regions_for_color
        if is_valid_region_for_index(r, selected_index)
    ]

    user_color_map = extract_user_color_map(selected_regions_for_color, request)
    if palette != prev_palette:
        user_color_map = {}

    auto_color_map = build_auto_color_map(selected_regions_for_color, palette)
    color_keys = {r: safe_color_key(r) for r in selected_regions_for_color}
    region_display_map = {r: display_region_label(r, selected_index) for r in all_regions}

    shown_start = filtered['DATE'].min().strftime('%Y-%m-%d') if not filtered.empty else '-'
    shown_end = filtered['DATE'].max().strftime('%Y-%m-%d') if not filtered.empty else '-'

    peak_summary = build_peak_trough_html(
        filtered,
        selected_index=selected_index,
        selected_regions=selected_regions,
        sort_by=peak_sort
    )

    sort_label_map = {
        'region': '항로명',
        'peak_desc': '최고값 내림차순',
        'trough_asc': '최저값 오름차순',
        'spread_desc': '스프레드 내림차순'
    }
    peak_sort_label = sort_label_map.get(peak_sort, '항로명')

    try:
        lw = max(0.1, float(line_width))
        line_width = str(lw)
    except Exception:
        lw = 1.2
        line_width = '1.2'

    fig = make_plot(
        filtered,
        selected_regions,
        line_width=lw,
        palette=palette,
        user_color_map=user_color_map
    )

    full_color_map_for_qs = {}
    for r in selected_regions_for_color:
        full_color_map_for_qs[r] = user_color_map.get(r, auto_color_map.get(r))

    qs = build_query_string(
        selected_index,
        start_date,
        end_date,
        year_start,
        year_end,
        selected_regions,
        line_width,
        palette,
        full_color_map_for_qs,
        peak_sort=peak_sort
    )
    qs = ('&' + qs) if qs else ''

    refresh_qs = urlencode({'index_name': selected_index})
    last_sync = meta.get('finished_at') if meta else '-'

    return render_template_string(
        HTML,
        selected_index=selected_index,
        last_sync=last_sync or '-',
        total_points=len(long_df),
        refresh_message=refresh_message,
        refresh_url=f'/refresh?{refresh_qs}',
        start_date=start_date,
        end_date=end_date,
        year_min=year_min,
        year_max=year_max,
        year_start=year_start,
        year_end=year_end,
        all_regions=all_regions,
        region_display_map=region_display_map,
        selected_regions=selected_regions,
        selected_regions_for_color=selected_regions_for_color,
        color_keys=color_keys,
        color_values=user_color_map,
        auto_color_map=auto_color_map,
        line_width=line_width,
        palette=palette,
        palette_options=PALETTE_OPTIONS,
        peak_sort=peak_sort,
        peak_sort_label=peak_sort_label,
        shown_start=shown_start,
        shown_end=shown_end,
        region_count=len(selected_regions) if selected_regions else len(all_regions),
        shown_rows=len(filtered),
        peak_summary=peak_summary,
        download_csv_url=f'/download?format=csv{qs}',
        download_xlsx_url=f'/download?format=excel{qs}',
        download_chart_html_url=f'/download?mode=chart_html{qs}',
        download_chart_png_url=f'/download?mode=chart_png{qs}',
        chart_html=fig.to_html(full_html=False, include_plotlyjs='cdn')
    )


def open_browser(host, port):
    webbrowser.open_new(f'http://{host}:{port}')


if __name__ == '__main__':
    args = parse_args()
    RUNTIME_CONFIG = args

    cleanup_invalid_regions()

    print('[System] Starting Shipping Index Dashboard...')
    print(f'[System] Database file: {DB_FILE}')
    print(f'[System] Supported indexes: {SUPPORTED_INDEXES}')

    if args.open_browser:
        Timer(1.5, open_browser, args=(args.host, args.port)).start()

    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)