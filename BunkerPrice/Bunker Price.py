# -*- coding: utf-8 -*-
import sys
import warnings
import re
import sqlite3
import os
import argparse
from pathlib import Path
from datetime import datetime
import urllib.parse
import time
import socket
import threading
import webbrowser

warnings.filterwarnings("ignore")

from flask import Flask, render_template_string, request, Response
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
import pandas as pd
import plotly.graph_objects as go

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
ALL_PORTS = ["Rotterdam", "Fujairah", "Singapore"]

BASE_DIR = Path(__file__).resolve().parent
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
FORECAST_CACHE = {"df": None, "fetched_at": None, "message": ""}
FORECAST_PRODUCT_COLUMNS = ["VLSFO", "MGO", "LSMGO", "HSFO"]

# =========================
# SQLite DB 유틸
# =========================
def get_db_path():
    return BASE_DIR / DB_FILENAME

def get_db_conn():
    conn = sqlite3.connect(get_db_path())
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
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
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
        row = conn.execute("SELECT fetched_at FROM data_meta WHERE dataset=?", (dataset,)).fetchone()
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
    print(f"[INFO] DB 저장 완료: table={table_name} / rows={len(out)}")
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
            if (!chart.rangeSelector || !chart.rangeSelector.buttons || chart.rangeSelector.buttons.length === 0) return {ok:false};
            const lastIndex = chart.rangeSelector.buttons.length - 1;
            try {
                chart.rangeSelector.clickButton(lastIndex, true);
                return {ok:true, chartIndex:i};
            } catch(e) { return {ok:false}; }
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
                if (!series || !series.name || !series.name.includes('VLSFO')) return;
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
                const points = rawPoints.map(normalizePoint).filter(p => p && p.x !== undefined && p.y !== undefined && p.y !== null);
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
    s = str(period or "").strip()
    m = re.search(r"(20\d{2})\s*Q([1-4])", s, flags=re.I)
    if not m:
        return pd.NaT
    year = int(m.group(1))
    q = int(m.group(2))
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    return pd.Timestamp(year=year, month=month_day[0], day=month_day[1])

def normalize_forecast_table(raw_df):
    if raw_df is None or raw_df.empty:
        return pd.DataFrame()
    df = raw_df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [" ".join([str(x) for x in c if str(x) != "nan"]).strip() for c in df.columns]
    else:
        df.columns = [str(c).strip() for c in df.columns]

    df = df.dropna(how="all").dropna(axis=1, how="all")
    if df.empty:
        return pd.DataFrame()

    col_text = " ".join(map(str, df.columns)).upper()
    first_row_text = " ".join(map(str, df.iloc[0].tolist())).upper() if len(df) else ""

    if not any(c in col_text for c in FORECAST_PRODUCT_COLUMNS) and any(c in first_row_text for c in FORECAST_PRODUCT_COLUMNS):
        df.columns = [str(x).strip() for x in df.iloc[0].tolist()]
        df = df.iloc[1:].reset_index(drop=True)
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            return pd.DataFrame()

    seen = {}
    unique_cols = []
    for c in df.columns:
        base = str(c).strip() or "COL"
        n = seen.get(base, 0)
        unique_cols.append(base if n == 0 else f"{base}_{n}")
        seen[base] = n + 1
    df.columns = unique_cols

    product_positions = {}
    for idx, c in enumerate(df.columns):
        u = str(c).strip().upper()
        for prod in FORECAST_PRODUCT_COLUMNS:
            if u == prod or prod in u.split():
                product_positions[prod] = idx

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
    from io import StringIO
    errors = []
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

    raise RuntimeError(f"{port_name} Bunker Price Forecast 표 파싱 실패: " + " | ".join(errors))

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
                time.sleep(4)
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
                return df, fetched_at, ""
        except Exception as e:
            print(f"[WARN] DB 로드 실패(bunker_forecast): {e}")

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
        empty = pd.DataFrame(columns=["port", "Period"] + FORECAST_PRODUCT_COLUMNS + ["graph_date", "source_url"])
        FORECAST_CACHE["df"] = empty.copy()
        FORECAST_CACHE["fetched_at"] = "-"
        FORECAST_CACHE["message"] = msg
        return empty, "-", msg

def collect_all_data():
    driver = make_driver()
    all_rows = []

    try:
        for url in URLS:
            print(f"[INFO] VLSFO 접속: {url}")
            driver.get(url)
            time.sleep(4)
            try_click_all_for_vlsfo(driver)
            time.sleep(2)

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
                return df, fetched_at
        except Exception as e:
            print(f"[WARN] DB 로드 실패(product_vlsfo): {e}")

    df = collect_all_data()
    fetched_at = get_dataset_meta(PRODUCT_TABLE)
    if fetched_at == "-":
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    DATA_CACHE["df"] = df.copy()
    DATA_CACHE["fetched_at"] = fetched_at
    return df, fetched_at

# =========================
# 차트 / HTML 빌더
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
    window_map = {"quarterly": 90, "semiannual": 180, "annual": 365}
    if ma_mode in window_map:
        w = window_map[ma_mode]
        out["ma"] = out.groupby("port")["price"].transform(lambda s: s.rolling(w, min_periods=1).mean())
    else:
        out["ma"] = pd.NA
    out["daily_change"] = out.groupby("port")["price"].diff()
    return out

def add_forecast_to_chart_df(df, forecast_df, selected_ports=None, start_date=None, end_date=None):
    if forecast_df is None or forecast_df.empty or "VLSFO" not in forecast_df.columns:
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

def port_color(port):
    return {"Rotterdam": TAC_BLUE, "Fujairah": TAC_GREEN, "Singapore": TAC_PURPLE}.get(port, TAC_ORANGE)

def apply_style(fig, height=460):
    fig.update_layout(
        template="plotly_white",
        height=height,
        margin=dict(l=20, r=20, t=20, b=20),
        font=dict(family="Pretendard, -apple-system, sans-serif", color=TAC_TEXT, size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1, bgcolor="rgba(255,255,255,0.85)"),
        xaxis=dict(showgrid=True, gridcolor="#f1f5f9"),
        yaxis=dict(showgrid=True, gridcolor="#f1f5f9")
    )
    return fig

def split_past_future_for_plot(sub, date_col="date", value_col="price"):
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
    past, future = split_past_future_for_plot(sub, date_col=x_col, value_col=y_col)
    if past is not None and not past.empty:
        fig.add_trace(go.Scatter(
            x=past[x_col], y=past[y_col], mode=mode, name=name,
            line=dict(color=color, width=width),
            marker=dict(size=marker_size) if "markers" in mode else None,
            hovertemplate=hovertemplate, showlegend=showlegend
        ))
    if future is not None and not future.empty:
        fig.add_trace(go.Scatter(
            x=future[x_col], y=future[y_col], mode=mode, name=f"{name} Forecast",
            line=dict(color=color, width=width, dash="dot"),
            marker=dict(size=marker_size, symbol="circle-open") if "markers" in mode else None,
            hovertemplate=hovertemplate, showlegend=showlegend
        ))

def build_line_chart(df, ma_mode="quarterly"):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'
    fig = go.Figure()
    for port in sorted(df["port"].unique()):
        sub = df[df["port"] == port].sort_values("date")
        color = port_color(port)
        add_solid_future_dashed_trace(fig, sub, x_col="date", y_col="price", name=f"{port} Price", color=color, width=2.6, mode="lines+markers", marker_size=4)
        if ma_mode != "off" and "ma" in sub.columns:
            ma_name = {"quarterly": "Quarterly MA", "semiannual": "Semi-Annual MA", "annual": "Annual MA"}.get(ma_mode, "MA")
            ma_sub = sub.dropna(subset=["ma"]).copy()
            add_solid_future_dashed_trace(fig, ma_sub, x_col="date", y_col="ma", name=f"{port} {ma_name}", color=color, width=1.8, mode="lines", marker_size=0)

    today_line = pd.Timestamp.today().normalize().strftime("%Y-%m-%d")
    fig.add_shape(type="line", x0=today_line, x1=today_line, y0=0, y1=1, xref="x", yref="paper", line=dict(color="#64748b", width=1.5, dash="dash"))
    fig.add_annotation(x=today_line, y=1, xref="x", yref="paper", text="Today", showarrow=False, xanchor="left", yanchor="bottom", font=dict(size=11, color="#64748b"))
    fig.update_layout(xaxis_title="Date", yaxis_title="USD/MT", xaxis=dict(rangeslider=dict(visible=True), type="date"))
    apply_style(fig, height=500)
    return fig.to_html(full_html=False, include_plotlyjs="cdn")

def build_latest_bar_chart(df):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'
    latest_date = df["date"].max()
    latest_df = df[df["date"] == latest_date].copy().sort_values("price", ascending=False)
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=latest_df["port"], y=latest_df["price"],
        text=[f"{v:.1f}" for v in latest_df["price"]],
        textposition="outside",
        marker_color=[port_color(p) for p in latest_df["port"]],
        showlegend=False
    ))
    fig.update_layout(xaxis_title="Port", yaxis_title="USD/MT")
    apply_style(fig, height=400)
    return fig.to_html(full_html=False, include_plotlyjs=False)

def build_spread_chart(df):
    if df.empty:
        return '<div class="empty">데이터 없음</div>'
    pivot_df = df.pivot_table(index="date", columns="port", values="price", aggfunc="mean").reset_index()
    fig = go.Figure()
    if "Rotterdam" in pivot_df.columns and "Singapore" in pivot_df.columns:
        fig.add_trace(go.Scatter(x=pivot_df["date"], y=pivot_df["Rotterdam"] - pivot_df["Singapore"], mode="lines", name="Rotterdam - Singapore", line=dict(color=TAC_BLUE, width=2.5)))
    if "Fujairah" in pivot_df.columns and "Singapore" in pivot_df.columns:
        fig.add_trace(go.Scatter(x=pivot_df["date"], y=pivot_df["Fujairah"] - pivot_df["Singapore"], mode="lines", name="Fujairah - Singapore", line=dict(color=TAC_RED, width=2.5)))
    fig.add_hline(y=0, line_dash="dash", line_color="#94a3b8")
    fig.update_layout(xaxis_title="Date", yaxis_title="Spread (USD/MT)")
    apply_style(fig, height=400)
    return fig.to_html(full_html=False, include_plotlyjs=False)

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

def compute_period_change_kpis(df):
    if df.empty:
        return []
    result = []
    period_defs = [("QoQ", "QE"), ("HoH", "2QE"), ("YoY", "YE")]
    for port in ["Fujairah", "Rotterdam", "Singapore"]:
        sub = df[df["port"] == port].copy().sort_values("date")
        item = {"port": port, "metrics": []}
        if sub.empty:
            for label, _ in period_defs:
                item["metrics"].append({"label": label, "value": "N/A", "class_name": "neutral"})
            result.append(item)
            continue
        for label, freq in period_defs:
            temp = sub.set_index("date").resample(freq)["price"].mean().dropna().reset_index()
            if len(temp) < 2:
                item["metrics"].append({"label": label, "value": "N/A", "class_name": "neutral"})
                continue
            current = temp.iloc[-1]["price"]
            previous = temp.iloc[-2]["price"]
            if previous in [0, None] or pd.isna(previous):
                item["metrics"].append({"label": label, "value": "N/A", "class_name": "neutral"})
                continue
            change_pct = (current - previous) / previous * 100
            cls = "up" if change_pct > 0 else "down" if change_pct < 0 else "neutral"
            item["metrics"].append({"label": label, "value": f"{change_pct:+.2f}%", "class_name": cls})
        result.append(item)
    return result

def build_forecast_table_html(forecast_df, fetched_at="-", message=""):
    if forecast_df is None or forecast_df.empty:
        return '<div class="empty">Forecast 데이터 없음</div>'
    blocks = []
    for port_name in ALL_PORTS:
        sub = forecast_df[forecast_df["port"] == port_name].copy()
        if sub.empty:
            continue
        rows = []
        for _, r in sub.iterrows():
            period = str(r.get("Period", ""))
            cells = [("-" if pd.isna(r.get(c)) else f"{float(r.get(c)):,.0f}") for c in FORECAST_PRODUCT_COLUMNS]
            rows.append(f"<tr><th>{period}</th><td>{cells[0]}</td><td>{cells[1]}</td><td>{cells[2]}</td><td>{cells[3]}</td></tr>")
        blocks.append(f"""
        <div style="border: 1px solid #e2e8f0; border-radius: 8px; overflow: hidden; background: white; margin-bottom: 12px;">
          <div style="background: #1e293b; color: white; padding: 8px 12px; font-weight: 700; font-size: 14px;">{port_name} Bunker Price Forecast</div>
          <table style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: center;">
            <thead><tr style="background: #f8fafc; color: #64748b;"><th>Period</th><th>VLSFO</th><th>MGO</th><th>LSMGO</th><th>HSFO</th></tr></thead>
            <tbody>{''.join(rows)}</tbody>
          </table>
        </div>
        """)
    return ''.join(blocks)

HTML = """
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ship & Bunker VLSFO Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-2: #f1f5f9;
  --border: #e2e8f0;
  --text: #0f172a;
  --text-muted: #64748b;
  --primary: #2563eb;
  --pos: #dc2626;
  --neg: #2563eb;
  --radius: 12px;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Pretendard', sans-serif;
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
  color: var(--text-muted);
  text-decoration: none;
  font-size: 13px;
  font-weight: 600;
  padding: 6px 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.header { margin-bottom: 24px; }
.header h1 { font-size: 26px; font-weight: 800; }
.meta { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  margin-bottom: 20px;
}
.card-title { font-size: 16px; font-weight: 700; margin-bottom: 14px; }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 12px;
}
.kpi-card {
  background: var(--surface-2);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.kpi-label { font-size: 11px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; margin-bottom: 4px; }
.kpi-value { font-size: 22px; font-weight: 800; font-family: 'JetBrains Mono', monospace; }
.port-kpi-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
.port-kpi-card { border: 1px solid var(--border); border-radius: 8px; padding: 14px; background: white; }
.port-kpi-metrics { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 8px; }
.port-kpi-metric { text-align: center; padding: 8px; border-radius: 6px; border: 1px solid var(--border); }
.port-kpi-metric.up { background: #fef2f2; border-color: #fecaca; color: #dc2626; font-weight: 700; }
.port-kpi-metric.down { background: #eff6ff; border-color: #bfdbfe; color: #2563eb; font-weight: 700; }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 900px) { .row2, .port-kpi-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>

  <div class="top-nav">
    <a href="../index.html" class="back-link">← Back to Dashboard</a>
    <span class="meta">Market Intelligence / <strong>02 Sea - Bunker Price</strong></span>
  </div>

  <div class="header">
    <h1>Ship & Bunker VLSFO Dashboard</h1>
    <div class="meta">Source: Ship &amp; Bunker &nbsp;|&nbsp; Fetched at: {{ fetched_at }}</div>
  </div>

  <div class="card">
    <div class="card-title">Market Snapshot</div>
    <div class="kpi-grid">
      <div class="kpi-card"><div class="kpi-label">Latest Date</div><div class="kpi-value">{{ kpis.latest_date }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Average Price</div><div class="kpi-value">{{ kpis.avg_price }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Rotterdam</div><div class="kpi-value">{{ kpis.port_values.get('Rotterdam', '-') }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Fujairah</div><div class="kpi-value">{{ kpis.port_values.get('Fujairah', '-') }}</div></div>
      <div class="kpi-card"><div class="kpi-label">Singapore</div><div class="kpi-value">{{ kpis.port_values.get('Singapore', '-') }}</div></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Period Change (QoQ / HoH / YoY)</div>
    <div class="port-kpi-grid">
      {% for port_item in period_change_kpis %}
      <div class="port-kpi-card">
        <div style="font-weight: 700; font-size: 14px;">{{ port_item.port }}</div>
        <div class="port-kpi-metrics">
          {% for metric in port_item.metrics %}
          <div class="port-kpi-metric {{ metric.class_name }}">
            <div style="font-size: 10px; color: var(--text-muted);">{{ metric.label }}</div>
            <div style="font-size: 14px;">{{ metric.value }}</div>
          </div>
          {% endfor %}
        </div>
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="card">
    <div class="card-title">VLSFO Price Trend (History &amp; Forecast)</div>
    {{ line_chart|safe }}
  </div>

  <div class="card">
    <div class="card-title">Bunker Price Forecast</div>
    {{ forecast_table_html|safe }}
  </div>

  <div class="row2">
    <div class="card">
      <div class="card-title">Latest Price Comparison</div>
      {{ bar_chart|safe }}
    </div>
    <div class="card">
      <div class="card-title">Port Spread Trend</div>
      {{ spread_chart|safe }}
    </div>
  </div>

</body>
</html>
"""

@app.route("/")
def index():
    force_refresh = request.args.get("refresh", "0") == "1"
    df, fetched_at = load_or_collect_data(force_refresh=force_refresh)
    forecast_df, forecast_fetched_at, forecast_message = load_or_collect_forecast_data(force_refresh=force_refresh)

    chart_df = add_forecast_to_chart_df(df, forecast_df)
    kpis = build_kpis(df)
    line_chart = build_line_chart(chart_df)
    forecast_table_html = build_forecast_table_html(forecast_df, fetched_at=forecast_fetched_at, message=forecast_message)
    bar_chart = build_latest_bar_chart(df)
    spread_chart = build_spread_chart(df)
    period_change_kpis = compute_period_change_kpis(df)

    return render_template_string(
        HTML,
        fetched_at=fetched_at,
        kpis=kpis,
        line_chart=line_chart,
        bar_chart=bar_chart,
        spread_chart=spread_chart,
        period_change_kpis=period_change_kpis,
        forecast_table_html=forecast_table_html
    )

def run_crawler_only():
    print("[System] Running Bunker Price Crawler in CLI mode...")
    load_or_collect_data(force_refresh=True)
    load_or_collect_forecast_data(force_refresh=True)

    print("[System] Generating static index.html for GitHub Pages...")
    with app.test_request_context("/"):
        html_content = index()
        output_file = BASE_DIR / "index.html"
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(html_content)
    print(f"[System] Static HTML generated successfully: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron", action="store_true", help="Run crawler and generate static HTML without starting web server")
    args = parser.parse_args()

    if args.cron:
        run_crawler_only()
    else:
        print("[INFO] Starting local dashboard server on http://127.0.0.1:8080 ...")
        app.run(host="127.0.0.1", port=8080, debug=False)
