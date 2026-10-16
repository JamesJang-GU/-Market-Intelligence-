# -*- coding: utf-8 -*-
"""
GDP, Inflation & GSCPI Static Dashboard
---------------------------------------
로컬 개발 모드:
    python macro_economies.py

GitHub Actions / GitHub Pages 정적 생성 모드:
    python macro_economies.py --cron

생성 파일:
    - index.html
    - macro_data.json
    - gscpi_data.json
    - dashboard_meta.json
    - gdp_inflation_gscpi_cache.db
"""

import argparse
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import datetime
from io import BytesIO, StringIO
from pathlib import Path

import pandas as pd
import requests
from flask import Flask, Response, render_template_string

# =========================================================
# 1. Basic Configuration
# =========================================================
BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "gdp_inflation_gscpi_cache.db"
STATIC_HTML_PATH = BASE_DIR / "index.html"
MACRO_JSON_PATH = BASE_DIR / "macro_data.json"
GSCPI_JSON_PATH = BASE_DIR / "gscpi_data.json"
META_JSON_PATH = BASE_DIR / "dashboard_meta.json"

try:
    CACHE_TTL_HOURS = float(
        os.environ.get("CACHE_TTL_HOURS", "24")
    )
except ValueError:
    CACHE_TTL_HOURS = 24.0

WB_GDP_INDICATOR = "NY.GDP.MKTP.KD.ZG"
WB_INFLATION_INDICATOR = "FP.CPI.TOTL.ZG"

WB_GDP_API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/"
    f"{WB_GDP_INDICATOR}?format=json&per_page=25000"
)

WB_INFLATION_API_URL = (
    "https://api.worldbank.org/v2/country/all/indicator/"
    f"{WB_INFLATION_INDICATOR}?format=json&per_page=25000"
)

COUNTRY_META_URL = (
    "https://api.worldbank.org/v2/country?format=json&per_page=400"
)

GSCPI_XLS_URL = (
    "https://www.newyorkfed.org/medialibrary/research/"
    "interactives/gscpi/downloads/gscpi_data.xls"
)

DEFAULT_COUNTRIES = [
    "Canada",
    "France",
    "Germany",
    "Italy",
    "Japan",
    "United Kingdom",
    "United States",
    "Korea, Rep.",
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

app = Flask(__name__)

# =========================================================
# 2. Logging and Local Server Utilities
# =========================================================
def log(message):
    print(message, flush=True)

def is_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False

def find_available_port(host="127.0.0.1"):
    candidate_ports = [
        8000,
        8001,
        8002,
        8080,
        8081,
        8082,
        5000,
    ]

    for port in candidate_ports:
        if is_port_available(host, port):
            return port

    for port in range(8100, 8201):
        if is_port_available(host, port):
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

    log(f"[WARN] 브라우저 자동 실행 실패. 직접 접속하세요: {url}")

# =========================================================
# 3. SQLite Cache
# =========================================================
def init_cache_db():
    """SQLite 캐시 DB와 테이블을 생성한다."""
    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataframe_cache (
                cache_key TEXT PRIMARY KEY,
                cached_at REAL NOT NULL,
                row_count INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

        connection.commit()

def save_dataframe_cache(cache_key, dataframe):
    """DataFrame을 SQLite JSON 캐시로 저장한다."""
    init_cache_db()

    payload = dataframe.to_json(
        orient="table",
        date_format="iso",
        force_ascii=False,
    )

    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")

        connection.execute(
            """
            INSERT INTO dataframe_cache (
                cache_key,
                cached_at,
                row_count,
                payload
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                cached_at = excluded.cached_at,
                row_count = excluded.row_count,
                payload = excluded.payload
            """,
            (
                cache_key,
                time.time(),
                int(len(dataframe)),
                payload,
            ),
        )

        connection.commit()

def load_dataframe_cache(cache_key, allow_expired=False):
    """
    SQLite에서 DataFrame 캐시를 읽는다.
    allow_expired=True면 만료된 데이터도 반환한다.
    """
    init_cache_db()

    with sqlite3.connect(DB_PATH, timeout=30) as connection:
        connection.execute("PRAGMA busy_timeout=30000")

        row = connection.execute(
            """
            SELECT cached_at, payload
            FROM dataframe_cache
            WHERE cache_key = ?
            """,
            (cache_key,),
        ).fetchone()

    if row is None:
        return None

    cached_at, payload = row

    age_hours = max(
        0.0,
        (time.time() - float(cached_at)) / 3600.0,
    )

    if (
        not allow_expired
        and CACHE_TTL_HOURS >= 0
        and age_hours > CACHE_TTL_HOURS
    ):
        return None

    try:
        dataframe = pd.read_json(
            StringIO(payload),
            orient="table",
        )

        dataframe.attrs["cache_age_hours"] = age_hours

        return dataframe

    except Exception as error:
        log(f"[WARN] 캐시 읽기 실패 - {cache_key}: {error}")
        return None

def get_cached_or_fetch(cache_key, fetch_function, force_refresh=False):
    """
    캐시가 유효하면 캐시를 사용한다.
    force_refresh=True면 최신 네트워크 수집을 시도한다.
    네트워크 실패 시 만료된 기존 캐시를 대체 데이터로 사용한다.
    """
    if not force_refresh:
        cached = load_dataframe_cache(cache_key)

        if cached is not None:
            log(
                f"[CACHE] {cache_key}: "
                f"{len(cached):,}행 로드 "
                f"({cached.attrs.get('cache_age_hours', 0):.1f}시간 경과)"
            )
            return cached

    log(f"[FETCH] {cache_key}: 최신 데이터 조회 중...")

    try:
        dataframe = fetch_function()

        save_dataframe_cache(cache_key, dataframe)

        log(
            f"[DB] {cache_key}: "
            f"{len(dataframe):,}행 저장 완료"
        )

        return dataframe

    except Exception as error:
        stale = load_dataframe_cache(
            cache_key,
            allow_expired=True,
        )

        if stale is not None:
            log(
                f"[WARN] {cache_key} 최신 수집 실패. "
                f"기존 DB 캐시를 사용합니다: {error}"
            )
            return stale

        raise RuntimeError(
            f"{cache_key} 수집 실패 및 대체 캐시 없음: {error}"
        ) from error

# =========================================================
# 4. Data Collection
# =========================================================
def safe_get_json(url):
    """JSON API 호출 함수."""
    response = requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=60,
    )

    response.raise_for_status()

    return response.json()

def get_country_meta(force_refresh=False):
    """World Bank 국가 및 지역 메타데이터를 가져온다."""

    def fetch():
        data = safe_get_json(COUNTRY_META_URL)

        rows = (
            data[1]
            if isinstance(data, list) and len(data) > 1
            else []
        )

        output = []

        for item in rows:
            region_data = item.get("region") or {}

            region = (
                region_data.get("value")
                if isinstance(region_data, dict)
                else None
            )

            country = item.get("name")
            code = item.get("id")

            if (
                code
                and country
                and region
                and region != "Aggregates"
            ):
                output.append(
                    {
                        "code": code,
                        "country": country,
                        "region": region,
                    }
                )

        dataframe = pd.DataFrame(output)

        if dataframe.empty:
            return pd.DataFrame(
                columns=["code", "country", "region"]
            )

        return (
            dataframe
            .drop_duplicates(subset=["code"])
            .sort_values(["region", "country"])
            .reset_index(drop=True)
        )

    return get_cached_or_fetch(
        "worldbank_country_meta",
        fetch,
        force_refresh=force_refresh,
    )

def get_worldbank_indicator(
    url,
    value_name,
    force_refresh=False,
):
    """World Bank GDP 또는 Inflation 시계열을 가져온다."""

    def fetch():
        data = safe_get_json(url)

        rows = (
            data[1]
            if isinstance(data, list) and len(data) > 1
            else []
        )

        output = []

        for item in rows:
            country_data = item.get("country") or {}

            country = (
                country_data.get("value")
                if isinstance(country_data, dict)
                else None
            )

            code = (
                item.get("countryiso3code") or ""
            ).strip()

            year = item.get("date")
            value = item.get("value")

            if not (country and code and year and value is not None):
                continue

            try:
                output.append(
                    {
                        "code": code,
                        "country": country,
                        "year": int(year),
                        value_name: float(value),
                    }
                )
            except Exception:
                continue

        dataframe = pd.DataFrame(output)

        if dataframe.empty:
            return pd.DataFrame(
                columns=[
                    "code",
                    "country",
                    "year",
                    value_name,
                ]
            )

        return dataframe

    if WB_GDP_INDICATOR in url:
        cache_key = "worldbank_gdp_" + value_name
    elif WB_INFLATION_INDICATOR in url:
        cache_key = "worldbank_inflation_" + value_name
    else:
        cache_key = "worldbank_" + value_name

    return get_cached_or_fetch(
        cache_key,
        fetch,
        force_refresh=force_refresh,
    )

def get_gscpi_data(force_refresh=False):
    """
    New York Fed Global Supply Chain Pressure Index 데이터를 가져온다.
    """

    def fetch():
        response = requests.get(
            GSCPI_XLS_URL,
            headers=REQUEST_HEADERS,
            timeout=90,
        )

        response.raise_for_status()

        content = response.content

        excel_file = pd.ExcelFile(BytesIO(content))

        target_sheet = None

        for sheet_name in excel_file.sheet_names:
            normalized_name = str(sheet_name).strip().lower()

            if "monthly" in normalized_name and "data" in normalized_name:
                target_sheet = sheet_name
                break

        if target_sheet is None:
            target_sheet = excel_file.sheet_names[-1]

        dataframe = pd.read_excel(
            BytesIO(content),
            sheet_name=target_sheet,
        )

        normalized_columns = {
            str(column).strip().lower(): column
            for column in dataframe.columns
        }

        date_column = normalized_columns.get(
            "date",
            dataframe.columns[0],
        )

        value_column = normalized_columns.get(
            "gscpi",
            dataframe.columns[1],
        )

        output = dataframe[
            [date_column, value_column]
        ].copy()

        output.columns = ["date", "gscpi"]

        output["date"] = pd.to_datetime(
            output["date"],
            errors="coerce",
        )

        output["gscpi"] = pd.to_numeric(
            output["gscpi"],
            errors="coerce",
        )

        output["year"] = output["date"].dt.year

        return (
            output
            .dropna(subset=["date", "gscpi"])
            .sort_values("date")
            .reset_index(drop=True)
        )

    dataframe = get_cached_or_fetch(
        "newyorkfed_gscpi",
        fetch,
        force_refresh=force_refresh,
    )

    if not dataframe.empty:
        dataframe["date"] = pd.to_datetime(
            dataframe["date"],
            errors="coerce",
        )

        dataframe["year"] = pd.to_numeric(
            dataframe["year"],
            errors="coerce",
        ).astype("Int64")

    return dataframe

# =========================================================
# 5. Static Data Generation
# =========================================================
def dataframe_to_records(dataframe, date_columns=None):
    """DataFrame을 JSON 저장용 record 배열로 전환한다."""
    if dataframe is None or dataframe.empty:
        return []

    output = dataframe.copy()

    if date_columns:
        for column in date_columns:
            if column in output.columns:
                output[column] = pd.to_datetime(
                    output[column],
                    errors="coerce",
                ).dt.strftime("%Y-%m-%d")

    output = output.where(pd.notnull(output), None)

    return output.to_dict(orient="records")

def collect_dashboard_data(force_refresh=False):
    """
    모든 원천 데이터를 수집하고 정적 페이지에서 사용할 구조로 정리한다.
    """
    meta = get_country_meta(force_refresh=force_refresh)

    gdp = get_worldbank_indicator(
        WB_GDP_API_URL,
        "gdp_growth",
        force_refresh=force_refresh,
    )

    inflation = get_worldbank_indicator(
        WB_INFLATION_API_URL,
        "inflation",
        force_refresh=force_refresh,
    )

    gscpi = get_gscpi_data(
        force_refresh=force_refresh,
    )

    gdp = gdp.merge(
        meta,
        on=["code", "country"],
        how="left",
    )

    inflation = inflation.merge(
        meta,
        on=["code", "country"],
        how="left",
    )

    gdp = gdp.dropna(
        subset=["country", "year", "gdp_growth"]
    )

    inflation = inflation.dropna(
        subset=["country", "year", "inflation"]
    )

    gdp = gdp.sort_values(
        ["country", "year"]
    ).reset_index(drop=True)

    inflation = inflation.sort_values(
        ["country", "year"]
    ).reset_index(drop=True)

    gscpi = gscpi.sort_values(
        "date"
    ).reset_index(drop=True)

    return {
        "countries": dataframe_to_records(meta),
        "gdp": dataframe_to_records(gdp),
        "inflation": dataframe_to_records(inflation),
        "gscpi": dataframe_to_records(
            gscpi,
            date_columns=["date"],
        ),
    }

def write_static_json_files(data):
    """GitHub Pages용 JSON 데이터 파일을 생성한다."""
    macro_payload = {
        "countries": data["countries"],
        "gdp": data["gdp"],
        "inflation": data["inflation"],
    }

    gscpi_payload = {
        "gscpi": data["gscpi"],
    }

    fetched_at = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    meta_payload = {
        "generated_at": fetched_at,
        "sources": {
            "gdp": "World Bank Open Data",
            "inflation": "World Bank Open Data",
            "gscpi": "Federal Reserve Bank of New York",
        },
        "record_count": {
            "countries": len(data["countries"]),
            "gdp": len(data["gdp"]),
            "inflation": len(data["inflation"]),
            "gscpi": len(data["gscpi"]),
        },
    }

    MACRO_JSON_PATH.write_text(
        json.dumps(
            macro_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    GSCPI_JSON_PATH.write_text(
        json.dumps(
            gscpi_payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    META_JSON_PATH.write_text(
        json.dumps(
            meta_payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log(f"[STATIC] 생성 완료: {MACRO_JSON_PATH.name}")
    log(f"[STATIC] 생성 완료: {GSCPI_JSON_PATH.name}")
    log(f"[STATIC] 생성 완료: {META_JSON_PATH.name}")

# =========================================================
# 6. Static HTML
# =========================================================
STATIC_HTML = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">

<title>GDP, Inflation & GSCPI Dashboard</title>

<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>

<style>
:root {
    --navy: #12355b;
    --blue: #1d4ed8;
    --teal: #0f766e;
    --red: #b91c1c;
    --bg: #f3f6fb;
    --card: #ffffff;
    --line: #d9e2ec;
    --text: #1f2937;
    --muted: #6b7280;
    --warn-bg: #fff8e1;
    --warn-text: #7a5a00;
}

* {
    box-sizing: border-box;
}

body {
    font-family: "Segoe UI", Arial, sans-serif;
    margin: 0;
    background: var(--bg);
    color: var(--text);
}

.wrap {
    max-width: 1520px;
    margin: 0 auto;
    padding: 24px;
}

.hero {
    background: linear-gradient(135deg, var(--navy), var(--teal));
    color: #ffffff;
    padding: 28px;
    border-radius: 18px;
    margin-bottom: 20px;
}

.hero h1 {
    margin: 0 0 10px 0;
    font-size: 30px;
}

.meta {
    font-size: 13px;
    opacity: 0.95;
    line-height: 1.7;
}

.tabs {
    display: flex;
    gap: 10px;
    margin-bottom: 18px;
}

.tab {
    display: inline-block;
    padding: 11px 18px;
    border-radius: 12px;
    text-decoration: none;
    background: #dbe4f0;
    color: #16324f;
    font-weight: 700;
    cursor: pointer;
}

.tab.active {
    background: var(--navy);
    color: #ffffff;
}

.panel {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 16px;
    padding: 18px;
    box-shadow: 0 4px 14px rgba(18, 53, 91, 0.06);
    margin-bottom: 18px;
    width: 100%;
}

.section-title {
    margin: 0 0 14px 0;
    font-size: 18px;
    color: var(--navy);
}

.grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 16px;
}

.row2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
}

.kpi {
    background: linear-gradient(180deg, #ffffff, #f8fbff);
    border: 1px solid var(--line);
    border-left: 6px solid var(--blue);
    padding: 16px;
    border-radius: 14px;
}

.kpi .label {
    color: var(--muted);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.4px;
}

.kpi .value {
    margin-top: 8px;
    font-size: 23px;
    font-weight: 800;
    color: var(--navy);
    word-break: break-word;
}

label {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-bottom: 6px;
    font-weight: 600;
}

select {
    width: 100%;
    padding: 11px;
    border: 1px solid var(--line);
    border-radius: 10px;
    background: #ffffff;
}

button,
.download-button {
    padding: 10px 14px;
    border: none;
    background: var(--navy);
    color: #ffffff;
    border-radius: 10px;
    cursor: pointer;
    text-decoration: none;
    display: inline-block;
    font-weight: 600;
}

button:hover,
.download-button:hover {
    opacity: 0.9;
}

.select-tools {
    display: flex;
    gap: 7px;
    margin: 0 0 7px 0;
    flex-wrap: wrap;
}

.select-tools button {
    padding: 6px 10px;
    font-size: 12px;
    background: #475569;
}

.select-tools button:first-child {
    background: var(--teal);
}

.btn-row {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
    flex-wrap: wrap;
}

.icon-btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 72px;
    height: 32px;
    border-radius: 8px;
    text-decoration: none;
    color: #ffffff;
    font-size: 12px;
    font-weight: 700;
    border: none;
    cursor: pointer;
}

.icon-csv {
    background: #2563eb;
}

.warning {
    background: var(--warn-bg);
    color: var(--warn-text);
    border: 1px solid #f5d76e;
    padding: 14px;
    border-radius: 12px;
    margin-bottom: 16px;
    display: none;
}

.loading {
    padding: 50px;
    text-align: center;
    font-weight: 700;
    color: var(--muted);
}

.empty {
    padding: 40px;
    text-align: center;
    color: var(--muted);
}

.chart {
    width: 100%;
    min-height: 430px;
}

ul.exec {
    margin: 0;
    padding-left: 18px;
}

ul.exec li {
    margin: 10px 0;
    line-height: 1.6;
}

.subnote {
    margin-top: 8px;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.6;
}

.hidden {
    display: none;
}

.source-link {
    color: inherit;
    text-decoration: underline;
}

@media (max-width: 1050px) {
    .grid {
        grid-template-columns: repeat(2, 1fr);
    }

    .row2 {
        grid-template-columns: 1fr;
    }
}

@media (max-width: 650px) {
    .wrap {
        padding: 14px;
    }

    .hero {
        padding: 20px;
    }

    .hero h1 {
        font-size: 24px;
    }

    .grid {
        grid-template-columns: 1fr;
    }

    .tabs {
        flex-wrap: wrap;
    }
}
</style>
</head>

<body>
<div class="wrap">

    <div class="hero">
        <h1>GDP, Inflation &amp; GSCPI</h1>
        <div class="meta">
            <div>
                <b>출처</b> |
                GDP, Inflation:
                <a class="source-link"
                   href="https://data.worldbank.org/"
                   target="_blank"
                   rel="noopener noreferrer">
                    World Bank Open Data
                </a>
                /
                GSCPI:
                <a class="source-link"
                   href="https://www.newyorkfed.org/research/policy/gscpi"
                   target="_blank"
                   rel="noopener noreferrer">
                    Federal Reserve Bank of New York
                </a>
            </div>
            <div>
                <b>GitHub Actions 갱신 시각</b> |
                <span id="generatedAt">데이터 로딩 중</span>
            </div>
        </div>
    </div>

    <div id="warning" class="warning"></div>

    <div class="tabs">
        <a id="macroTab" class="tab active" onclick="showTab('macro')">
            GDP + Inflation
        </a>

        <a id="gscpiTab" class="tab" onclick="showTab('gscpi')">
            GSCPI
        </a>
    </div>

    <div id="loading" class="loading">
        GitHub Pages 데이터 파일을 불러오는 중입니다.
    </div>

    <div id="macroSection" class="hidden">

        <div class="panel">
            <h3 class="section-title">공통 필터</h3>

            <div class="grid">
                <div>
                    <label for="regionSelect">지역 선택</label>
                    <select id="regionSelect" onchange="onRegionChange()"></select>
                </div>

                <div>
                    <label for="yearsSelect">기간 선택</label>
                    <select id="yearsSelect" onchange="renderMacroDashboard()">
                        <option value="5">최근 5개년</option>
                        <option value="10" selected>최근 10개년</option>
                        <option value="20">최근 20개년</option>
                    </select>
                </div>

                <div style="grid-column:span 2;">
                    <label for="countrySelect">국가 선택</label>

                    <div class="select-tools">
                        <button type="button" onclick="setAllCountries(true)">
                            권역 전체 선택
                        </button>

                        <button type="button" onclick="setAllCountries(false)">
                            선택 해제
                        </button>
                    </div>

                    <select
                        id="countrySelect"
                        multiple
                        size="8"
                        onchange="renderMacroDashboard()">
                    </select>
                </div>
            </div>

            <div class="subnote">
                최초 화면은 G7 및 한국을 기본으로 선택합니다.
                지역 변경 시 해당 권역의 국가가 전체 선택됩니다.
                Ctrl 또는 Shift 키를 활용하여 국가를 추가·제외할 수 있습니다.
            </div>
        </div>

        <div class="grid">
            <div class="kpi">
                <div class="label">Selected Countries</div>
                <div class="value" id="selectedCountriesKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">GDP Latest Year</div>
                <div class="value" id="latestGdpYearKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">Inflation Latest Year</div>
                <div class="value" id="latestInflationYearKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">Selected Region</div>
                <div class="value" id="selectedRegionKpi">-</div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">GDP 성장률 추이</h3>

                <div class="btn-row">
                    <button class="icon-btn icon-csv"
                            onclick="downloadMacroData('gdp')">
                        CSV 다운로드
                    </button>
                </div>

                <div id="gdpLine" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">Inflation 추이</h3>

                <div class="btn-row">
                    <button class="icon-btn icon-csv"
                            onclick="downloadMacroData('inflation')">
                        CSV 다운로드
                    </button>
                </div>

                <div id="inflationLine" class="chart"></div>
            </div>
        </div>

        <div class="panel">
            <h3 class="section-title">GDP / Inflation 평균 비교</h3>

            <div class="btn-row">
                <button class="icon-btn icon-csv"
                        onclick="downloadMacroData('macro_compare')">
                    CSV 다운로드
                </button>
            </div>

            <div id="macroCompareChart" class="chart"></div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">
                    국가별 최신 GDP 성장률 비교
                </h3>

                <div id="gdpBar" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    국가별 최신 Inflation 비교
                </h3>

                <div id="inflationBar" class="chart"></div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">GDP 시사점</h3>
                <ul class="exec" id="gdpInsights"></ul>
            </div>

            <div class="panel">
                <h3 class="section-title">Inflation 시사점</h3>
                <ul class="exec" id="inflationInsights"></ul>
            </div>
        </div>
    </div>

    <div id="gscpiSection" class="hidden">

        <div class="panel">
            <h3 class="section-title">GSCPI 필터</h3>

            <div class="grid">
                <div>
                    <label for="gscpiYearsSelect">기간 선택</label>

                    <select
                        id="gscpiYearsSelect"
                        onchange="renderGscpiDashboard()">
                        <option value="5">최근 5개년</option>
                        <option value="10" selected>최근 10개년</option>
                        <option value="20">최근 20개년</option>
                    </select>
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="kpi">
                <div class="label">Latest Point</div>
                <div class="value" id="latestGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">Average, Last 12M</div>
                <div class="value" id="avgGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">Maximum, Last 12M</div>
                <div class="value" id="maxGscpiKpi">-</div>
            </div>

            <div class="kpi">
                <div class="label">Minimum, Last 12M</div>
                <div class="value" id="minGscpiKpi">-</div>
            </div>
        </div>

        <div class="row2">
            <div class="panel">
                <h3 class="section-title">GSCPI 추이</h3>

                <div class="btn-row">
                    <button class="icon-btn icon-csv"
                            onclick="downloadGscpiData()">
                        CSV 다운로드
                    </button>
                </div>

                <div id="gscpiLine" class="chart"></div>
            </div>

            <div class="panel">
                <h3 class="section-title">
                    최근 12개월 GSCPI 비교
                </h3>

                <div id="gscpiBar" class="chart"></div>
            </div>
        </div>

        <div class="panel">
            <h3 class="section-title">GSCPI 시사점</h3>
            <ul class="exec" id="gscpiInsights"></ul>
        </div>
    </div>
</div>

<script>
let macroData = {
    countries: [],
    gdp: [],
    inflation: []
};

let gscpiData = {
    gscpi: []
};

let activeTab = "macro";

function showWarning(message) {
    const warning = document.getElementById("warning");

    warning.innerText = message;
    warning.style.display = "block";
}

function hideWarning() {
    document.getElementById("warning").style.display = "none";
}

function numberFormat(value, digits = 2) {
    const number = Number(value);

    if (!Number.isFinite(number)) {
        return "-";
    }

    return number.toLocaleString(
        undefined,
        {
            minimumFractionDigits: digits,
            maximumFractionDigits: digits
        }
    );
}

function chartLayout(title, yAxisTitle) {
    return {
        title: {
            text: title || "",
            font: {
                size: 15,
                color: "#12355b"
            }
        },
        template: "plotly_white",
        height: 440,
        margin: {
            l: 55,
            r: 30,
            t: title ? 60 : 30,
            b: 65
        },
        xaxis: {
            gridcolor: "#edf2f7"
        },
        yaxis: {
            title: yAxisTitle || "",
            gridcolor: "#edf2f7",
            zerolinecolor: "#cbd5e1"
        },
        legend: {
            orientation: "h",
            yanchor: "bottom",
            y: 1.03,
            xanchor: "right",
            x: 1
        }
    };
}

function emptyChart(elementId, message) {
    const element = document.getElementById(elementId);

    element.innerHTML =
        "<div class='empty'>"
        + message
        + "</div>";
}

function getSelectedCountries() {
    const select = document.getElementById("countrySelect");

    return Array.from(select.selectedOptions).map(
        option => option.value
    );
}

function getSelectedYears() {
    return Number(
        document.getElementById("yearsSelect").value
    );
}

function getSelectedGscpiYears() {
    return Number(
        document.getElementById("gscpiYearsSelect").value
    );
}

function uniqueRegions() {
    const regions = macroData.countries
        .map(item => item.region)
        .filter(Boolean);

    return [...new Set(regions)].sort();
}

function populateRegionSelect() {
    const select = document.getElementById("regionSelect");

    select.innerHTML = "";

    const allOption = new Option("전체", "전체");
    select.add(allOption);

    uniqueRegions().forEach(region => {
        select.add(new Option(region, region));
    });
}

function populateCountrySelect(
    selectedRegion,
    selectedCountries = []
) {
    const countrySelect = document.getElementById("countrySelect");

    let countries = macroData.countries;

    if (selectedRegion !== "전체") {
        countries = countries.filter(
            item => item.region === selectedRegion
        );
    }

    countries = countries
        .map(item => item.country)
        .filter(Boolean)
        .sort();

    countrySelect.innerHTML = "";

    countries.forEach(country => {
        const option = new Option(country, country);

        option.selected = selectedCountries.includes(country);

        countrySelect.add(option);
    });
}

function setAllCountries(selected) {
    const countrySelect = document.getElementById("countrySelect");

    Array.from(countrySelect.options).forEach(option => {
        option.selected = selected;
    });

    renderMacroDashboard();
}

function onRegionChange() {
    const selectedRegion =
        document.getElementById("regionSelect").value;

    const countries = macroData.countries
        .filter(item => {
            return (
                selectedRegion === "전체"
                || item.region === selectedRegion
            );
        })
        .map(item => item.country);

    populateCountrySelect(selectedRegion, countries);

    renderMacroDashboard();
}

function yearsFilter(data, years) {
    if (!data || data.length === 0) {
        return [];
    }

    const maxYear = Math.max(
        ...data.map(item => Number(item.year))
    );

    const minYear = maxYear - years + 1;

    return data.filter(item => Number(item.year) >= minYear);
}

function selectedMacroData(metric) {
    const countries = getSelectedCountries();
    const years = getSelectedYears();

    const source =
        metric === "gdp"
            ? macroData.gdp
            : macroData.inflation;

    const selected = source.filter(item => {
        return countries.includes(item.country);
    });

    return yearsFilter(selected, years);
}

function latestByCountry(data, fieldName) {
    const map = new Map();

    data.forEach(item => {
        const current = map.get(item.country);

        if (
            !current
            || Number(item.year) > Number(current.year)
        ) {
            map.set(item.country, item);
        }
    });

    return Array.from(map.values()).sort(
        (a, b) => Number(b[fieldName]) - Number(a[fieldName])
    );
}

function groupByCountry(data) {
    const grouped = {};

    data.forEach(item => {
        if (!grouped[item.country]) {
            grouped[item.country] = [];
        }

        grouped[item.country].push(item);
    });

    Object.keys(grouped).forEach(country => {
        grouped[country].sort(
            (a, b) => Number(a.year) - Number(b.year)
        );
    });

    return grouped;
}

function drawLineChart(
    elementId,
    data,
    fieldName,
    yAxisTitle,
    colorPalette
) {
    if (!data.length) {
        emptyChart(elementId, "데이터 없음");
        return;
    }

    const grouped = groupByCountry(data);

    const traces = Object.keys(grouped).map(
        (country, index) => {
            const rows = grouped[country];

            return {
                x: rows.map(item => item.year),
                y: rows.map(item => item[fieldName]),
                mode: "lines+markers",
                type: "scatter",
                name: country,
                line: {
                    width: 2.5,
                    color: colorPalette[
                        index % colorPalette.length
                    ]
                },
                marker: {
                    size: 6
                }
            };
        }
    );

    Plotly.react(
        elementId,
        traces,
        chartLayout("", yAxisTitle),
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function drawBarChart(
    elementId,
    data,
    fieldName,
    yAxisTitle,
    barColor
) {
    if (!data.length) {
        emptyChart(elementId, "데이터 없음");
        return;
    }

    const sorted = [...data].sort(
        (a, b) => Number(a[fieldName]) - Number(b[fieldName])
    );

    const trace = {
        x: sorted.map(item => item[fieldName]),
        y: sorted.map(item => item.country),
        type: "bar",
        orientation: "h",
        marker: {
            color: barColor
        },
        text: sorted.map(
            item => numberFormat(item[fieldName], 2)
        ),
        textposition: "auto",
        hovertemplate:
            "%{y}<br>"
            + yAxisTitle
            + ": %{x:.2f}%<extra></extra>"
    };

    const layout = chartLayout("", yAxisTitle);

    layout.margin.l = 145;

    Plotly.react(
        elementId,
        [trace],
        layout,
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function drawMacroCompareChart(gdp, inflation) {
    if (!gdp.length && !inflation.length) {
        emptyChart(
            "macroCompareChart",
            "데이터 없음"
        );
        return;
    }

    const gdpYearMap = {};
    const inflationYearMap = {};

    gdp.forEach(item => {
        if (!gdpYearMap[item.year]) {
            gdpYearMap[item.year] = [];
        }

        gdpYearMap[item.year].push(
            Number(item.gdp_growth)
        );
    });

    inflation.forEach(item => {
        if (!inflationYearMap[item.year]) {
            inflationYearMap[item.year] = [];
        }

        inflationYearMap[item.year].push(
            Number(item.inflation)
        );
    });

    const gdpYears = Object.keys(gdpYearMap)
        .map(Number)
        .sort((a, b) => a - b);

    const inflationYears = Object.keys(inflationYearMap)
        .map(Number)
        .sort((a, b) => a - b);

    const traces = [];

    if (gdpYears.length) {
        traces.push({
            x: gdpYears,
            y: gdpYears.map(year => {
                const values = gdpYearMap[year];

                return values.reduce(
                    (sum, value) => sum + value,
                    0
                ) / values.length;
            }),
            mode: "lines+markers",
            type: "scatter",
            name: "GDP 평균",
            line: {
                color: "#1d4ed8",
                width: 3
            },
            marker: {
                size: 7
            },
            yaxis: "y"
        });
    }

    if (inflationYears.length) {
        traces.push({
            x: inflationYears,
            y: inflationYears.map(year => {
                const values = inflationYearMap[year];

                return values.reduce(
                    (sum, value) => sum + value,
                    0
                ) / values.length;
            }),
            mode: "lines+markers",
            type: "scatter",
            name: "Inflation 평균",
            line: {
                color: "#b91c1c",
                width: 3
            },
            marker: {
                size: 7
            },
            yaxis: "y2"
        });
    }

    const layout = chartLayout(
        "",
        "GDP 성장률 (%)"
    );

    layout.yaxis2 = {
        title: "Inflation (%)",
        overlaying: "y",
        side: "right",
        gridcolor: "#edf2f7"
    };

    Plotly.react(
        "macroCompareChart",
        traces,
        layout,
        {
            responsive: true,
            displaylogo: false
        }
    );
}

function buildMacroInsights(data, fieldName, metricName) {
    if (!data.length) {
        return [
            "관련 데이터가 없어 정량적 시사점 도출이 제한됩니다."
        ];
    }

    const latest = latestByCountry(data, fieldName);

    const values = latest.map(
        item => Number(item[fieldName])
    );

    const average =
        values.reduce((sum, value) => sum + value, 0)
        / values.length;

    const maximum = latest.reduce(
        (current, item) => {
            return Number(item[fieldName])
                > Number(current[fieldName])
                ? item
                : current;
        },
        latest[0]
    );

    const minimum = latest.reduce(
        (current, item) => {
            return Number(item[fieldName])
                < Number(current[fieldName])
                ? item
                : current;
        },
        latest[0]
    );

    const spread =
        Number(maximum[fieldName])
        - Number(minimum[fieldName]);

    const yearly = {};

    data.forEach(item => {
        if (!yearly[item.year]) {
            yearly[item.year] = [];
        }

        yearly[item.year].push(
            Number(item[fieldName])
        );
    });

    const years = Object.keys(yearly)
        .map(Number)
        .sort((a, b) => a - b);

    let trendText =
        metricName + " 추세 판단: 비교 가능한 연도 수 부족";

    if (years.length >= 2) {
        const latestYear = years[years.length - 1];
        const previousYear = years[years.length - 2];

        const latestAverage =
            yearly[latestYear].reduce(
                (sum, value) => sum + value,
                0
            ) / yearly[latestYear].length;

        const previousAverage =
            yearly[previousYear].reduce(
                (sum, value) => sum + value,
                0
            ) / yearly[previousYear].length;

        const difference =
            latestAverage - previousAverage;

        if (difference > 0) {
            trendText =
                "전년 대비 평균 "
                + metricName
                + ": "
                + numberFormat(difference, 2)
                + "p 상승";
        } else if (difference < 0) {
            trendText =
                "전년 대비 평균 "
                + metricName
                + ": "
                + numberFormat(
                    Math.abs(difference),
                    2
                )
                + "p 하락";
        } else {
            trendText =
                "전년 대비 평균 "
                + metricName
                + ": 유사한 수준 유지";
        }
    }

    const levelText =
        average >= 0
            ? "최신 평균 "
              + metricName
              + ": "
              + numberFormat(average, 2)
              + "% (플러스 영역)"
            : "최신 평균 "
              + metricName
              + ": "
              + numberFormat(average, 2)
              + "% (마이너스 영역)";

    return [
        levelText,
        "최고 국가: "
            + maximum.country
            + " ("
            + numberFormat(maximum[fieldName], 2)
            + "%)",
        "최저 국가: "
            + minimum.country
            + " ("
            + numberFormat(minimum[fieldName], 2)
            + "%)",
        "국가 간 격차: "
            + numberFormat(spread, 2)
            + "p",
        trendText
    ];
}

function renderInsightList(elementId, insights) {
    const element = document.getElementById(elementId);

    element.innerHTML = insights.map(
        insight => "<li>" + insight + "</li>"
    ).join("");
}

function renderMacroDashboard() {
    if (!macroData.gdp.length && !macroData.inflation.length) {
        return;
    }

    const selectedRegion =
        document.getElementById("regionSelect").value;

    const selectedCountries = getSelectedCountries();

    const gdp = selectedMacroData("gdp");
    const inflation = selectedMacroData("inflation");

    document.getElementById("selectedCountriesKpi").innerText =
        selectedCountries.length;

    document.getElementById("selectedRegionKpi").innerText =
        selectedRegion;

    document.getElementById("latestGdpYearKpi").innerText =
        gdp.length
            ? Math.max(...gdp.map(item => Number(item.year)))
            : "-";

    document.getElementById("latestInflationYearKpi").innerText =
        inflation.length
            ? Math.max(
                ...inflation.map(item => Number(item.year))
            )
            : "-";

    drawLineChart(
        "gdpLine",
        gdp,
        "gdp_growth",
        "GDP 성장률 (%)",
        [
            "#1d4ed8",
            "#0f766e",
            "#b91c1c",
            "#7c3aed",
            "#c2410c",
            "#0369a1",
            "#15803d",
            "#be123c"
        ]
    );

    drawLineChart(
        "inflationLine",
        inflation,
        "inflation",
        "Inflation (%)",
        [
            "#b91c1c",
            "#c2410c",
            "#7c3aed",
            "#0369a1",
            "#0f766e",
            "#1d4ed8",
            "#15803d",
            "#be123c"
        ]
    );

    drawMacroCompareChart(gdp, inflation);

    drawBarChart(
        "gdpBar",
        latestByCountry(gdp, "gdp_growth"),
        "gdp_growth",
        "GDP 성장률 (%)",
        "#1d4ed8"
    );

    drawBarChart(
        "inflationBar",
        latestByCountry(inflation, "inflation"),
        "Inflation (%)",
        "#b91c1c"
    );

    renderInsightList(
        "gdpInsights",
        buildMacroInsights(
            gdp,
            "gdp_growth",
            "GDP 성장률"
        )
    );

    renderInsightList(
        "inflationInsights",
        buildMacroInsights(
            inflation,
            "inflation",
            "Inflation"
        )
    );
}

function selectedGscpiData() {
    const years = getSelectedGscpiYears();

    if (!gscpiData.gscpi.length) {
        return [];
    }

    const maxYear = Math.max(
        ...gscpiData.gscpi.map(
            item => Number(item.year)
        )
    );

    const minYear = maxYear - years + 1;

    return gscpiData.gscpi.filter(
        item => Number(item.year) >= minYear
    );
}

function buildGscpiInsights(data) {
    if (!data.length) {
        return [
            "관련 데이터가 없어 공급망 시사점 도출이 제한됩니다."
        ];
    }

    const sorted = [...data].sort(
        (a, b) => new Date(a.date) - new Date(b.date)
    );

    const latest = sorted[sorted.length - 1];
    const last12 = sorted.slice(-12);

    const values = last12.map(
        item => Number(item.gscpi)
    );

    const average =
        values.reduce((sum, value) => sum + value, 0)
        / values.length;

    const maximum = Math.max(...values);
    const minimum = Math.min(...values);
    const amplitude = maximum - minimum;

    let trend =
        "1년 비교 판단: 데이터 부족";

    if (sorted.length >= 13) {
        const previous = Number(
            sorted[sorted.length - 13].gscpi
        );

        const difference =
            Number(latest.gscpi) - previous;

        if (difference > 0) {
            trend =
                "1년 전 대비 GSCPI: "
                + numberFormat(difference, 2)
                + "p 상승";
        } else if (difference < 0) {
            trend =
                "1년 전 대비 GSCPI: "
                + numberFormat(
                    Math.abs(difference),
                    2
                )
                + "p 하락";
        } else {
            trend =
                "1년 전 대비 GSCPI: 유사한 수준 유지";
        }
    }

    let level =
        "최근 수준 평가: 최근 12개월 평균과 유사";

    if (Number(latest.gscpi) > average) {
        level =
            "최근 수준 평가: 최근 12개월 평균 상회";
    } else if (Number(latest.gscpi) < average) {
        level =
            "최근 수준 평가: 최근 12개월 평균 하회";
    }

    return [
        "최신 시점: " + latest.date.substring(0, 7),
        "최신 GSCPI: "
            + numberFormat(latest.gscpi, 2),
        "최근 12개월 평균: "
            + numberFormat(average, 2),
        "최근 12개월 범위: "
            + numberFormat(minimum, 2)
            + " ~ "
            + numberFormat(maximum, 2),
        "최근 12개월 변동폭: "
            + numberFormat(amplitude, 2)
            + "p",
        level,
        trend
    ];
}

function renderGscpiDashboard() {
    const data = selectedGscpiData();

    if (!data.length) {
        emptyChart("gscpiLine", "데이터 없음");
        emptyChart("gscpiBar", "데이터 없음");
        return;
    }

    const sorted = [...data].sort(
        (a, b) => new Date(a.date) - new Date(b.date)
    );

    const latest12 = sorted.slice(-12);
    const values = latest12.map(
        item => Number(item.gscpi)
    );

    const latest = sorted[sorted.length - 1];

    const average =
        values.reduce((sum, value) => sum + value, 0)
        / values.length;

    document.getElementById("latestGscpiKpi").innerText =
        latest.date.substring(0, 7);

    document.getElementById("avgGscpiKpi").innerText =
        numberFormat(average, 2);

    document.getElementById("maxGscpiKpi").innerText =
        numberFormat(Math.max(...values), 2);

    document.getElementById("minGscpiKpi").innerText =
        numberFormat(Math.min(...values), 2);

    const lineTrace = {
        x: sorted.map(item => item.date),
        y: sorted.map(item => Number(item.gscpi)),
        type: "scatter",
        mode: "lines+markers",
        name: "GSCPI",
        line: {
            color: "#0f766e",
            width: 3
        },
        marker: {
            size: 5
        },
        hovertemplate:
            "%{x|%Y-%m}<br>GSCPI: %{y:.2f}<extra></extra>"
    };

    Plotly.react(
        "gscpiLine",
        [lineTrace],
        chartLayout("", "GSCPI"),
        {
            responsive: true,
            displaylogo: false
        }
    );

    const barTrace = {
        x: latest12.map(
            item => item.date.substring(0, 7)
        ),
        y: latest12.map(
            item => Number(item.gscpi)
        ),
        type: "bar",
        marker: {
            color: "#0f766e"
        },
        hovertemplate:
            "%{x}<br>GSCPI: %{y:.2f}<extra></extra>"
    };

    Plotly.react(
        "gscpiBar",
        [barTrace],
        chartLayout("", "GSCPI"),
        {
            responsive: true,
            displaylogo: false
        }
    );

    renderInsightList(
        "gscpiInsights",
        buildGscpiInsights(sorted)
    );
}

function csvEscape(value) {
    const text = String(value ?? "");

    return '"' + text.replaceAll('"', '""') + '"';
}

function downloadCsv(rows, fileName) {
    if (!rows.length) {
        showWarning("다운로드할 데이터가 없습니다.");
        return;
    }

    const columns = Object.keys(rows[0]);

    const csv = [
        columns.join(","),
        ...rows.map(row => {
            return columns.map(
                column => csvEscape(row[column])
            ).join(",");
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
    anchor.download = fileName;

    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);

    URL.revokeObjectURL(url);
}

function downloadMacroData(dataset) {
    const gdp = selectedMacroData("gdp");
    const inflation = selectedMacroData("inflation");

    if (dataset === "gdp") {
        downloadCsv(
            gdp,
            "worldbank_gdp_growth.csv"
        );
        return;
    }

    if (dataset === "inflation") {
        downloadCsv(
            inflation,
            "worldbank_inflation.csv"
        );
        return;
    }

    const map = new Map();

    gdp.forEach(item => {
        const key =
            item.code + "|" + item.country + "|" + item.year;

        map.set(key, {
            code: item.code,
            country: item.country,
            region: item.region,
            year: item.year,
            gdp_growth: item.gdp_growth,
            inflation: null
        });
    });

    inflation.forEach(item => {
        const key =
            item.code + "|" + item.country + "|" + item.year;

        if (!map.has(key)) {
            map.set(key, {
                code: item.code,
                country: item.country,
                region: item.region,
                year: item.year,
                gdp_growth: null,
                inflation: item.inflation
            });
        } else {
            map.get(key).inflation = item.inflation;
        }
    });

    const rows = Array.from(map.values()).sort(
        (a, b) => {
            const countryOrder =
                a.country.localeCompare(b.country);

            if (countryOrder !== 0) {
                return countryOrder;
            }

            return Number(a.year) - Number(b.year);
        }
    );

    downloadCsv(
        rows,
        "worldbank_gdp_inflation_comparison.csv"
    );
}

function downloadGscpiData() {
    downloadCsv(
        selectedGscpiData(),
        "newyorkfed_gscpi.csv"
    );
}

function showTab(tabName) {
    activeTab = tabName;

    const macroSection =
        document.getElementById("macroSection");

    const gscpiSection =
        document.getElementById("gscpiSection");

    const macroTab =
        document.getElementById("macroTab");

    const gscpiTab =
        document.getElementById("gscpiTab");

    if (tabName === "macro") {
        macroSection.classList.remove("hidden");
        gscpiSection.classList.add("hidden");

        macroTab.classList.add("active");
        gscpiTab.classList.remove("active");

        renderMacroDashboard();
    } else {
        macroSection.classList.add("hidden");
        gscpiSection.classList.remove("hidden");

        macroTab.classList.remove("active");
        gscpiTab.classList.add("active");

        renderGscpiDashboard();
    }
}

async function initializeDashboard() {
    try {
        hideWarning();

        const responses = await Promise.all([
            fetch("./macro_data.json"),
            fetch("./gscpi_data.json"),
            fetch("./dashboard_meta.json")
        ]);

        if (!responses.every(response => response.ok)) {
            throw new Error(
                "정적 데이터 JSON 파일을 불러오지 못했습니다."
            );
        }

        macroData = await responses[0].json();
        gscpiData = await responses[1].json();

        const metaData = await responses[2].json();

        document.getElementById("generatedAt").innerText =
            metaData.generated_at || "-";

        populateRegionSelect();

        const initialCountries =
            DEFAULT_COUNTRIES.filter(country => {
                return macroData.countries.some(
                    item => item.country === country
                );
            });

        populateCountrySelect(
            "전체",
            initialCountries
        );

        document
            .getElementById("loading")
            .classList.add("hidden");

        showTab(activeTab);

    } catch (error) {
        document.getElementById("loading").innerHTML =
            "<div class='empty'>"
            + "대시보드 데이터 로드에 실패했습니다.<br>"
            + "GitHub Actions 실행 결과 및 JSON 파일 배포 여부를 확인하세요."
            + "</div>";

        showWarning(
            "데이터를 불러오지 못했습니다: "
            + error.message
        );

        console.error(error);
    }
}

const DEFAULT_COUNTRIES = [
    "Canada",
    "France",
    "Germany",
    "Italy",
    "Japan",
    "United Kingdom",
    "United States",
    "Korea, Rep."
];

window.addEventListener(
    "DOMContentLoaded",
    initializeDashboard
);
</script>
</body>
</html>
"""

def generate_static_html():
    """GitHub Pages용 index.html 파일을 생성한다."""
    STATIC_HTML_PATH.write_text(
        STATIC_HTML,
        encoding="utf-8",
    )

    log(f"[STATIC] 생성 완료: {STATIC_HTML_PATH.name}")

# =========================================================
# 7. Local Flask Development Mode
# =========================================================
@app.route("/")
def index():
    """
    로컬 검증용 화면.
    GitHub Pages 배포 환경에서는 index.html을 직접 사용한다.
    """
    if not STATIC_HTML_PATH.exists():
        generate_static_html()

    return render_template_string(STATIC_HTML)

@app.route("/download_json/<filename>")
def download_json(filename):
    """
    로컬 개발 시 JSON 데이터 파일 확인을 위한 단순 엔드포인트.
    GitHub Pages에서는 사용하지 않는다.
    """
    allowed_files = {
        "macro_data.json": MACRO_JSON_PATH,
        "gscpi_data.json": GSCPI_JSON_PATH,
        "dashboard_meta.json": META_JSON_PATH,
    }

    target_path = allowed_files.get(filename)

    if target_path is None or not target_path.exists():
        return Response(
            "File not found",
            status=404,
            mimetype="text/plain",
        )

    return Response(
        target_path.read_text(encoding="utf-8"),
        mimetype="application/json; charset=utf-8",
    )

# =========================================================
# 8. GitHub Actions Execution
# =========================================================
def run_cron_job():
    """
    GitHub Actions 전용 실행 함수.

    1) 원천 데이터 최신 수집
    2) SQLite 캐시 갱신
    3) macro_data.json 생성
    4) gscpi_data.json 생성
    5) dashboard_meta.json 생성
    6) index.html 생성
    """
    log("=" * 60)
    log("[CRON] Macro Economies Dashboard 갱신 시작")
    log("=" * 60)

    init_cache_db()

    try:
        data = collect_dashboard_data(force_refresh=True)

        write_static_json_files(data)
        generate_static_html()

        log("[CRON] 정적 대시보드 생성 완료")

    except Exception as error:
        log(f"[ERROR] 정적 대시보드 생성 실패: {error}")
        raise

    log("=" * 60)
    log("[CRON] Macro Economies Dashboard 갱신 종료")
    log("=" * 60)

# =========================================================
# 9. Program Entry Point
# =========================================================
def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "GDP, Inflation & GSCPI Dashboard "
            "(GitHub Pages / Local Flask)"
        )
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help=(
            "GitHub Actions용 실행 모드: "
            "데이터 수집, SQLite 저장, JSON 및 index.html 생성 후 종료"
        ),
    )

    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="로컬 Flask 서버 Host",
    )

    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help=(
            "로컬 Flask 서버 Port. "
            "0이면 사용 가능한 포트를 자동 탐색"
        ),
    )

    parser.add_argument(
        "--no-open",
        action="store_true",
        help="로컬 실행 시 브라우저 자동 실행 비활성화",
    )

    parser.add_argument(
        "--refresh",
        action="store_true",
        help="로컬 실행 전 최신 데이터를 강제로 수집",
    )

    return parser.parse_args()

def main():
    args = parse_args()

    init_cache_db()

    if args.cron:
        run_cron_job()
        sys.exit(0)

    # 로컬 검증 모드에서도 최초 실행에 필요한 파일을 생성한다.
    try:
        data = collect_dashboard_data(
            force_refresh=args.refresh
        )

        write_static_json_files(data)
        generate_static_html()

    except Exception as error:
        log(f"[WARN] 데이터 갱신 실패: {error}")
        log("[WARN] 기존 정적 파일 또는 SQLite 캐시가 있으면 사용합니다.")

        if not STATIC_HTML_PATH.exists():
            generate_static_html()

    host = args.host

    port = (
        args.port
        if args.port > 0
        else find_available_port(host)
    )

    local_url = f"http://{host}:{port}"

    log(f"[INFO] SQLite 캐시 DB: {DB_PATH}")
    log(f"[INFO] Flask 서버 시작: {local_url}")

    if not args.no_open:
        threading.Thread(
            target=wait_and_open_browser,
            args=(local_url, port),
            daemon=True,
        ).start()

    app.run(
        host=host,
        port=port,
        debug=False,
        use_reloader=False,
    )

if __name__ == "__main__":
    main()
