# -*- coding: utf-8 -*-
import argparse
import datetime as dt
import importlib.util
import json
import os
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

        print(
            f"[SETUP] '{pip_name}' 패키지가 없어 설치를 시도합니다...",
            flush=True,
        )

        try:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    pip_name,
                ]
            )
        except Exception:
            subprocess.check_call(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    pip_name,
                    "--break-system-packages",
                ]
            )

setup_environment()

import pandas as pd
import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, redirect, render_template_string, request, url_for

# =========================================================
# 1. Configuration
# =========================================================
SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_URL = (
    "https://www.3plogistics.com/3pl-market-info-resources/"
    "3pl-market-information/aas-top-25-global-freight-forwarders-list/"
)

DB_PATH = SCRIPT_DIR / "freight_forwarders_data.db"
STATIC_HTML_PATH = SCRIPT_DIR / "index.html"
STATIC_CSV_PATH = SCRIPT_DIR / "freight_forwarders_history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

EXPECTED_COLUMNS = [
    "rank",
    "provider",
    "hq",
    "revenue",
    "teu",
    "air_tons",
]

app = Flask(__name__)
RUNTIME_CONFIG = None

# =========================================================
# 2. Common Utilities
# =========================================================
def log(message):
    print(message, flush=True)

def clean_number(value):
    """문자열 숫자 값을 float으로 정리한다."""
    text = str(value).strip()

    if not text or text.lower() in {"nan", "none", "-", "n/a", "na"}:
        return 0.0

    text = text.replace(",", "")
    text = "".join(
        char
        for char in text
        if char.isdigit() or char == "."
    )

    if not text:
        return 0.0

    try:
        return float(text)
    except ValueError:
        return 0.0

def normalize_provider_name(value):
    """업체명 공백을 정리한다."""
    return " ".join(str(value).strip().split())

def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_REUSEADDR,
            1,
        )

        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False

def find_available_port(host="127.0.0.1", preferred=8080):
    candidates = [
        preferred,
        8080,
        8081,
        8082,
        8000,
        5000,
        8501,
        7860,
        3000,
    ]

    checked = set()

    for port in candidates:
        if port in checked:
            continue

        checked.add(port)

        if is_port_available(host, port):
            return port

    for port in range(8000, 8101):
        if port not in checked and is_port_available(host, port):
            return port

    raise RuntimeError("사용 가능한 포트를 찾지 못했습니다.")

def wait_and_open_browser(url, port, timeout=20):
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            with socket.create_connection(
                ("127.0.0.1", port),
                timeout=1,
            ):
                webbrowser.open_new(url)
                return
        except Exception:
            time.sleep(0.4)

# =========================================================
# 3. Web Crawling
# =========================================================
def select_best_table(html_text):
    """
    대상 웹페이지 내 여러 표 중 A&A Top 25 Forwarder 표를 식별한다.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    tables = soup.find_all("table")

    if not tables:
        raise RuntimeError("웹페이지에서 table 태그를 찾지 못했습니다.")

    candidates = []

    for index, table in enumerate(tables):
        try:
            dataframe = pd.read_html(
                StringIO(str(table))
            )[0]
        except Exception:
            continue

        if dataframe is None or dataframe.empty:
            continue

        content = (
            " ".join(map(str, dataframe.columns))
            + " "
            + " ".join(
                map(
                    str,
                    dataframe.head(5).values.ravel(),
                )
            )
        ).lower()

        score = 0

        for keyword in [
            "rank",
            "provider",
            "headquarters",
            "hq",
            "revenue",
            "teu",
            "air",
        ]:
            if keyword in content:
                score += 5

        if dataframe.shape[1] >= 6:
            score += 3

        if dataframe.shape[0] >= 20:
            score += 5

        if any(
            company in content
            for company in [
                "dhl",
                "kuehne",
                "db schenker",
                "dsv",
                "ceva",
            ]
        ):
            score += 3

        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError(
            "pandas로 읽을 수 있는 테이블을 찾지 못했습니다."
        )

    candidates.sort(
        key=lambda item: (item[0], -item[1]),
        reverse=True,
    )

    best_score, _, best_dataframe = candidates[0]

    if best_score <= 0:
        raise RuntimeError(
            "Top 25 Freight Forwarder 표를 식별하지 못했습니다."
        )

    return best_dataframe

def fetch_top_25_data(url=DEFAULT_URL):
    """
    A&A Top 25 Global Freight Forwarders 데이터를 가져와 표준 형식으로 정리한다.
    """
    log(
        "[FETCH] 최신 A&A Top 25 Global Freight Forwarders "
        "데이터 수집 중..."
    )

    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30,
    )
    response.raise_for_status()

    raw_dataframe = select_best_table(response.text)

    if isinstance(raw_dataframe.columns, pd.MultiIndex):
        raw_dataframe.columns = [
            " ".join(
                str(value)
                for value in column
                if str(value).lower() != "nan"
            ).strip()
            for column in raw_dataframe.columns
        ]
    else:
        raw_dataframe.columns = [
            str(column).strip()
            for column in raw_dataframe.columns
        ]

    if raw_dataframe.shape[1] < 6:
        raise RuntimeError(
            "예상보다 컬럼 수가 적습니다. "
            f"현재 shape={raw_dataframe.shape}"
        )

    dataframe = raw_dataframe.iloc[:, :6].copy()
    dataframe.columns = EXPECTED_COLUMNS

    for column in [
        "rank",
        "revenue",
        "teu",
        "air_tons",
    ]:
        dataframe[column] = dataframe[column].apply(clean_number)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["provider"] = dataframe["provider"].apply(
        normalize_provider_name
    )
    dataframe["hq"] = dataframe["hq"].astype(str).str.strip()
    dataframe["date"] = dt.date.today().isoformat()
    dataframe["source_url"] = url

    dataframe = dataframe[
        dataframe["provider"].astype(str).str.len() > 0
    ]

    dataframe = dataframe.sort_values(
        "rank"
    ).reset_index(drop=True)

    if dataframe.empty:
        raise RuntimeError("정제 후 데이터가 비어 있습니다.")

    log(f"[FETCH] 수집 완료: {len(dataframe)} rows")

    return dataframe

# =========================================================
# 4. SQLite Database
# =========================================================
def get_connection():
    """SQLite 연결 및 필요한 테이블을 생성한다."""
    connection = sqlite3.connect(DB_PATH)

    connection.execute(
        """
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
        """
    )

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS run_log (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT,
            status TEXT,
            message TEXT,
            row_count INTEGER
        )
        """
    )

    connection.commit()

    return connection

def save_today_data(current_dataframe):
    """오늘 수집한 데이터를 SQLite에 저장한다."""
    connection = get_connection()

    today = dt.date.today().isoformat()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        save_dataframe = current_dataframe.copy()
        save_dataframe["created_at"] = now

        connection.execute(
            "DELETE FROM forwarders WHERE date = ?",
            (today,),
        )

        save_dataframe.to_sql(
            "forwarders",
            connection,
            if_exists="append",
            index=False,
        )

        connection.execute(
            """
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                "SUCCESS",
                "OK",
                len(save_dataframe),
            ),
        )

        connection.commit()

        log(
            f"[DB] 저장 완료: {DB_PATH} / "
            f"rows={len(save_dataframe)}"
        )

    except Exception as error:
        connection.rollback()

        connection.execute(
            """
            INSERT INTO run_log (
                run_at,
                status,
                message,
                row_count
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                now,
                "ERROR",
                str(error)[:1000],
                0,
            ),
        )

        connection.commit()
        raise

    finally:
        connection.close()

def refresh_data(url=DEFAULT_URL):
    """최신 데이터를 수집하고 DB에 저장한다."""
    current_dataframe = fetch_top_25_data(url=url)
    save_today_data(current_dataframe)

    return current_dataframe

def load_history():
    """누적된 Forwarder 이력 데이터를 읽는다."""
    connection = get_connection()

    try:
        return pd.read_sql_query(
            """
            SELECT
                date,
                rank,
                provider,
                hq,
                revenue,
                teu,
                air_tons,
                source_url,
                created_at
            FROM forwarders
            ORDER BY date DESC, rank ASC
            """,
            connection,
        )
    finally:
        connection.close()

def get_last_run_meta():
    """최근 실행 이력을 조회한다."""
    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT
                run_at,
                status,
                message,
                row_count
            FROM run_log
            ORDER BY run_id DESC
            LIMIT 1
            """
        ).fetchone()

        if not row:
            return {
                "run_at": "-",
                "status": "-",
                "message": "-",
                "row_count": 0,
            }

        return {
            "run_at": row[0],
            "status": row[1],
            "message": row[2],
            "row_count": row[3],
        }

    finally:
        connection.close()

# =========================================================
# 5. Dashboard HTML Template
# =========================================================
HTML = r'''
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A&A Top 25 Global Freight Forwarders Dashboard</title>
<style>
:root {
    --primary:#003366;
    --primary2:#0f4c81;
    --bg:#f4f7f6;
    --card:#ffffff;
    --border:#e5e7eb;
    --text:#263238;
    --muted:#667085;
    --up:#28a745;
    --down:#d9534f;
}
* { box-sizing:border-box; }
body {
    font-family:'Segoe UI', Arial, sans-serif;
    margin:0;
    padding:32px;
    background:linear-gradient(180deg,#f8fafc 0%,var(--bg) 100%);
    color:var(--text);
}
.container {
    max-width:1180px;
    margin:auto;
    background:var(--card);
    padding:34px;
    border-radius:18px;
    box-shadow:0 12px 34px rgba(15,23,42,.10);
}
.header {
    display:flex;
    justify-content:space-between;
    gap:16px;
    align-items:flex-start;
    border-bottom:1px solid var(--border);
    padding-bottom:18px;
    margin-bottom:22px;
}
h1 {
    margin:0;
    color:var(--primary);
    border-left:8px solid var(--primary);
    padding-left:15px;
    font-size:28px;
    line-height:1.2;
}
.subtitle {
    margin-top:8px;
    color:var(--muted);
    font-size:13px;
    line-height:1.7;
}
.subtitle a {
    color:var(--primary);
    font-weight:800;
    text-decoration:none;
}
.subtitle a:hover { text-decoration:underline; }
.actions {
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    justify-content:flex-end;
}
.btn {
    display:inline-flex;
    align-items:center;
    justify-content:center;
    background:var(--primary);
    color:white;
    padding:10px 18px;
    border:0;
    border-radius:10px;
    cursor:pointer;
    font-weight:800;
    white-space:nowrap;
    text-decoration:none;
    font-size:13px;
}
.btn:hover { background:var(--primary2); }
.btn.secondary {
    background:white;
    color:var(--primary);
    border:1px solid var(--primary);
}
.alert {
    padding:12px 14px;
    background:#fff8e1;
    color:#7a5a00;
    border:1px solid #f5d76e;
    border-radius:12px;
    margin:14px 0;
    font-weight:700;
}
.kpi-grid {
    display:grid;
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:14px;
    margin:18px 0 22px 0;
}
.kpi {
    background:#f8fafc;
    border:1px solid var(--border);
    border-radius:14px;
    padding:16px;
}
.kpi-label {
    color:var(--muted);
    font-size:12px;
    font-weight:800;
    text-transform:uppercase;
}
.kpi-value {
    margin-top:7px;
    color:#111827;
    font-size:24px;
    font-weight:900;
}
.date-panel {
    display:flex;
    gap:15px;
    align-items:center;
    flex-wrap:wrap;
    background:#eef3f8;
    padding:15px 18px;
    border-radius:12px;
    margin:20px 0;
    border-left:5px solid var(--primary);
}
select {
    padding:9px 10px;
    border-radius:8px;
    border:1px solid #cbd5e1;
    background:white;
}
h2 {
    margin-top:30px;
    color:#1f2937;
    font-size:20px;
}
small {
    color:#98a2b3;
    font-weight:500;
}
.table-wrap {
    width:100%;
    overflow:auto;
    border:1px solid var(--border);
    border-radius:12px;
}
table {
    width:100%;
    border-collapse:collapse;
    background:white;
}
th {
    background:var(--primary);
    color:white;
    padding:12px;
    text-align:left;
    cursor:pointer;
    white-space:nowrap;
}
td {
    padding:11px 12px;
    border-bottom:1px solid #eef2f7;
    white-space:nowrap;
}
tr:hover td { background:#f9fafb; }
.diff-up {
    color:var(--up);
    font-weight:900;
}
.diff-down {
    color:var(--down);
    font-weight:900;
}
.methodology {
    background:#f8f9fa;
    padding:20px;
    border-radius:14px;
    margin-top:30px;
    border:1px solid #e9ecef;
}
.method-grid {
    display:grid;
    grid-template-columns:repeat(3,1fr);
    gap:18px;
    margin-top:15px;
}
.footnote {
    margin-top:20px;
    font-size:12px;
    color:#777;
    border-top:1px solid #eee;
    padding-top:12px;
    text-align:center;
    white-space:normal;
    line-height:1.6;
}
.empty {
    padding:40px;
    text-align:center;
    color:#667085;
}
@media (max-width:900px) {
    body { padding:14px; }
    .container { padding:20px; }
    .header { flex-direction:column; }
    .kpi-grid, .method-grid { grid-template-columns:1fr; }
    .actions { justify-content:flex-start; }
}
</style>
</head>
<body>
<div class="container">

  <div class="header">
    <div>
      <h1>A&A’s Top 25 Global Freight Forwarders List</h1>
      <div class="subtitle">
        Source:
        <a href="{{ source_url }}" target="_blank" rel="noopener noreferrer">
          A&amp;A Top 25 Global Freight Forwarders List
        </a><br>
        Last run: {{ meta.run_at }} / {{ meta.status }} / rows: {{ meta.row_count }}
      </div>
    </div>

    <div class="actions">
      {% if static_mode %}
      <a class="btn" href="{{ csv_filename }}" download>
        📥 Download CSV
      </a>
      {% else %}
      <a class="btn secondary" href="/refresh">
        🔄 Refresh
      </a>
      <a class="btn" href="/download_csv">
        📥 Download CSV
      </a>
      {% endif %}
    </div>
  </div>

  {% if message %}
  <div class="alert">{{ message }}</div>
  {% endif %}

  {% if is_empty %}

  <div class="empty">
    DB에 표시할 데이터가 없습니다.
    {% if not static_mode %}
    Refresh를 눌러 데이터를 수집하세요.
    {% endif %}
  </div>

  {% else %}

  <div class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label">Latest Date</div>
      <div class="kpi-value">{{ latest_date }}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Total Revenue</div>
      <div class="kpi-value">{{ total_revenue }}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Total Ocean TEU</div>
      <div class="kpi-value">{{ total_teu }}</div>
    </div>
    <div class="kpi">
      <div class="kpi-label">Total Air Tons</div>
      <div class="kpi-value">{{ total_air }}</div>
    </div>
  </div>

  <div class="date-panel">
    <label>📅 기준 시점:</label>
    <select id="baseDate" onchange="updateUI()"></select>

    <label>↔ 비교 시점:</label>
    <select id="compareDate" onchange="updateUI()"></select>

    <span style="font-size:12px;color:#667085;">
      데이터가 누적될수록 비교 대상이 늘어납니다.
    </span>
  </div>

  <h2>
    1. 주요 변동 현황
    <small id="compLabel"></small>
  </h2>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>업체명</th>
          <th>순위 변화</th>
          <th>매출 증감</th>
          <th>Ocean TEU 증감</th>
          <th>Air Tons 증감</th>
        </tr>
      </thead>
      <tbody id="compBody"></tbody>
    </table>
  </div>

  <h2>
    2. 전체 순위 리스트
    <small id="baseLabel"></small>
  </h2>

  <div class="table-wrap">
    <table id="mainTable">
      <thead>
        <tr>
          <th onclick="sortTable(0,'num')">Rank</th>
          <th onclick="sortTable(1,'str')">Provider</th>
          <th onclick="sortTable(2,'str')">HQ</th>
          <th onclick="sortTable(3,'num')">Revenue ($M)</th>
          <th onclick="sortTable(4,'num')">Ocean (TEU)</th>
          <th onclick="sortTable(5,'num')">Air (Tons)</th>
        </tr>
      </thead>
      <tbody id="mainBody"></tbody>
    </table>
  </div>

  <div class="methodology">
    <h3 style="margin-top:0;">Methodology &amp; Notes</h3>

    <div class="method-grid">
      <div>
        <b>Revenue Scope</b>
        <ul>
          <li>DTM, ITM, DCC, VAWD 포함</li>
          <li>A&amp;A 추정치 기반</li>
        </ul>
      </div>

      <div>
        <b>Currency</b>
        <ul>
          <li>연평균 환율 적용</li>
          <li>US$ 환산 기준</li>
        </ul>
      </div>

      <div>
        <b>Ranking Basis</b>
        <ul>
          <li>매출/물동량 개별 순위 합산</li>
          <li>종합 평균 순위 산출</li>
        </ul>
      </div>
    </div>

    <div class="footnote">
      <b>DTM:</b> Domestic Transportation Management
      &nbsp;|&nbsp;
      <b>ITM:</b> International Transportation Management
      &nbsp;|&nbsp;
      <b>DCC:</b> Dedicated Contract Carriage
      &nbsp;|&nbsp;
      <b>VAWD:</b> Value-Added Warehousing and Distribution
    </div>
  </div>

  {% endif %}
</div>

<script>
const rawData = {{ data_json|safe }};
const dates = {{ dates_json|safe }};
let sortAsc = true;

function fmtNum(value) {
    const number = Number(value || 0);

    return number.toLocaleString(
        undefined,
        {
            maximumFractionDigits: 0
        }
    );
}

function fmtMoney(value) {
    return "$" + fmtNum(value) + "M";
}

function init() {
    if (!dates || dates.length === 0) {
        return;
    }

    const baseSelect = document.getElementById("baseDate");
    const compareSelect = document.getElementById("compareDate");

    dates.forEach(function(dateValue) {
        baseSelect.add(
            new Option(dateValue, dateValue)
        );

        compareSelect.add(
            new Option(dateValue, dateValue)
        );
    });

    if (dates.length > 1) {
        compareSelect.selectedIndex = 1;
    }

    updateUI();
}

function updateUI() {
    const baseDate = document.getElementById("baseDate").value;
    const compareDate = document.getElementById("compareDate").value;

    const baseData = rawData[baseDate] || [];
    const compareData = rawData[compareDate] || [];

    document.getElementById("baseLabel").innerText =
        "(기준일: " + baseDate + ")";

    document.getElementById("compLabel").innerText =
        "(" + compareDate + " 대비)";

    const mainBody = document.getElementById("mainBody");

    mainBody.innerHTML = baseData.map(function(row) {
        return (
            "<tr>" +
            "<td data-val='" + row.rank + "'>" +
            parseInt(row.rank) +
            "</td>" +
            "<td>" + row.provider + "</td>" +
            "<td>" + row.hq + "</td>" +
            "<td data-val='" + row.revenue + "'>" +
            fmtMoney(row.revenue) +
            "</td>" +
            "<td data-val='" + row.teu + "'>" +
            fmtNum(row.teu) +
            "</td>" +
            "<td data-val='" + row.air_tons + "'>" +
            fmtNum(row.air_tons) +
            "</td>" +
            "</tr>"
        );
    }).join("");

    const comparisonBody = document.getElementById("compBody");

    if (baseDate === compareDate) {
        comparisonBody.innerHTML =
            "<tr>" +
            "<td colspan='5' style='text-align:center;'>" +
            "동일 시점입니다." +
            "</td>" +
            "</tr>";

        return;
    }

    const comparisonMap = new Map(
        compareData.map(function(item) {
            return [item.provider, item];
        })
    );

    let comparisonHtml = "";

    function fmtDiff(value, suffix) {
        const number = Number(value || 0);

        if (number === 0) {
            return "-";
        }

        if (number > 0) {
            return (
                "<span class='diff-up'>" +
                "▲ " +
                fmtNum(number) +
                suffix +
                "</span>"
            );
        }

        return (
            "<span class='diff-down'>" +
            "▼ " +
            fmtNum(Math.abs(number)) +
            suffix +
            "</span>"
        );
    }

    baseData.forEach(function(current) {
        const previous = comparisonMap.get(current.provider);

        if (!previous) {
            return;
        }

        const rankDifference =
            Number(previous.rank) - Number(current.rank);

        const revenueDifference =
            Number(current.revenue) - Number(previous.revenue);

        const teuDifference =
            Number(current.teu) - Number(previous.teu);

        const airDifference =
            Number(current.air_tons) - Number(previous.air_tons);

        let rankHtml = "-";

        if (rankDifference > 0) {
            rankHtml =
                "<span class='diff-up'>" +
                "▲ " +
                rankDifference +
                "</span>";
        } else if (rankDifference < 0) {
            rankHtml =
                "<span class='diff-down'>" +
                "▼ " +
                Math.abs(rankDifference) +
                "</span>";
        }

        comparisonHtml += (
            "<tr>" +
            "<td>" + current.provider + "</td>" +
            "<td>" + rankHtml + "</td>" +
            "<td>" + fmtDiff(revenueDifference, "M") + "</td>" +
            "<td>" + fmtDiff(teuDifference, "") + "</td>" +
            "<td>" + fmtDiff(airDifference, "") + "</td>" +
            "</tr>"
        );
    });

    comparisonBody.innerHTML =
        comparisonHtml ||
        (
            "<tr>" +
            "<td colspan='5' style='text-align:center;'>" +
            "비교 가능한 데이터가 없습니다." +
            "</td>" +
            "</tr>"
        );
}

function sortTable(index, type) {
    const tableBody = document.getElementById("mainBody");
    const rows = Array.from(tableBody.rows);
    const direction = sortAsc ? 1 : -1;

    sortAsc = !sortAsc;

    rows.sort(function(rowA, rowB) {
        let valueA =
            rowA.cells[index].dataset.val ||
            rowA.cells[index].innerText;

        let valueB =
            rowB.cells[index].dataset.val ||
            rowB.cells[index].innerText;

        if (type === "num") {
            valueA =
                parseFloat(
                    String(valueA).replace(/[^0-9.-]/g, "")
                ) || 0;

            valueB =
                parseFloat(
                    String(valueB).replace(/[^0-9.-]/g, "")
                ) || 0;

            return direction * (valueA - valueB);
        }

        return direction * String(valueA).localeCompare(
            String(valueB)
        );
    });

    rows.forEach(function(row) {
        tableBody.appendChild(row);
    });
}

window.onload = init;
</script>

</body>
</html>
'''

# =========================================================
# 6. Dashboard Rendering
# =========================================================
def render_dashboard_html(
    message="",
    static_mode=False,
    source_url=DEFAULT_URL,
):
    """
    DB 데이터를 기준으로 HTML 대시보드를 렌더링한다.

    static_mode=True:
    - GitHub Pages용 index.html 생성
    - Flask /refresh 경로 대신 CSV 파일 링크 사용
    """
    dataframe = load_history()
    meta = get_last_run_meta()

    if dataframe.empty:
        return render_template_string(
            HTML,
            is_empty=True,
            message=message,
            meta=meta,
            source_url=source_url,
            data_json="{}",
            dates_json="[]",
            latest_date="-",
            total_revenue="-",
            total_teu="-",
            total_air="-",
            static_mode=static_mode,
            csv_filename=STATIC_CSV_PATH.name,
        )

    for column in [
        "rank",
        "revenue",
        "teu",
        "air_tons",
    ]:
        dataframe[column] = pd.to_numeric(
            dataframe[column],
            errors="coerce",
        ).fillna(0)

    all_dates = sorted(
        dataframe["date"].dropna().unique().tolist(),
        reverse=True,
    )

    data_by_date = {
        date_value: dataframe[
            dataframe["date"] == date_value
        ].sort_values(
            "rank"
        ).to_dict(
            orient="records"
        )
        for date_value in all_dates
    }

    latest_date = all_dates[0]

    latest_dataframe = dataframe[
        dataframe["date"] == latest_date
    ]

    return render_template_string(
        HTML,
        is_empty=False,
        message=message,
        meta=meta,
        source_url=source_url,
        data_json=json.dumps(
            data_by_date,
            ensure_ascii=False,
        ),
        dates_json=json.dumps(
            all_dates,
            ensure_ascii=False,
        ),
        latest_date=latest_date,
        total_revenue=(
            f"${latest_dataframe['revenue'].sum():,.0f}M"
        ),
        total_teu=(
            f"{latest_dataframe['teu'].sum():,.0f}"
        ),
        total_air=(
            f"{latest_dataframe['air_tons'].sum():,.0f}"
        ),
        static_mode=static_mode,
        csv_filename=STATIC_CSV_PATH.name,
    )

# =========================================================
# 7. Flask Routes
# =========================================================
@app.route("/")
def index():
    message = request.args.get("msg", "")

    source_url = (
        RUNTIME_CONFIG.url
        if RUNTIME_CONFIG
        else DEFAULT_URL
    )

    return render_dashboard_html(
        message=message,
        static_mode=False,
        source_url=source_url,
    )

@app.route("/refresh")
def refresh():
    source_url = (
        RUNTIME_CONFIG.url
        if RUNTIME_CONFIG
        else DEFAULT_URL
    )

    try:
        refresh_data(url=source_url)

        return redirect(
            url_for(
                "index",
                msg="데이터 수집 및 DB 저장 완료",
            )
        )

    except Exception as error:
        return redirect(
            url_for(
                "index",
                msg=f"데이터 수집 실패: {error}",
            )
        )

@app.route("/download_csv")
def download_csv():
    dataframe = load_history()

    csv_data = dataframe.to_csv(
        index=False,
        encoding="utf-8-sig",
    )

    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                "attachment; "
                "filename=Freight_Forwarders_History.csv"
            )
        },
    )

# =========================================================
# 8. Static File Generation
# =========================================================
def export_static_csv():
    """
    GitHub Pages에서 다운로드할 수 있는
    Forwarder 누적 이력 CSV 파일을 생성한다.
    """
    dataframe = load_history()

    if not dataframe.empty:
        dataframe = dataframe.copy()

        if "date" in dataframe.columns:
            dataframe["date"] = pd.to_datetime(
                dataframe["date"],
                errors="coerce",
            ).dt.strftime("%Y-%m-%d")

    dataframe.to_csv(
        STATIC_CSV_PATH,
        index=False,
        encoding="utf-8-sig",
    )

    log(
        f"[STATIC] CSV 생성 완료: {STATIC_CSV_PATH} "
        f"/ rows={len(dataframe)}"
    )

def generate_static_html(source_url=DEFAULT_URL):
    """
    SQLite DB 데이터를 사용해 정적 index.html을 생성한다.
    """
    with app.app_context():
        html_content = render_dashboard_html(
            message="",
            static_mode=True,
            source_url=source_url,
        )

    STATIC_HTML_PATH.write_text(
        html_content,
        encoding="utf-8",
    )

    log(f"[STATIC] HTML 생성 완료: {STATIC_HTML_PATH}")

def run_cron_job(url=DEFAULT_URL):
    """
    GitHub Actions 및 --crawl-only 실행용 정적 대시보드 생성 프로세스

    1. 최신 데이터 수집
    2. SQLite DB 저장
    3. 누적 CSV 생성
    4. 정적 index.html 생성
    """
    log("[FORWARDER] Static dashboard build started.")

    get_connection().close()

    crawl_message = ""

    try:
        dataframe = refresh_data(url=url)

        log(
            "[FORWARDER] 최신 데이터 수집 및 DB 저장 완료: "
            f"{len(dataframe)} rows"
        )

    except Exception as error:
        crawl_message = (
            "최신 데이터 수집에 실패하여 기존 DB 데이터를 표시합니다. "
            f"상세: {error}"
        )

        log(f"[FORWARDER] Crawl warning: {error}")

        existing_dataframe = load_history()

        if existing_dataframe.empty:
            raise RuntimeError(
                "최신 크롤링에 실패했고 기존 DB 데이터도 없습니다."
            ) from error

        log(
            "[FORWARDER] 기존 DB 데이터를 사용합니다: "
            f"{len(existing_dataframe)} rows"
        )

    export_static_csv()

    with app.app_context():
        html_content = render_dashboard_html(
            message=crawl_message,
            static_mode=True,
            source_url=url,
        )

    STATIC_HTML_PATH.write_text(
        html_content,
        encoding="utf-8",
    )

    log(f"[STATIC] HTML 생성 완료: {STATIC_HTML_PATH}")
    log("[FORWARDER] Static dashboard build completed.")

# =========================================================
# 9. Runtime Arguments
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "A&A Top 25 Global Freight Forwarders "
            "DB 저장형 웹 대시보드"
        )
    )

    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="수집 대상 URL",
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Flask host",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help=(
            "Flask port. 사용 중이면 "
            "자동으로 다른 포트를 찾습니다."
        ),
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="웹페이지를 자동으로 열지 않음",
    )

    parser.add_argument(
        "--no-fetch-on-start",
        action="store_true",
        help=(
            "실행 시작 시 자동 수집하지 않고 "
            "기존 DB만 표시"
        ),
    )

    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help=(
            "크롤링 → DB 저장 → CSV 생성 → "
            "정적 index.html 생성 후 종료"
        ),
    )

    return parser.parse_args()

# =========================================================
# 10. Main
# =========================================================
def main():
    global RUNTIME_CONFIG

    args = parse_args()
    RUNTIME_CONFIG = args

    get_connection().close()

    # GitHub Actions 또는 CLI의 --crawl-only 실행 시:
    # 크롤링 → DB 저장 → CSV → index.html → 종료
    if args.crawl_only or os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            run_cron_job(url=args.url)
            sys.exit(0)

        except Exception as error:
            log(
                "[FORWARDER] Static dashboard build failed: "
                f"{error}"
            )
            sys.exit(1)

    # 로컬 Flask 실행 전 초기 데이터 수집
    if not args.no_fetch_on_start:
        try:
            refresh_data(url=args.url)

        except Exception as error:
            log(f"[WARN] 시작 시 데이터 수집 실패: {error}")
            log(
                "[WARN] 기존 DB 데이터가 있으면 "
                "웹 화면에 표시합니다."
            )

    port = find_available_port(
        args.host,
        args.port,
    )

    local_url = f"http://{args.host}:{port}"

    log(f"[INFO] DB 파일: {DB_PATH}")
    log(f"[INFO] Flask 서버 시작: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, port),
            daemon=True,
        ).start()

    app.run(
        host=args.host,
        port=port,
        debug=False,
        use_reloader=False,
    )

if __name__ == "__main__":
    main()
