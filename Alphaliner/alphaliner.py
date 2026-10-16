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
ALPHALINER_URL = 'https://alphaliner.axsmarine.com/PublicTop100/'
MEM_CACHE = {}
CACHE_MINUTES = 30
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, 'alphaliner_history.db')

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

def fmt_million(x):
    if pd.isna(x):
        return ''
    return f'{x/1_000_000:.1f}M'

def fmt_num(x):
    if pd.isna(x):
        return ''
    return f'{int(round(float(x))):,}'

def fmt_pct(x):
    if pd.isna(x):
        return ''
    return f'{float(x):.1f}%'

def fmt_delta(x, pct=False):
    if pd.isna(x):
        return '-'
    x = float(x)
    sign = '+' if x > 0 else ''
    if pct:
        return f'{sign}{x:.1f}%p'
    return f'{sign}{int(round(x)):,}'

def delta_span(value, pct=False, rank_mode=False):
    if pd.isna(value):
        return '<span class="delta-zero">-</span>'
    n = float(value)
    if rank_mode:
        if n > 0:
            return f'<span class="delta-pos">▲ Up {int(round(n))}</span>'
        if n < 0:
            return f'<span class="delta-neg">▼ Down {abs(int(round(n)))}</span>'
        return '<span class="delta-zero">-</span>'
    cls = 'delta-pos' if n > 0 else ('delta-neg' if n < 0 else 'delta-zero')
    arrow = '▲ ' if n > 0 else ('▼ ' if n < 0 else '')
    return f'<span class="{cls}">{arrow}{fmt_delta(n, pct=pct)}</span>'

def safe_num(x):
    try:
        if x is None or x == '':
            return 0.0
        return float(str(x).replace(',', ''))
    except Exception:
        return 0.0

def get_db_conn():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot_master (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at TEXT, snapshot_hash TEXT UNIQUE, active_ships TEXT, total_teu TEXT, trans_atlantic TEXT, trans_pacific TEXT, feast_europe TEXT, row_count INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot_detail (snapshot_id INTEGER, rank INTEGER, operator TEXT, total_teu REAL, market_share REAL, total_ships REAL, owned_teu REAL, owned_ships REAL, chartered_teu REAL, chartered_ships REAL, chartered_percent REAL, orderbook_teu REAL, orderbook_ships REAL, orderbook_percent REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS access_log (log_id INTEGER PRIMARY KEY AUTOINCREMENT, accessed_at TEXT, snapshot_id INTEGER, refresh_flag INTEGER, client_ip TEXT, user_agent TEXT)")
    conn.commit()
    return conn

def make_snapshot_hash(bundle):
    df = bundle['df'].copy()
    key_cols = [c for c in ['Rank', 'Operator', 'Total_TEU', 'Market_Share', 'Total_Ships', 'Owned_TEU', 'Owned_Ships', 'Chartered_TEU', 'Chartered_Ships', 'Chartered_Percent', 'Orderbook_TEU', 'Orderbook_Ships', 'Orderbook_Percent'] if c in df.columns]
    payload = df[key_cols].fillna('').astype(str).to_csv(index=False)
    payload += str(bundle.get('summary', {})) + str(bundle.get('regional', {}))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()

def save_snapshot(bundle):
    try:
        conn = get_db_conn()
        summary = bundle.get('summary', {})
        regional = bundle.get('regional', {})
        df = bundle['df'].copy()

        if df.empty or not summary.get('totalTEU') or summary.get('totalTEU') == 'None':
            print("[Warning] Parsed data is incomplete. Skipping DB save.")
            conn.close()
            return None

        snapshot_hash = make_snapshot_hash(bundle)
        cur = conn.cursor()

        cur.execute('SELECT snapshot_id FROM snapshot_master WHERE snapshot_hash = ? AND total_teu IS NOT NULL AND total_teu != "None"', (snapshot_hash,))
        row = cur.fetchone()
        if row:
            conn.close()
            return row[0]

        captured_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(
            'INSERT INTO snapshot_master (captured_at, snapshot_hash, active_ships, total_teu, trans_atlantic, trans_pacific, feast_europe, row_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (
                captured_at,
                snapshot_hash,
                str(summary.get('totalActiveShips', '')),
                str(summary.get('totalTEU', '')),
                str(regional.get('transAtlantic', '')),
                str(regional.get('transPacific', '')),
                str(regional.get('feastEurope', '')),
                len(df)
            )
        )
        snapshot_id = cur.lastrowid
        rows = []
        for _, r in df.iterrows():
            rows.append((
                snapshot_id,
                int(r.get('Rank')) if pd.notna(r.get('Rank')) else None,
                str(r.get('Operator')) if pd.notna(r.get('Operator')) else '',
                float(r.get('Total_TEU')) if pd.notna(r.get('Total_TEU')) else None,
                float(r.get('Market_Share')) if pd.notna(r.get('Market_Share')) else None,
                float(r.get('Total_Ships')) if pd.notna(r.get('Total_Ships')) else None,
                float(r.get('Owned_TEU')) if pd.notna(r.get('Owned_TEU')) else None,
                float(r.get('Owned_Ships')) if pd.notna(r.get('Owned_Ships')) else None,
                float(r.get('Chartered_TEU')) if pd.notna(r.get('Chartered_TEU')) else None,
                float(r.get('Chartered_Ships')) if pd.notna(r.get('Chartered_Ships')) else None,
                float(r.get('Chartered_Percent')) if pd.notna(r.get('Chartered_Percent')) else None,
                float(r.get('Orderbook_TEU')) if pd.notna(r.get('Orderbook_TEU')) else None,
                float(r.get('Orderbook_Ships')) if pd.notna(r.get('Orderbook_Ships')) else None,
                float(r.get('Orderbook_Percent')) if pd.notna(r.get('Orderbook_Percent')) else None
            ))
        cur.executemany(
            'INSERT INTO snapshot_detail (snapshot_id, rank, operator, total_teu, market_share, total_ships, owned_teu, owned_ships, chartered_teu, chartered_ships, chartered_percent, orderbook_teu, orderbook_ships, orderbook_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            rows
        )
        conn.commit()
        conn.close()
        return snapshot_id
    except Exception as e:
        print(f"[Error] Failed to save snapshot: {e}")
        return None

def log_access(snapshot_id, refresh_flag, client_ip='', user_agent=''):
    try:
        conn = get_db_conn()
        conn.execute(
            'INSERT INTO access_log (accessed_at, snapshot_id, refresh_flag, client_ip, user_agent) VALUES (?, ?, ?, ?, ?)',
            (
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                snapshot_id,
                1 if refresh_flag else 0,
                client_ip,
                (user_agent or '')[:500]
            )
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

def load_snapshot_detail(snapshot_id):
    if not snapshot_id:
        return pd.DataFrame()
    conn = get_db_conn()
    q = "SELECT rank as Rank, operator as Operator, total_teu as Total_TEU, market_share as Market_Share, total_ships as Total_Ships, owned_teu as Owned_TEU, owned_ships as Owned_Ships, chartered_teu as Chartered_TEU, chartered_ships as Chartered_Ships, chartered_percent as Chartered_Percent, orderbook_teu as Orderbook_TEU, orderbook_ships as Orderbook_Ships, orderbook_percent as Orderbook_Percent FROM snapshot_detail WHERE snapshot_id = ? ORDER BY rank"
    df = pd.read_sql_query(q, conn, params=(snapshot_id,))
    conn.close()
    return df

def get_snapshot_meta(snapshot_id):
    if not snapshot_id:
        return None
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('SELECT captured_at, active_ships, total_teu FROM snapshot_master WHERE snapshot_id = ?', (snapshot_id,))
    row = cur.fetchone()
    conn.close()
    return {'captured_at': row[0], 'active_ships': row[1], 'total_teu': row[2]} if row else None

def get_previous_snapshot_id(current_snapshot_id):
    if not current_snapshot_id:
        return None
    meta = get_snapshot_meta(current_snapshot_id)
    if not meta:
        return None
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute('''
        SELECT m.snapshot_id
        FROM snapshot_master m
        WHERE m.captured_at < ?
          AND m.total_teu IS NOT NULL
          AND m.total_teu != ''
          AND m.total_teu != 'None'
          AND (SELECT COUNT(*) FROM snapshot_detail d WHERE d.snapshot_id = m.snapshot_id) >= 50
        ORDER BY m.captured_at DESC, m.snapshot_id DESC
        LIMIT 1
    ''', (meta['captured_at'],))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def build_snapshot_delta(current_df, previous_df):
    if current_df is None or current_df.empty or previous_df is None or previous_df.empty:
        return pd.DataFrame()
    merged = current_df.merge(previous_df, on='Operator', how='left', suffixes=('_current', '_previous'))
    cols = ['Rank', 'Total_TEU', 'Market_Share', 'Total_Ships', 'Owned_TEU', 'Owned_Ships', 'Chartered_TEU', 'Chartered_Ships', 'Chartered_Percent', 'Orderbook_TEU', 'Orderbook_Ships', 'Orderbook_Percent']
    for c in cols:
        if f'{c}_current' in merged.columns:
            merged[f'{c}_current'] = pd.to_numeric(merged[f'{c}_current'], errors='coerce')
        if f'{c}_previous' in merged.columns:
            merged[f'{c}_previous'] = pd.to_numeric(merged[f'{c}_previous'], errors='coerce')
    merged['Delta_TEU'] = merged['Total_TEU_current'].fillna(0) - merged['Total_TEU_previous'].fillna(0)
    merged['Delta_Market_Share'] = merged['Market_Share_current'].fillna(0) - merged['Market_Share_previous'].fillna(0)
    merged['Delta_Ships'] = merged['Total_Ships_current'].fillna(0) - merged['Total_Ships_previous'].fillna(0)
    merged['Rank_Change'] = merged['Rank_previous'].fillna(merged['Rank_current']) - merged['Rank_current']
    out = merged[['Rank_current', 'Operator', 'Total_TEU_current', 'Total_TEU_previous', 'Delta_TEU', 'Market_Share_current', 'Market_Share_previous', 'Delta_Market_Share', 'Total_Ships_current', 'Total_Ships_previous', 'Delta_Ships', 'Rank_Change']].copy()
    out.columns = ['Rank', 'Operator', 'Current_TEU', 'Previous_TEU', 'Delta_TEU', 'Current_Market_Share', 'Previous_Market_Share', 'Delta_Market_Share', 'Current_Ships', 'Previous_Ships', 'Delta_Ships', 'Rank_Change']
    return out.sort_values('Rank').reset_index(drop=True)

def get_snapshot_id_by_mode(current_snapshot_id, compare_mode='previous', custom_date=None):
    if compare_mode == 'previous':
        return get_previous_snapshot_id(current_snapshot_id)
    conn = get_db_conn()
    cur = conn.cursor()
    current_meta = get_snapshot_meta(current_snapshot_id)
    current_captured = current_meta['captured_at'] if current_meta else datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    valid_condition = "AND total_teu IS NOT NULL AND total_teu != '' AND total_teu != 'None' AND (SELECT COUNT(*) FROM snapshot_detail d WHERE d.snapshot_id = snapshot_master.snapshot_id) >= 50"

    if compare_mode == '1m':
        target = (datetime.datetime.strptime(current_captured, '%Y-%m-%d %H:%M:%S') - datetime.timedelta(days=30)).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(f'''
            SELECT snapshot_id FROM snapshot_master
            WHERE captured_at <= ? {valid_condition}
            ORDER BY captured_at DESC LIMIT 1
        ''', (target,))
    elif compare_mode == '3m':
        target = (datetime.datetime.strptime(current_captured, '%Y-%m-%d %H:%M:%S') - datetime.timedelta(days=90)).strftime('%Y-%m-%d %H:%M:%S')
        cur.execute(f'''
            SELECT snapshot_id FROM snapshot_master
            WHERE captured_at <= ? {valid_condition}
            ORDER BY captured_at DESC LIMIT 1
        ''', (target,))
    elif compare_mode == 'custom' and custom_date:
        try:
            target = datetime.datetime.strptime(custom_date, '%Y-%m-%d').strftime('%Y-%m-%d 23:59:59')
            cur.execute(f'''
                SELECT snapshot_id FROM snapshot_master
                WHERE captured_at <= ? {valid_condition}
                ORDER BY captured_at DESC LIMIT 1
            ''', (target,))
        except Exception:
            conn.close()
            return get_previous_snapshot_id(current_snapshot_id)
    else:
        conn.close()
        return get_previous_snapshot_id(current_snapshot_id)

    row = cur.fetchone()
    conn.close()
    return row[0] if row else get_previous_snapshot_id(current_snapshot_id)

def fetch_bundle(force=False):
    cache_key = 'alphaliner_simple_bundle'
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
        driver.get(ALPHALINER_URL)
        time.sleep(10)

        payload = driver.execute_script(r"""
            var out = {summary:{}, regional:{}, rows:[]};
            try {
                var txt = document.body.innerText || '';

                var activeMatch = txt.match(/([0-9,]+)\s+active\s+ships/i);
                if (activeMatch) {
                    out.summary.totalActiveShips = activeMatch[1];
                }

                var teuMatch = txt.match(/([0-9,]+)\s+TEU/i);
                if (teuMatch) {
                    out.summary.totalTEU = teuMatch[1];
                }

                var regBlock = txt.match(/Trans-Atlantic[\s\S]*?Trans-Pacific[\s\S]*?Feast-Europe[\s\S]*?([0-9,]+)\s*TEU[\s\S]*?([0-9,]+)\s*TEU[\s\S]*?([0-9,]+)\s*TEU/i);
                if (regBlock) {
                    out.regional.transAtlantic = regBlock[1];
                    out.regional.transPacific = regBlock[2];
                    out.regional.feastEurope = regBlock[3];
                } else {
                    var matches = [...txt.matchAll(/([0-9,]+)\s*TEU/gi)].map(function(m){ return m[1]; });
                    if (matches.length >= 4) {
                        out.regional.transAtlantic = matches[1];
                        out.regional.transPacific = matches[2];
                        out.regional.feastEurope = matches[3];
                    }
                }
            } catch(e) {}

            try {
                var target = null;
                Ext.StoreManager.items.forEach(function(s){
                    try { if (!target && s.storeId === 'Top100description') target = s; } catch(e) {}
                });
                if (target) {
                    for (var i=0; i<target.getCount(); i++) out.rows.push(target.getAt(i).data);
                }
            } catch(e) {}
            return out;
        """)
    finally:
        driver.quit()

    raw = pd.DataFrame(payload['rows'])
    df = pd.DataFrame({
        'Rank': range(1, len(raw) + 1),
        'Operator': raw.get('operator'),
        'Total_TEU': raw.get('totalTeu'),
        'Market_Share': raw.get('percent'),
        'Total_Ships': raw.get('totalShips'),
        'Owned_TEU': raw.get('ownedTeu'),
        'Owned_Ships': raw.get('ownedShips'),
        'Chartered_TEU': raw.get('chartTeu'),
        'Chartered_Ships': raw.get('chartShips'),
        'Chartered_Percent': raw.get('chartProcent'),
        'Orderbook_TEU': raw.get('orderTeu'),
        'Orderbook_Ships': raw.get('orderShips'),
        'Orderbook_Percent': raw.get('orderProcents'),
    }).sort_values('Rank').reset_index(drop=True)

    summary_data = payload.get('summary', {})

    if (not summary_data.get('totalTEU') or summary_data.get('totalTEU') == 'None') and not df.empty:
        calc_teu = pd.to_numeric(df['Total_TEU'], errors='coerce').sum()
        if calc_teu > 0:
            summary_data['totalTEU'] = f"{int(calc_teu):,}"

    if (not summary_data.get('totalActiveShips') or summary_data.get('totalActiveShips') == 'None') and not df.empty:
        calc_ships = pd.to_numeric(df['Total_Ships'], errors='coerce').sum()
        if calc_ships > 0:
            summary_data['totalActiveShips'] = f"{int(calc_ships):,}"

    bundle = {
        'df': df,
        'summary': summary_data,
        'regional': payload.get('regional', {})
    }
    set_cache(cache_key, bundle)
    return bundle

def make_download_response(df, filename, kind='csv'):
    if kind == 'excel':
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Current')
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

def make_compare_download_response(current_df, previous_df, delta_df, filename, kind='csv'):
    if kind == 'excel':
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            current_df.to_excel(writer, index=False, sheet_name='Current')
            previous_df.to_excel(writer, index=False, sheet_name='Previous')
            delta_df.to_excel(writer, index=False, sheet_name='Delta')
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )
    return Response(
        delta_df.to_csv(index=False, encoding='utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )

def fig_html(fig, first=False):
    return fig.to_html(full_html=False, include_plotlyjs='cdn' if first else False)

HTML = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alphaliner Top 100 Carriers Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-2: #f1f5f9;
  --border: #e2e8f0;
  --border-dark: #cbd5e1;
  --text: #0f172a;
  --text-muted: #64748b;
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --accent-navy: #1e293b;
  --pos: #dc2626;
  --neg: #2563eb;
  --zero: #64748b;
  --shadow-sm: 0 1px 3px rgba(0,0,0,0.05);
  --shadow: 0 4px 12px -2px rgba(0,0,0,0.06), 0 2px 6px -1px rgba(0,0,0,0.04);
  --radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
  padding: 24px;
  max-width: 1440px;
  margin: 0 auto;
}
.top-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text-muted);
  text-decoration: none;
  font-size: 13px;
  font-weight: 600;
  padding: 6px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  transition: all 0.2s ease;
}
.back-link:hover {
  color: var(--primary);
  border-color: var(--primary);
  background: #eff6ff;
}
.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  margin-bottom: 24px;
  gap: 16px;
  flex-wrap: wrap;
}
.title-group h1 {
  font-size: 26px;
  font-weight: 800;
  color: var(--text);
  letter-spacing: -0.5px;
}
.meta {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 4px;
}
.meta strong { color: var(--accent-navy); }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  margin-bottom: 20px;
  box-shadow: var(--shadow-sm);
}
.card-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--text);
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.toolbar-wrap {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
.filter-form {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}
.select-input, .date-input {
  padding: 7px 12px;
  border: 1px solid var(--border-dark);
  border-radius: 6px;
  font-size: 13px;
  background: var(--surface);
  color: var(--text);
  outline: none;
}
.btn-apply {
  padding: 7px 14px;
  background: var(--primary);
  color: white;
  border: none;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.2s;
}
.btn-apply:hover { background: var(--primary-hover); }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 12px;
}
.kpi-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.kpi-label {
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.kpi-value {
  font-size: 22px;
  font-weight: 800;
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
  margin-bottom: 4px;
}
.kpi-desc {
  font-size: 12px;
  color: var(--text-muted);
}
.delta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.delta-card {
  background: #fafafa;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.delta-main {
  font-size: 20px;
  font-weight: 800;
  font-family: 'JetBrains Mono', monospace;
  margin: 4px 0;
}
.delta-pos { color: var(--pos); font-weight: 700; }
.delta-neg { color: var(--neg); font-weight: 700; }
.delta-zero { color: var(--zero); font-weight: 600; }
.quick-facts {
  background: #eff6ff;
  border: 1px solid #bfdbfe;
  border-radius: 8px;
  padding: 14px 18px;
  font-size: 13px;
  color: #1e3a8a;
  line-height: 1.6;
  margin-top: 14px;
}
.quick-facts b { color: #172554; }
.table-container {
  width: 100%;
  overflow-x: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  text-align: right;
}
th, td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: middle;
}
th {
  background: var(--surface-2);
  color: #334155;
  font-weight: 700;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.3px;
}
th:first-child, td:first-child,
th:nth-child(2), td:nth-child(2) {
  text-align: left;
}
tbody tr:hover { background: #f8fafc; }
.detail-super th {
  background: #e2e8f0;
  color: #0f172a;
  font-weight: 800;
  text-align: center;
  border-right: 1px solid #cbd5e1;
}
.detail-sub th {
  background: #f1f5f9;
  color: #475569;
  text-align: center;
}
.footer {
  text-align: center;
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 32px;
  padding-bottom: 12px;
}
@media (max-width: 768px) {
  body { padding: 14px; }
  .header { flex-direction: column; align-items: flex-start; }
  .kpi-grid, .delta-grid { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

  <div class="top-nav">
    <a href="../index.html" class="back-link">
      <span>←</span> Back to Dashboard
    </a>
    <span class="meta">Market Intelligence / <strong>02 Sea</strong></span>
  </div>

  <div class="header">
    <div class="title-group">
      <h1>Alphaliner Top 100 Global Carriers</h1>
      <div class="meta">
        <strong>Source:</strong> Alphaliner Public Top 100 &nbsp;|&nbsp;
        <strong>Captured:</strong> {{ fetched_at }}
        {% if previous_snapshot_at %}
        &nbsp;|&nbsp; <strong>Base Snapshot:</strong> {{ previous_snapshot_at }}
        {% endif %}
      </div>
    </div>
  </div>

  <div class="card">
    <div class="toolbar-wrap">
      <div>
        <div style="font-weight: 700; font-size: 14px;">시계열 비교 기준 설정</div>
        <div style="font-size: 12px; color: var(--text-muted);">현재 스냅샷과 비교할 과거 시점을 선택합니다.</div>
      </div>
      <form method="get" class="filter-form">
        <select name="compare_mode" class="select-input">
          <option value="previous" {% if compare_mode == 'previous' %}selected{% endif %}>직전 스냅샷 (Previous)</option>
          <option value="1m" {% if compare_mode == '1m' %}selected{% endif %}>1개월 전 (1 Month ago)</option>
          <option value="3m" {% if compare_mode == '3m' %}selected{% endif %}>3개월 전 (3 Months ago)</option>
          <option value="custom" {% if compare_mode == 'custom' %}selected{% endif %}>직접 지정 (Custom)</option>
        </select>
        <input type="date" name="custom_date" value="{{ custom_date or '' }}" class="date-input">
        <button type="submit" class="btn-apply">적용</button>
      </form>
    </div>
    <div style="margin-top: 10px; font-size: 12px; color: var(--text-muted);">
      현재 비교 기준: <strong>{{ compare_label }}</strong>
    </div>
  </div>

  <div class="card">
    <div class="card-title">글로벌 선대 및 주요 항로 공급량 현황</div>
    <div class="kpi-grid">
      <div class="kpi-card">
        <div class="kpi-label">Active Ships</div>
        <div class="kpi-value">{{ active_ships }}</div>
        <div class="kpi-desc">운항 중인 총 컨테이너 선박 수</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Total TEU</div>
        <div class="kpi-value">{{ total_teu_global }}</div>
        <div class="kpi-desc">글로벌 총 공급 선복량</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Trans-Atlantic</div>
        <div class="kpi-value">{{ trans_atlantic }}</div>
        <div class="kpi-desc">대서양 항로 주간 선복량</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Trans-Pacific</div>
        <div class="kpi-value">{{ trans_pacific }}</div>
        <div class="kpi-desc">태평양(미주) 항로 주간 선복량</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">FEast-Europe</div>
        <div class="kpi-value">{{ feast_europe }}</div>
        <div class="kpi-desc">극동-유럽 항로 주간 선복량</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">스냅샷 변동치 분석 (Delta Summary)</div>
    {% if has_previous %}
    <div class="delta-grid">
      <div class="delta-card">
        <div class="kpi-label">Total TEU Change</div>
        <div class="delta-main {{ total_teu_delta_class }}">{{ total_teu_delta }}</div>
        <div class="kpi-desc">총 선복량 증감</div>
      </div>
      <div class="delta-card">
        <div class="kpi-label">Active Ships Change</div>
        <div class="delta-main {{ active_ships_delta_class }}">{{ active_ships_delta }}</div>
        <div class="kpi-desc">운항 선박 수 증감</div>
      </div>
      <div class="delta-card">
        <div class="kpi-label">최대 선복 증가 선사</div>
        <div class="delta-main delta-pos">{{ biggest_gain_name }}</div>
        <div class="kpi-desc">{{ biggest_gain_value|safe }}</div>
      </div>
      <div class="delta-card">
        <div class="kpi-label">최대 선복 감소 선사</div>
        <div class="delta-main delta-neg">{{ biggest_loss_name }}</div>
        <div class="kpi-desc">{{ biggest_loss_value|safe }}</div>
      </div>
    </div>
    {% else %}
    <div style="font-size: 13px; color: var(--text-muted);">비교 가능한 과거 스냅샷 데이터가 없습니다.</div>
    {% endif %}
  </div>

  <div class="card">
    <div class="card-title">상위 10대 선사 공급 선복량 (Top 10 Capacity)</div>
    <div style="margin-bottom: 8px;">
      {{ chart1_en|safe }}
    </div>
    <div class="quick-facts">
      Top 3 선사 점유율: <b>{{ top3_share }}</b> &nbsp;|&nbsp; Top 5 선사 점유율: <b>{{ top5_share }}</b><br>
      누적 점유율 50% 도달 선사 수: <b>{{ top50_count }}개사 (합산 점유율 {{ top50_share }})</b><br>
      상위 50% 과점 선사군: <b>{{ top50_names }}</b>
    </div>
  </div>

  <div class="card">
    <div class="card-title">상위 선사 점유율 요약</div>
    <div class="table-container">
      {{ share_table_en|safe }}
    </div>
  </div>

  <div class="card">
    <div class="card-title">선대 세부 구조 (Owned vs Chartered vs Orderbook)</div>
    <div class="table-container">
      {{ detail_table_en|safe }}
    </div>
  </div>

  <div class="card">
    <div class="card-title">선사별 순위 및 선복량 변동 상세 (Current vs Previous)</div>
    {% if has_previous %}
    <div class="table-container">
      {{ delta_table_html|safe }}
    </div>
    {% else %}
    <div style="font-size: 13px; color: var(--text-muted);">비교 데이터가 없습니다.</div>
    {% endif %}
  </div>

  <div class="footer">
    Market Intelligence Dashboard &copy; 2026. Data automatically captured and generated via Alphaliner.
  </div>

</body>
</html>
"""

@app.route('/download')
def download():
    fmt = request.args.get('format', 'csv')
    mode = request.args.get('mode', 'current')
    refresh = request.args.get('refresh', '0') == '1'
    compare_mode = request.args.get('compare_mode', 'previous')
    custom_date = request.args.get('custom_date', '')

    bundle = fetch_bundle(force=refresh)
    snapshot_id = save_snapshot(bundle)
    log_access(snapshot_id, refresh, request.remote_addr or '', request.headers.get('User-Agent', ''))

    df = bundle['df']
    previous_snapshot_id = get_snapshot_id_by_mode(snapshot_id, compare_mode=compare_mode, custom_date=custom_date)
    previous_df = load_snapshot_detail(previous_snapshot_id) if previous_snapshot_id else pd.DataFrame()
    delta_df = build_snapshot_delta(df, previous_df)

    today = datetime.datetime.now().strftime('%Y%m%d')

    if mode == 'compare':
        if fmt == 'excel':
            return make_compare_download_response(df, previous_df, delta_df, f'Alphaliner_Top100_Compare_{today}.xlsx', 'excel')
        return make_compare_download_response(df, previous_df, delta_df, f'Alphaliner_Top100_Compare_{today}.csv', 'csv')

    if fmt == 'excel':
        return make_download_response(df, f'Alphaliner_Top100_{today}.xlsx', 'excel')
    return make_download_response(df, f'Alphaliner_Top100_{today}.csv', 'csv')

@app.route('/')
def index():
    fetched_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    refresh = request.args.get('refresh', '0') == '1'
    compare_mode = request.args.get('compare_mode', 'previous')
    custom_date = request.args.get('custom_date', '')

    bundle = fetch_bundle(force=refresh)
    snapshot_id = save_snapshot(bundle)
    log_access(snapshot_id, refresh, request.remote_addr or '', request.headers.get('User-Agent', ''))

    df = bundle['df']
    summary = bundle['summary']
    regional = bundle['regional']

    previous_snapshot_id = get_snapshot_id_by_mode(snapshot_id, compare_mode=compare_mode, custom_date=custom_date)
    previous_meta = get_snapshot_meta(previous_snapshot_id) if previous_snapshot_id else None
    previous_snapshot_at = previous_meta['captured_at'] if previous_meta else None

    compare_label = 'Previous snapshot' if compare_mode == 'previous' else ('1 month ago' if compare_mode == '1m' else ('3 months ago' if compare_mode == '3m' else (f'Custom date: {custom_date}' if custom_date else 'Custom date')))
    previous_df = load_snapshot_detail(previous_snapshot_id) if previous_snapshot_id else pd.DataFrame()
    delta_df = build_snapshot_delta(df, previous_df)
    has_previous = not delta_df.empty

    top10 = df.nsmallest(10, 'Rank').copy()
    fig1 = go.Figure()
    fig1.add_trace(go.Bar(
        x=top10['Operator'],
        y=top10['Total_TEU'],
        name='Total TEU',
        marker_color='#2563eb',
        text=[fmt_million(x) for x in top10['Total_TEU']],
        textposition='outside',
        cliponaxis=False,
        textfont=dict(size=11, family='Pretendard, sans-serif')
    ))
    fig1.update_layout(
        template='plotly_white',
        height=500,
        margin=dict(l=20, r=20, t=30, b=60),
        yaxis=dict(title='Total TEU', gridcolor='#f1f5f9'),
        xaxis=dict(categoryorder='array', categoryarray=top10['Operator'].tolist(), tickangle=-30),
        showlegend=False,
        hoverlabel=dict(namelength=-1)
    )

    share_df = top10[['Rank', 'Operator', 'Market_Share']].copy()
    share_df['Market_Share'] = share_df['Market_Share'].apply(fmt_pct)
    share_table_en = share_df.to_html(index=False, border=0)

    detail_cols = ['Rank', 'Operator', 'Total_TEU', 'Total_Ships', 'Owned_TEU', 'Owned_Ships', 'Chartered_TEU', 'Chartered_Ships', 'Chartered_Percent', 'Orderbook_TEU', 'Orderbook_Ships', 'Orderbook_Percent']
    detail = df[detail_cols].copy()

    for c in ['Total_TEU', 'Owned_TEU', 'Chartered_TEU', 'Orderbook_TEU']:
        detail[c] = detail[c].apply(fmt_num)
    for c in ['Total_Ships', 'Owned_Ships', 'Chartered_Ships', 'Orderbook_Ships']:
        detail[c] = detail[c].apply(fmt_num)
    for c in ['Chartered_Percent', 'Orderbook_Percent']:
        detail[c] = detail[c].apply(fmt_pct)

    rows = []
    rows.append('<table border="0">')
    rows.append('<thead>')
    rows.append('<tr class="detail-super"><th rowspan="2">Rank</th><th rowspan="2">Operator</th><th colspan="2">Total</th><th colspan="2">Owned</th><th colspan="3">Chartered</th><th colspan="3">Orderbook</th></tr>')
    rows.append('<tr class="detail-sub"><th>TEU</th><th>Ships</th><th>TEU</th><th>Ships</th><th>TEU</th><th>Ships</th><th>%</th><th>TEU</th><th>Ships</th><th>%</th></tr>')
    rows.append('</thead><tbody>')
    for _, r in detail.iterrows():
        op = '' if pd.isna(r['Operator']) else str(r['Operator'])
        rows.append(
            f'<tr><td>{r["Rank"]}</td><td title="{op}">{op}</td><td>{r["Total_TEU"]}</td><td>{r["Total_Ships"]}</td><td>{r["Owned_TEU"]}</td><td>{r["Owned_Ships"]}</td><td>{r["Chartered_TEU"]}</td><td>{r["Chartered_Ships"]}</td><td>{r["Chartered_Percent"]}</td><td>{r["Orderbook_TEU"]}</td><td>{r["Orderbook_Ships"]}</td><td>{r["Orderbook_Percent"]}</td></tr>'
        )
    rows.append('</tbody></table>')
    detail_table_en = ''.join(rows)

    top3_share = f"{top10['Market_Share'].fillna(0).head(3).sum():.1f}%"
    top5_share = f"{top10['Market_Share'].fillna(0).head(5).sum():.1f}%"

    share_run = df[['Operator', 'Market_Share']].copy()
    share_run['Market_Share'] = share_run['Market_Share'].fillna(0)
    share_run['cum_share'] = share_run['Market_Share'].cumsum()
    above50 = share_run[share_run['cum_share'] >= 50]
    top50_df = share_run.iloc[:above50.index[0] + 1] if not above50.empty else share_run.copy()
    top50_count = len(top50_df)
    top50_share = f"{top50_df['Market_Share'].sum():.1f}%"
    top50_names = ', '.join(top50_df['Operator'].astype(str).tolist())

    total_teu_delta = '-'
    total_teu_delta_class = 'delta-zero'
    active_ships_delta = '-'
    active_ships_delta_class = 'delta-zero'
    biggest_gain_name = '-'
    biggest_gain_value = '-'
    biggest_loss_name = '-'
    biggest_loss_value = '-'
    delta_table_html = ''

    if has_previous:
        current_total_teu = pd.to_numeric(df['Total_TEU'], errors='coerce').fillna(0).sum()
        previous_total_teu = pd.to_numeric(previous_df['Total_TEU'], errors='coerce').fillna(0).sum()
        total_teu_delta_num = current_total_teu - previous_total_teu
        total_teu_delta = fmt_delta(total_teu_delta_num)
        total_teu_delta_class = 'delta-pos' if total_teu_delta_num > 0 else ('delta-neg' if total_teu_delta_num < 0 else 'delta-zero')

        current_active = safe_num(summary.get('totalActiveShips', '0'))
        previous_active = safe_num(previous_meta['active_ships']) if previous_meta else 0
        active_delta_num = current_active - previous_active
        active_ships_delta = fmt_delta(active_delta_num)
        active_ships_delta_class = 'delta-pos' if active_delta_num > 0 else ('delta-neg' if active_delta_num < 0 else 'delta-zero')

        gain_row = delta_df.sort_values('Delta_TEU', ascending=False).iloc[0]
        loss_row = delta_df.sort_values('Delta_TEU', ascending=True).iloc[0]
        biggest_gain_name = str(gain_row['Operator'])
        biggest_gain_value = delta_span(gain_row['Delta_TEU'])
        biggest_loss_name = str(loss_row['Operator'])
        biggest_loss_value = delta_span(loss_row['Delta_TEU'])

        delta_view = delta_df[['Rank', 'Operator', 'Current_TEU', 'Previous_TEU', 'Delta_TEU', 'Current_Market_Share', 'Previous_Market_Share', 'Delta_Market_Share', 'Rank_Change']].copy()
        delta_view['Current_TEU'] = delta_view['Current_TEU'].apply(fmt_num)
        delta_view['Previous_TEU'] = delta_view['Previous_TEU'].apply(fmt_num)
        delta_view['Delta_TEU'] = delta_df['Delta_TEU'].apply(lambda x: delta_span(x))
        delta_view['Current_Market_Share'] = delta_view['Current_Market_Share'].apply(fmt_pct)
        delta_view['Previous_Market_Share'] = delta_view['Previous_Market_Share'].apply(fmt_pct)
        delta_view['Delta_Market_Share'] = delta_df['Delta_Market_Share'].apply(lambda x: delta_span(x, pct=True))
        delta_view['Rank_Change'] = delta_df['Rank_Change'].apply(lambda x: delta_span(x, rank_mode=True))
        delta_view.columns = ['Rank', 'Operator', 'Current TEU', 'Previous TEU', 'Δ TEU', 'Current Share', 'Previous Share', 'Δ Share', 'Rank Move']
        delta_table_html = delta_view.to_html(index=False, border=0, escape=False)

    return render_template_string(
        HTML,
        fetched_at=fetched_at,
        previous_snapshot_at=previous_snapshot_at,
        compare_mode=compare_mode,
        custom_date=custom_date,
        compare_label=compare_label,
        has_previous=has_previous,
        active_ships=summary.get('totalActiveShips', '-') or '-',
        total_teu_global=summary.get('totalTEU', '-') or '-',
        trans_atlantic=regional.get('transAtlantic', '-') or '-',
        trans_pacific=regional.get('transPacific', '-') or '-',
        feast_europe=regional.get('feastEurope', '-') or '-',
        top3_share=top3_share,
        top5_share=top5_share,
        top50_count=top50_count,
        top50_share=top50_share,
        top50_names=top50_names,
        chart1_en=fig_html(fig1, True),
        share_table_en=share_table_en,
        detail_table_en=detail_table_en,
        total_teu_delta=total_teu_delta,
        total_teu_delta_class=total_teu_delta_class,
        active_ships_delta=active_ships_delta,
        active_ships_delta_class=active_ships_delta_class,
        biggest_gain_name=biggest_gain_name,
        biggest_gain_value=biggest_gain_value,
        biggest_loss_name=biggest_loss_name,
        biggest_loss_value=biggest_loss_value,
        delta_table_html=delta_table_html
    )

def open_browser():
    webbrowser.open_new("http://127.0.0.1:8081")

def run_crawler_only():
    print("[System] Running in CLI mode: Fetching latest Alphaliner data...")
    bundle = fetch_bundle(force=True)
    snapshot_id = save_snapshot(bundle)
    if snapshot_id:
        print(f"[System] Crawling completed successfully. Snapshot ID: {snapshot_id}")
    else:
        print("[System] Snapshot already exists or failed to save.")

    print("[System] Generating static index.html for GitHub Pages...")
    with app.test_request_context('/'):
        html_content = index()
        output_path = os.path.join(BASE_DIR, 'index.html')
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
    print(f"[System] Static index.html generated successfully at: {output_path}")

if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--cron':
        run_crawler_only()
    else:
        print('[System] Starting dashboard server...')
        print(f'[System] Database file: {DB_FILE}')
        Timer(1.5, open_browser).start()
        app.run(host='127.0.0.1', port=8081, debug=False, use_reloader=False)
