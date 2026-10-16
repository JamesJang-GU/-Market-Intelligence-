# -*- coding: utf-8 -*-
import argparse
import datetime as dt
import json
import socket
import sqlite3
import sys
import threading
import time
import webbrowser
from io import StringIO
from pathlib import Path

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

EXPECTED_COLUMNS = ["rank", "provider", "hq", "revenue", "teu", "air_tons"]

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
    text = "".join(char for char in text if char.isdigit() or char == ".")

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
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

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
            with socket.create_connection(("127.0.0.1", port), timeout=1):
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
            dataframe = pd.read_html(StringIO(str(table)))[0]
        except Exception:
            continue

        if dataframe is None or dataframe.empty:
            continue

        content = (
            " ".join(map(str, dataframe.columns))
            + " "
            + " ".join(map(str, dataframe.head(5).values.ravel()))
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
            for company in ["dhl", "kuehne", "db schenker", "dsv", "ceva"]
        ):
            score += 3

        candidates.append((score, index, dataframe))

    if not candidates:
        raise RuntimeError("pandas로 읽을 수 있는 표를 찾지 못했습니다.")

    candidates.sort(key=lambda item: (item[0], -item[1]), reverse=True)

    best_score, _, best_dataframe = candidates[0]

    if best_score <= 0:
        raise RuntimeError("Top 25 Freight Forwarder 표를 식별하지 못했습니다.")

    return best_dataframe

def fetch_top_25_data(url=DEFAULT_URL):
    """
    A&A Top 25 Global Freight Forwarders 데이터를 가져와 표준 형식으로 정리한다.
    """
    log("[FETCH] 최신 A&A Top 25 Global Freight Forwarders 데이터 수집 중...")

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
            f"예상보다 컬럼 수가 적습니다. 현재 표의 크기: {raw_dataframe.shape}"
        )

    dataframe = raw_dataframe.iloc[:, :6].copy()
    dataframe.columns = EXPECTED_COLUMNS

    for column in ["rank", "revenue", "teu", "air_tons"]:
        dataframe[column] = dataframe[column].apply(clean_number)

    dataframe["rank"] = dataframe["rank"].astype(int)
    dataframe["provider"] = dataframe["provider"].apply(normalize_provider_name)
    dataframe["hq"] = dataframe["hq"].astype(str).str.strip()
    dataframe["date"] = dt.date.today().isoformat()
    dataframe["source_url"] = url

    dataframe = dataframe[
        dataframe["provider"].astype(str).str.len() > 0
    ]

    dataframe = dataframe.sort_values("rank").reset_index(drop=True)

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

    connection
