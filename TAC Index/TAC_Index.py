# -*- coding: utf-8 -*-
import sys
import os
import time
import datetime
import warnings
import webbrowser
from threading import Timer
from io import BytesIO
import sqlite3
import hashlib

warnings.filterwarnings('ignore')

from flask import Flask, render_template_string, Response, request
import pandas as pd
import plotly.graph_objects as go
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)

# TAC Index URL 설정
TAC_INDEX_URL = 'https://www.tacindex.com/'
MEM_CACHE = {}
CACHE_MINUTES = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# 루트 디렉토리 경로 (MI/)
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, '..'))
DB_FILE = os.path.join(BASE_DIR, 'TAC_Index.db')


def get_cache(key):
    item = MEM_CACHE.get(key)
    if not item:
        return None
    expire_at, value = item
    if datetime.datetime.now() >= expire_at:
        MEM_CACHE.pop(key, None)
        return None
    return value


def set_cache(key, value, minutes=CACHE_MINUTES):
    MEM_CACHE[key] = (datetime.datetime.now() + datetime.timedelta(minutes=minutes), value)


def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    # Master Table (스냅샷 수집 이력)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tac_master (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT,
            data_hash TEXT UNIQUE,
            latest_date TEXT,
            overall_index REAL,
            weekly_change REAL,
            row_count INTEGER
        )
    ''')
    # Detail Table (노선별 상세 지수 수치)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tac_detail (
            snapshot_id INTEGER,
            route_code TEXT,
            route_name TEXT,
            index_value REAL,
            weekly_change REAL,
            monthly_change REAL
        )
    ''')
    conn.commit()
    return conn


def make_data_hash(df, latest_date):
    if df.empty:
        return hashlib.sha256(str(latest_date).encode('utf-8')).hexdigest()
    payload = df.fillna('').astype(str).to_csv(index=False) + str(latest_date)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def save_snapshot(bundle):
    try:
        conn = get_db_conn()
        df = bundle.get('df', pd.DataFrame())
        summary = bundle.get('summary', {})

        if df.empty:
            print("[Warning] TAC Index data is empty. Skipping DB save.")
            conn.close()
            return None

        latest_date = summary.get('latest_date', datetime.datetime.now().strftime('%Y-%m-%d'))
        data_hash = make_data_hash(df, latest_date)
        cur = conn.cursor()

        # 이미 저장된 데이터와 동일한 해시(Hash)가 존재하는 경우 Skip
        cur.execute('SELECT id FROM tac_master WHERE data_hash = ?', (data_hash,))
        row = cur.fetchone()
        if row:
            print(f"[Info] No new updates detected for TAC Index. Skipping DB save (Existing ID: {row[0]}).")
            conn.close()
            return row[0]

        captured_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(
            '''INSERT INTO tac_master (captured_at, data_hash, latest_date, overall_index, weekly_change, row_count)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (
                captured_at,
                data_hash,
                str(latest_date),
                float(summary.get('overall_index', 0.0) or 0.0),
                float(summary.get('weekly_change', 0.0) or 0.0),
                len(df)
            )
        )
        snapshot_id = cur.lastrowid

        rows = []
        for _, r in df.iterrows():
            rows.append((
                snapshot_id,
                str(r.get('route_code', '')),
                str(r.get('route_name', '')),
                float(r.get('index_value', 0.0) or 0.0),
                float(r.get('weekly_change', 0.0) or 0.0),
                float(r.get('monthly_change', 0.0) or 0.0)
            ))

        cur.executemany(
            '''INSERT INTO tac_detail (snapshot_id, route_code, route_name, index_value, weekly_change, monthly_change)
               VALUES (?, ?, ?, ?, ?, ?)''',
            rows
        )
        conn.commit()
        conn.close()
        print(f"[Success] New TAC Index snapshot saved. Snapshot ID: {snapshot_id}")
        return snapshot_id
    except Exception as e:
        print(f"[Error] Failed to save TAC Index snapshot: {e}")
        return None


def fetch_tac_index(force=False):
    cache_key = 'tac_index_bundle'
    if not force:
        cached = get_cache(cache_key)
        if cached is not None:
            return cached

    chrome_options = Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

    driver = webdriver.Chrome(options=chrome_options)
    try:
        driver.get(TAC_INDEX_URL)
        time.sleep(8)  # DOM 렌더링 대기

        payload = driver.execute_script(r"""
            var out = {summary:{}, rows:[]};
            try {
                var txt = document.body.innerText || '';
                
                // TAC Index 대표 수치 및 일자 정규식 파싱
                var dateMatch = txt.match(/([0-9]{2}\s+[A-Za-z]{3}\s+[0-9]{4})/i) || txt.match(/([0-9]{4}-[0-9]{2}-[0-9]{2})/);
                if (dateMatch) out.summary.latest_date = dateMatch[1];
                
                var indexMatch = txt.match(/Index\s*:\s*([0-9,.]+)/i) || txt.match(/Overall\s*([0-9,.]+)/i);
                if (indexMatch) out.summary.overall_index = indexMatch[1].replace(/,/g, '');

                var changeMatch = txt.match(/([+-]?[0-9,.]+)%/i);
                if (changeMatch) out.summary.weekly_change = changeMatch[1].replace(/,/g, '');
            } catch(e) {}

            try {
                // 테이블 데이터 파싱
                var trs = document.querySelectorAll('table tr');
                trs.forEach(function(tr){
                    var tds = tr.querySelectorAll('td, th');
                    if(tds.length >= 3){
                        var code = tds[0].innerText.trim();
                        var val = tds[1].innerText.trim().replace(/,/g, '');
                        var chg = tds[2].innerText.trim().replace(/,/g, '');
                        if(code && !isNaN(parseFloat(val))){
                            out.rows.push({
                                route_code: code,
                                route_name: code,
                                index_value: parseFloat(val),
                                weekly_change: parseFloat(chg) || 0.0,
                                monthly_change: 0.0
                            });
                        }
                    }
                });
            } catch(e) {}
            return out;
        """)
    finally:
        driver.quit()

    df = pd.DataFrame(payload.get('rows', []))
    summary = payload.get('summary', {})

    if summary.get('latest_date') is None:
        summary['latest_date'] = datetime.datetime.now().strftime('%Y-%m-%d')

    bundle = {'df': df, 'summary': summary}
    set_cache(cache_key, bundle)
    return bundle


def get_history_df():
    conn = get_db_conn()
    q = "SELECT captured_at, latest_date, overall_index, weekly_change FROM tac_master ORDER BY id ASC"
    df = pd.read_sql_query(q, conn)
    conn.close()
    return df


HTML_TEMPLATE = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TAC Air Freight Index Dashboard</title>
<style>
:root { --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0; --primary: #0284c7; --text: #0f172a; --muted: #64748b; }
body { font-family: 'Segoe UI', Arial, sans-serif; max-width: 1200px; margin: 24px auto; padding: 20px; background: var(--bg); color: var(--text); }
.header { border-bottom: 3px solid var(--primary); padding-bottom: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; }
h1 { margin: 0; color: var(--primary); font-size: 26px; }
.meta { font-size: 13px; color: var(--muted); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.04); }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
.val-box { background: #f1f5f9; padding: 16px; border-radius: 8px; }
.val-label { font-size: 12px; color: var(--muted); font-weight: 700; text-transform: uppercase; }
.val-num { font-size: 26px; font-weight: 800; margin-top: 4px; color: #0369a1; }
table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
th, td { border: 1px solid var(--border); padding: 8px 12px; text-align: left; }
th { background: #f8fafc; color: #334155; }
.btn-back { display: inline-block; padding: 6px 12px; background: #e2e8f0; color: #334155; text-decoration: none; border-radius: 6px; font-size: 13px; font-weight: 600; }
</style>
</head>
<body>
  <div class="header">
    <div>
      <h1>✈️ TAC Air Freight Rate Index</h1>
      <div class="meta">Source: TAC Index &nbsp;|&nbsp; Updated: {{ fetched_at }}</div>
    </div>
    <a href="./index.html" class="btn-back">← Portal Home</a>
  </div>

  <div class="card">
    <div class="grid">
      <div class="val-box">
        <div class="val-label">Latest Overall Index</div>
        <div class="val-num">{{ overall_index }}</div>
      </div>
      <div class="val-box">
        <div class="val-label">Weekly Change</div>
        <div class="val-num">{{ weekly_change }}%</div>
      </div>
      <div class="val-box">
        <div class="val-label">Data Date</div>
        <div class="val-num" style="font-size: 18px; margin-top: 10px;">{{ latest_date }}</div>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Historical Trend</h3>
    {{ chart_html|safe }}
  </div>

  <div class="card">
    <h3>Route Breakdown</h3>
    <div>{{ table_html|safe }}</div>
  </div>
</body>
</html>
"""


@app.route('/')
def index():
    fetched_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    bundle = fetch_tac_index(force=True)
    save_snapshot(bundle)

    df = bundle.get('df', pd.DataFrame())
    summary = bundle.get('summary', {})

    history_df = get_history_df()
    fig = go.Figure()
    if not history_df.empty:
        fig.add_trace(go.Scatter(
            x=history_df['latest_date'],
            y=history_df['overall_index'],
            mode='lines+markers',
            name='Overall Index',
            line=dict(color='#0284c7', width=3)
        ))
    fig.update_layout(
        template='plotly_white',
        height=380,
        margin=dict(l=20, r=20, t=20, b=40),
        xaxis_title="Date",
        yaxis_title="Index Value"
    )

    chart_html = fig.to_html(full_html=False, include_plotlyjs='cdn')
    table_html = df.to_html(index=False, border=0) if not df.empty else "<p>No detailed route data available.</p>"

    return render_template_string(
        HTML_TEMPLATE,
        fetched_at=fetched_at,
        overall_index=summary.get('overall_index', '-'),
        weekly_change=summary.get('weekly_change', '-'),
        latest_date=summary.get('latest_date', '-'),
        chart_html=chart_html,
        table_html=table_html
    )


def run_crawler_only():
    print("[System] Running TAC Index Crawler...")
    bundle = fetch_tac_index(force=True)
    snapshot_id = save_snapshot(bundle)

    # 상위 루트 디렉토리에 TAC_Index.html 출력
    output_html_path = os.path.join(ROOT_DIR, 'TAC_Index.html')
    with app.test_request_context('/'):
        html_content = index()
        with open(output_html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
    print(f"[System] Generated {output_html_path} successfully!")


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--cron':
        run_crawler_only()
    else:
        app.run(host='127.0.0.1', port=8082, debug=False)
