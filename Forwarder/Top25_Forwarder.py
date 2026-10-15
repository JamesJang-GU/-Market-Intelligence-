# -*- coding: utf-8 -*-
import argparse
import datetime as dt
import importlib.util
import json
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from io import StringIO
from pathlib import Path


def setup_environment():
    required = {
        "requests": "requests",
        "pandas": "pandas",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
        "flask": "flask",
    }
    for import_name, pip_name in required.items():
        if importlib.util.find_spec(import_name) is not None:
            continue
        print(f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...", flush=True)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name])
        except Exception:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "--break-system-packages"])


setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template_string, request, url_for

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_URL = "https://www.3plogistics.com/3pl-market-info-resources/3pl-market-information/aas-top-25-global-freight-forwarders-list/"
DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
EXPECTED_COLUMNS = ["rank", "provider", "hq", "revenue", "teu", "air_tons"]

app = Flask(__name__)
RUNTIME_CONFIG = None


def log(msg):
    print(msg, flush=True)


def clean_number(value):
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0
    s = s.replace(",", "")
    s = "".join(ch for ch in s if ch.isdigit() or ch == ".")
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def normalize_provider_name(value):
    return " ".join(str(value).strip().split())


def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(host="127.0.0.1", preferred=8080):
    candidates = [preferred, 8080, 8081, 8082, 8000, 5000, 8501, 7860, 3000]
    seen = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if is_port_available(host, p):
            return p
    for p in range(8000, 8101):
        if p not in seen and is_port_available(host, p):
            return p
    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다.")


def wait_and_open_browser(url, port, timeout=20):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                webbrowser.open_new(url)
                return
        except Exception:
            time.sleep(0.4)


def select_best_table(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")
    if not tables:
        raise RuntimeError("웹페이지에서 table 태그를 찾지 못했습니다.")
    candidates = []
    for idx, table in enumerate(tables):
        try:
            df = pd.read_html(StringIO(str(table)))[0]
        except Exception:
            continue
        if df is None or df.empty:
            continue
        text = (" ".join(map(str, df.columns)) + " " + " ".join(map(str, df.head(5).values.ravel()))).lower()
        score = 0
        for key in ["rank", "provider", "headquarters", "hq", "revenue", "teu", "air"]:
            if key in text:
                score += 5
        if df.shape[1] >= 6:
            score += 3
        if df.shape[0] >= 20:
            score += 5
        if "dhl" in text or "kuehne" in text or "db schenker" in text:
            score += 3
        candidates.append((score, idx, df))
    if not candidates:
        raise RuntimeError("pandas로 읽을 수 있는 테이블을 찾지 못했습니다.")
    candidates.sort(key=lambda x: (x[0], -x[1]), reverse=True)
    best_score, _, best_df = candidates[0]
    if best_score <= 0:
        raise RuntimeError("Top 25 포워더 테이블을 식별하지 못했습니다.")
    return best_df


def fetch_top_25_data(url=DEFAULT_URL):
    log("[FETCH] 최신 A&A Top 25 Global Freight Forwarders 데이터 수집 중...")
    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    raw_df = select_best_table(response.text)
    if isinstance(raw_df.columns, pd.MultiIndex):
        raw_df.columns = [" ".join([str(x) for x in col if str(x) != "nan"]).strip() for col in raw_df.columns]
    else:
        raw_df.columns = [str(c).strip() for c in raw_df.columns]
    if raw_df.shape[1] < 6:
        raise RuntimeError(f"예상보다 컬럼 수가 적습니다. 현재 shape={raw_df.shape}")
    df = raw_df.iloc[:, :6].copy()
    df.columns = EXPECTED_COLUMNS
    for col in ["rank", "revenue", "teu", "air_tons"]:
        df[col] = df[col].apply(clean_number)
    df["rank"] = df["rank"].astype(int)
    df["provider"] = df["provider"].apply(normalize_provider_name)
    df["hq"] = df["hq"].astype(str).str.strip()
    df["date"] = dt.date.today().isoformat()
    df["source_url"] = url
    df = df[df["provider"].astype(str).str.len() > 0]
    df = df.sort_values("rank").reset_index(drop=True)
    if df.empty:
        raise RuntimeError("정제 후 데이터가 비어 있습니다.")
    log(f"[FETCH] 수집 완료: {len(df)} rows")
    return df


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS forwarders (
            date TEXT NOT NULL,
            rank INTEGER,
            provider TEXT NOT NULL,
            hq TEXT,
            revenue REAL,
            teu REAL,
            air_tons REAL,
            source_url TEXT,
            created_at TEXT,
            PRIMARY KEY (date, provider)
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            status TEXT,
            message TEXT,
            row_count INTEGER
        )
    ''')
    conn.commit()
    return conn


def save_today_data(curr_df):
    conn = get_connection()
    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        save_df = curr_df.copy()
        save_df["created_at"] = now
        conn.execute("DELETE FROM forwarders WHERE date = ?", (today,))
        save_df.to_sql("forwarders", conn, if_exists="append", index=False)
        conn.execute("INSERT INTO run_log(run_at, status, message, row_count) VALUES (?, ?, ?, ?)", (now, "SUCCESS", "OK", len(save_df)))
        conn.commit()
        log(f"[DB] 저장 완료: {DB_PATH} / rows={len(save_df)}")
    except Exception as e:
        conn.rollback()
        conn.execute("INSERT INTO run_log(run_at, status, message, row_count) VALUES (?, ?, ?, ?)", (now, "ERROR", str(e)[:1000], 0))
        conn.commit()
        raise
    finally:
        conn.close()


def refresh_data(url=DEFAULT_URL):
    curr_df = fetch_top_25_data(url=url)
    save_today_data(curr_df)
    return curr_df


def load_history():
    conn = get_connection()
    try:
        return pd.read_sql_query("SELECT date, rank, provider, hq, revenue, teu, air_tons, source_url, created_at FROM forwarders ORDER BY date DESC, rank ASC", conn)
    finally:
        conn.close()


def get_last_run_meta():
    conn = get_connection()
    try:
        row = conn.execute("SELECT run_at, status, message, row_count FROM run_log ORDER BY run_id DESC LIMIT 1").fetchone()
        if not row:
            return {"run_at": "-", "status": "-", "message": "-", "row_count": 0}
        return {"run_at": row[0], "status": row[1], "message": row[2], "row_count": row[3]}
    finally:
        conn.close()


HTML = r'''
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A&A Top 25 Global Freight Forwarders Dashboard</title>
<style>
:root { --primary:#003366; --primary2:#0f4c81; --bg:#f4f7f6; --card:#ffffff; --border:#e5e7eb; --text:#263238; --muted:#667085; --up:#28a745; --down:#d9534f; }
* { box-sizing:border-box; }
body { font-family:'Segoe UI', Arial, sans-serif; margin:0; padding:32px; background:linear-gradient(180deg,#f8fafc 0%,var(--bg) 100%); color:var(--text); }
.container { max-width:1180px; margin:auto; background:var(--card); padding:34px; border-radius:18px; box-shadow:0 12px 34px rgba(15,23,42,.10); }
.header { display:flex; justify-content:space-between; gap:16px; align-items:flex-start; border-bottom:1px solid var(--border); padding-bottom:18px; margin-bottom:22px; }
h1 { margin:0; color:var(--primary); border-left:8px solid var(--primary); padding-left:15px; font-size:28px; line-height:1.2; }
.subtitle { margin-top:8px; color:var(--muted); font-size:13px; line-height:1.7; }
.subtitle a { color:var(--primary); font-weight:800; text-decoration:none; }
.subtitle a:hover { text-decoration:underline; }
.actions { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
.btn { display:inline-flex; align-items:center; justify-content:center; background:var(--primary); color:white; padding:10px 18px; border:0; border-radius:10px; cursor:pointer; font-weight:800; white-space:nowrap; text-decoration:none; font-size:13px; }
.btn:hover { background:var(--primary2); }
.btn.secondary { background:white; color:var(--primary); border:1px solid var(--primary); }
.alert { padding:12px 14px; background:#fff8e1; color:#7a5a00; border:1px solid #f5d76e; border-radius:12px; margin:14px 0; font-weight:700; }
.kpi-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin:18px 0 22px 0; }
.kpi { background:#f8fafc; border:1px solid var(--border); border-radius:14px; padding:16px; }
.kpi-label { color:var(--muted); font-size:12px; font-weight:800; text-transform:uppercase; }
.kpi-value { margin-top:7px; color:#111827; font-size:24px; font-weight:900; }
.date-panel { display:flex; gap:15px; align-items:center; flex-wrap:wrap; background:#eef3f8; padding:15px 18px; border-radius:12px; margin:20px 0; border-left:5px solid var(--primary); }
select { padding:9px 10px; border-radius:8px; border:1px solid #cbd5e1; background:white; }
h2 { margin-top:30px; color:#1f2937; font-size:20px; }
small { color:#98a2b3; font-weight:500; }
.table-wrap { width:100%; overflow:auto; border:1px solid var(--border); border-radius:12px; }
table { width:100%; border-collapse:collapse; background:white; }
th { background:var(--primary); color:white; padding:12px; text-align:left; cursor:pointer; white-space:nowrap; }
td { padding:11px 12px; border-bottom:1px solid #eef2f7; white-space:nowrap; }
tr:hover td { background:#f9fafb; }
.diff-up { color:var(--up); font-weight:900; }
.diff-down { color:var(--down); font-weight:900; }
.methodology { background:#f8f9fa; padding:20px; border-radius:14px; margin-top:30px; border:1px solid #e9ecef; }
.method-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:18px; margin-top:15px; }
.footnote { margin-top:20px; font-size:12px; color:#777; border-top:1px solid #eee; padding-top:12px; text-align:center; white-space:normal; line-height:1.6; }
.empty { padding:40px; text-align:center; color:#667085; }
@media (max-width:900px) { body { padding:14px; } .container { padding:20px; } .header { flex-direction:column; } .kpi-grid, .method-grid { grid-template-columns:1fr; } .actions { justify-content:flex-start; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>A&A’s Top 25 Global Freight Forwarders List</h1>
      <div class="subtitle">
        Source: <a href="{{ source_url }}" target="_blank" rel="noopener noreferrer">A&amp;A Top 25 Global Freight Forwarders List</a><br>
        Last run: {{ meta.run_at }} / {{ meta.status }} / rows: {{ meta.row_count }}
      </div>
    </div>
    <div class="actions"><a class="btn secondary" href="/refresh">🔄 Refresh</a><a class="btn" href="/download_csv">📥 Download CSV</a></div>
  </div>
  {% if message %}<div class="alert">{{ message }}</div>{% endif %}
  {% if is_empty %}<div class="empty">DB에 표시할 데이터가 없습니다. Refresh를 눌러 데이터를 수집하세요.</div>{% else %}
  <div class="kpi-grid"><div class="kpi"><div class="kpi-label">Latest Date</div><div class="kpi-value">{{ latest_date }}</div></div><div class="kpi"><div class="kpi-label">Total Revenue</div><div class="kpi-value">{{ total_revenue }}</div></div><div class="kpi"><div class="kpi-label">Total Ocean TEU</div><div class="kpi-value">{{ total_teu }}</div></div><div class="kpi"><div class="kpi-label">Total Air Tons</div><div class="kpi-value">{{ total_air }}</div></div></div>
  <div class="date-panel"><label>📅 기준 시점:</label><select id="baseDate" onchange="updateUI()"></select><label>↔ 비교 시점:</label><select id="compareDate" onchange="updateUI()"></select><span style="font-size:12px;color:#667085;">데이터가 누적될수록 비교 대상이 늘어납니다.</span></div>
  <h2>1. 주요 변동 현황 <small id="compLabel"></small></h2><div class="table-wrap"><table><thead><tr><th>업체명</th><th>순위 변화</th><th>매출 증감</th><th>Ocean TEU 증감</th><th>Air Tons 증감</th></tr></thead><tbody id="compBody"></tbody></table></div>
  <h2>2. 전체 순위 리스트 <small id="baseLabel"></small></h2><div class="table-wrap"><table id="mainTable"><thead><tr><th onclick="sortTable(0,'num')">Rank</th><th onclick="sortTable(1,'str')">Provider</th><th onclick="sortTable(2,'str')">HQ</th><th onclick="sortTable(3,'num')">Revenue ($M)</th><th onclick="sortTable(4,'num')">Ocean (TEU)</th><th onclick="sortTable(5,'num')">Air (Tons)</th></tr></thead><tbody id="mainBody"></tbody></table></div>
  <div class="methodology"><h3 style="margin-top:0;">Methodology & Notes</h3><div class="method-grid"><div><b>Revenue Scope</b><ul><li>DTM, ITM, DCC, VAWD 포함</li><li>A&A 추정치 기반</li></ul></div><div><b>Currency</b><ul><li>연평균 환율 적용</li><li>US$ 환산 기준</li></ul></div><div><b>Ranking Basis</b><ul><li>매출/물동량 개별 순위 합산</li><li>종합 평균 순위 산출</li></ul></div></div><div class="footnote"><b>DTM:</b> Domestic Transportation Management &nbsp;|&nbsp; <b>ITM:</b> International Transportation Management &nbsp;|&nbsp; <b>DCC:</b> Dedicated Contract Carriage &nbsp;|&nbsp; <b>VAWD:</b> Value-Added Warehousing and Distribution</div></div>
  {% endif %}
</div>
<script>
const rawData = {{ data_json|safe }};
const dates = {{ dates_json|safe }};
let sortAsc = true;
function fmtNum(v){ const n=Number(v||0); return n.toLocaleString(undefined,{maximumFractionDigits:0}); }
function fmtMoney(v){ return "$"+fmtNum(v)+"M"; }
function init(){ if(!dates||dates.length===0) return; const baseSel=document.getElementById("baseDate"); const compSel=document.getElementById("compareDate"); dates.forEach(function(d){ baseSel.add(new Option(d,d)); compSel.add(new Option(d,d)); }); if(dates.length>1) compSel.selectedIndex=1; updateUI(); }
function updateUI(){ const baseDate=document.getElementById("baseDate").value; const compDate=document.getElementById("compareDate").value; const baseData=rawData[baseDate]||[]; const compData=rawData[compDate]||[]; document.getElementById("baseLabel").innerText="(기준일: "+baseDate+")"; document.getElementById("compLabel").innerText="("+compDate+" 대비)"; const mainBody=document.getElementById("mainBody"); mainBody.innerHTML=baseData.map(function(r){ return "<tr><td data-val='"+r.rank+"'>"+parseInt(r.rank)+"</td><td>"+r.provider+"</td><td>"+r.hq+"</td><td data-val='"+r.revenue+"'>"+fmtMoney(r.revenue)+"</td><td data-val='"+r.teu+"'>"+fmtNum(r.teu)+"</td><td data-val='"+r.air_tons+"'>"+fmtNum(r.air_tons)+"</td></tr>"; }).join(""); const compBody=document.getElementById("compBody"); if(baseDate===compDate){ compBody.innerHTML="<tr><td colspan='5' style='text-align:center;'>동일 시점입니다.</td></tr>"; return; } const compMap=new Map(compData.map(function(i){ return [i.provider,i]; })); let compHtml=""; function fmtDiff(v,suffix){ const n=Number(v||0); if(n===0) return "-"; if(n>0) return "<span class='diff-up'>▲ "+fmtNum(n)+suffix+"</span>"; return "<span class='diff-down'>▼ "+fmtNum(Math.abs(n))+suffix+"</span>"; } baseData.forEach(function(curr){ const prev=compMap.get(curr.provider); if(!prev) return; const rankDiff=Number(prev.rank)-Number(curr.rank); const revDiff=Number(curr.revenue)-Number(prev.revenue); const teuDiff=Number(curr.teu)-Number(prev.teu); const airDiff=Number(curr.air_tons)-Number(prev.air_tons); const rankFmt=rankDiff===0?"-":(rankDiff>0?"<span class='diff-up'>▲ "+rankDiff+"</span>":"<span class='diff-down'>▼ "+Math.abs(rankDiff)+"</span>"); compHtml += "<tr><td>"+curr.provider+"</td><td>"+rankFmt+"</td><td>"+fmtDiff(revDiff,"M")+"</td><td>"+fmtDiff(teuDiff,"")+"</td><td>"+fmtDiff(airDiff,"")+"</td></tr>"; }); compBody.innerHTML=compHtml||"<tr><td colspan='5' style='text-align:center;'>비교 가능한 데이터가 없습니다.</td></tr>"; }
function sortTable(idx,type){ const tbody=document.getElementById("mainBody"); const rows=Array.from(tbody.rows); const dir=sortAsc?1:-1; sortAsc=!sortAsc; rows.sort(function(a,b){ let vA=a.cells[idx].dataset.val||a.cells[idx].innerText; let vB=b.cells[idx].dataset.val||b.cells[idx].innerText; if(type==="num"){ vA=parseFloat(String(vA).replace(/[^0-9.-]/g,""))||0; vB=parseFloat(String(vB).replace(/[^0-9.-]/g,""))||0; return dir*(vA-vB); } return dir*String(vA).localeCompare(String(vB)); }); rows.forEach(function(r){ tbody.appendChild(r); }); }
window.onload=init;
</script>
</body>
</html>
'''


@app.route("/")
def index():
    msg = request.args.get("msg", "")
    df = load_history()
    meta = get_last_run_meta()
    if df.empty:
        return render_template_string(HTML, is_empty=True, message=msg, meta=meta, source_url=RUNTIME_CONFIG.url if RUNTIME_CONFIG else DEFAULT_URL, data_json="{}", dates_json="[]", latest_date="-", total_revenue="-", total_teu="-", total_air="-")
    for col in ["rank", "revenue", "teu", "air_tons"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
    all_dates = sorted(df["date"].dropna().unique().tolist(), reverse=True)
    data_by_date = {d: df[df["date"] == d].sort_values("rank").to_dict(orient="records") for d in all_dates}
    latest_date = all_dates[0]
    latest_df = df[df["date"] == latest_date]
    return render_template_string(
        HTML,
        is_empty=False,
        message=msg,
        meta=meta,
        source_url=RUNTIME_CONFIG.url if RUNTIME_CONFIG else DEFAULT_URL,
        data_json=json.dumps(data_by_date, ensure_ascii=False),
        dates_json=json.dumps(all_dates, ensure_ascii=False),
        latest_date=latest_date,
        total_revenue=f"${latest_df['revenue'].sum():,.0f}M",
        total_teu=f"{latest_df['teu'].sum():,.0f}",
        total_air=f"{latest_df['air_tons'].sum():,.0f}",
    )


@app.route("/refresh")
def refresh():
    try:
        refresh_data(url=RUNTIME_CONFIG.url)
        return redirect(url_for("index", msg="데이터 수집 및 DB 저장 완료"))
    except Exception as e:
        return redirect(url_for("index", msg=f"데이터 수집 실패: {e}"))


@app.route("/download_csv")
def download_csv():
    df = load_history()
    csv_data = df.to_csv(index=False, encoding="utf-8-sig")
    return Response(csv_data, mimetype="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=Freight_Forwarders_History.csv"})


def parse_args():
    parser = argparse.ArgumentParser(description="A&A Top 25 Global Freight Forwarders DB 저장형 웹 대시보드")
    parser.add_argument("--url", default=DEFAULT_URL, help="수집 대상 URL")
    parser.add_argument("--host", default="127.0.0.1", help="Flask host")
    parser.add_argument("--port", type=int, default=8080, help="Flask port. 사용 중이면 자동으로 다른 포트를 찾습니다.")
    parser.add_argument("--no-open", action="store_true", help="웹페이지를 자동으로 열지 않음")
    parser.add_argument("--no-fetch-on-start", action="store_true", help="실행 시작 시 자동 수집하지 않고 기존 DB만 표시")
    return parser.parse_args()


def main():
    global RUNTIME_CONFIG
    args = parse_args()
    RUNTIME_CONFIG = args
    get_connection().close()
    if not args.no_fetch_on_start:
        try:
            refresh_data(url=args.url)
        except Exception as e:
            log(f"[WARN] 시작 시 데이터 수집 실패: {e}")
            log("[WARN] 기존 DB 데이터가 있으면 웹 화면에 표시합니다.")
    port = find_available_port(args.host, args.port)
    local_url = f"http://{args.host}:{port}"
    log(f"[INFO] DB 파일: {DB_PATH}")
    log(f"[INFO] Flask 서버 시작: {local_url}")
    if not args.no_open:
        threading.Thread(target=wait_and_open_browser, args=(local_url, port), daemon=True).start()
    app.run(host=args.host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
