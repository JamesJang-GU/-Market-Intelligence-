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

warnings.filterwarnings("ignore")

from flask import Flask, render_template_string, Response, request
import pandas as pd
import plotly.graph_objects as go
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)

ALPHALINER_URL = "https://alphaliner.axsmarine.com/PublicTop100/"
MEM_CACHE = {}
CACHE_MINUTES = 30

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "alphaliner_history.db")

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
    MEM_CACHE[key] = (
        datetime.datetime.now() + datetime.timedelta(minutes=minutes),
        value
    )

def fmt_million(x):
    if pd.isna(x):
        return ""

    return f"{x / 1_000_000:.1f}M"

def fmt_num(x):
    if pd.isna(x):
        return ""

    return f"{int(round(float(x))):,}"

def fmt_pct(x):
    if pd.isna(x):
        return ""

    return f"{float(x):.1f}%"

def fmt_delta(x, pct=False):
    if pd.isna(x):
        return "-"

    x = float(x)
    sign = "+" if x > 0 else ""

    if pct:
        return f"{sign}{x:.1f}%p"

    return f"{sign}{int(round(x)):,}"

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

    cls = "delta-pos" if n > 0 else ("delta-neg" if n < 0 else "delta-zero")
    arrow = "▲ " if n > 0 else ("▼ " if n < 0 else "")

    return f'<span class="{cls}">{arrow}{fmt_delta(n, pct=pct)}</span>'

def safe_num(x):
    try:
        if x is None or x == "":
            return 0.0

        return float(str(x).replace(",", ""))

    except Exception:
        return 0.0

def get_db_conn():
    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_master (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT,
            snapshot_hash TEXT UNIQUE,
            active_ships TEXT,
            total_teu TEXT,
            trans_atlantic TEXT,
            trans_pacific TEXT,
            feast_europe TEXT,
            row_count INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS snapshot_detail (
            snapshot_id INTEGER,
            rank INTEGER,
            operator TEXT,
            total_teu REAL,
            market_share REAL,
            total_ships REAL,
            owned_teu REAL,
            owned_ships REAL,
            chartered_teu REAL,
            chartered_ships REAL,
            chartered_percent REAL,
            orderbook_teu REAL,
            orderbook_ships REAL,
            orderbook_percent REAL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            accessed_at TEXT,
            snapshot_id INTEGER,
            refresh_flag INTEGER,
            client_ip TEXT,
            user_agent TEXT
        )
    """)

    conn.commit()

    return conn

def make_snapshot_hash(bundle):
    df = bundle["df"].copy()

    key_cols = [
        c for c in [
            "Rank",
            "Operator",
            "Total_TEU",
            "Market_Share",
            "Total_Ships",
            "Owned_TEU",
            "Owned_Ships",
            "Chartered_TEU",
            "Chartered_Ships",
            "Chartered_Percent",
            "Orderbook_TEU",
            "Orderbook_Ships",
            "Orderbook_Percent",
        ]
        if c in df.columns
    ]

    payload = df[key_cols].fillna("").astype(str).to_csv(index=False)
    payload += str(bundle.get("summary", {}))
    payload += str(bundle.get("regional", {}))

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

def save_snapshot(bundle):
    try:
        conn = get_db_conn()

        summary = bundle.get("summary", {})
        regional = bundle.get("regional", {})
        df = bundle["df"].copy()

        if df.empty or not summary.get("totalTEU") or summary.get("totalTEU") == "None":
            print("[Warning] Parsed data is incomplete. Skipping DB save.")
            conn.close()
            return None

        snapshot_hash = make_snapshot_hash(bundle)
        cur = conn.cursor()

        cur.execute("""
            SELECT snapshot_id
            FROM snapshot_master
            WHERE snapshot_hash = ?
              AND total_teu IS NOT NULL
              AND total_teu != "None"
        """, (snapshot_hash,))

        row = cur.fetchone()

        if row:
            conn.close()
            return row[0]

        captured_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cur.execute("""
            INSERT INTO snapshot_master (
                captured_at,
                snapshot_hash,
                active_ships,
                total_teu,
                trans_atlantic,
                trans_pacific,
                feast_europe,
                row_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            captured_at,
            snapshot_hash,
            str(summary.get("totalActiveShips", "")),
            str(summary.get("totalTEU", "")),
            str(regional.get("transAtlantic", "")),
            str(regional.get("transPacific", "")),
            str(regional.get("feastEurope", "")),
            len(df)
        ))

        snapshot_id = cur.lastrowid
        rows = []

        for _, r in df.iterrows():
            rows.append((
                snapshot_id,
                int(r.get("Rank")) if pd.notna(r.get("Rank")) else None,
                str(r.get("Operator")) if pd.notna(r.get("Operator")) else "",
                float(r.get("Total_TEU")) if pd.notna(r.get("Total_TEU")) else None,
                float(r.get("Market_Share")) if pd.notna(r.get("Market_Share")) else None,
                float(r.get("Total_Ships")) if pd.notna(r.get("Total_Ships")) else None,
                float(r.get("Owned_TEU")) if pd.notna(r.get("Owned_TEU")) else None,
                float(r.get("Owned_Ships")) if pd.notna(r.get("Owned_Ships")) else None,
                float(r.get("Chartered_TEU")) if pd.notna(r.get("Chartered_TEU")) else None,
                float(r.get("Chartered_Ships")) if pd.notna(r.get("Chartered_Ships")) else None,
                float(r.get("Chartered_Percent")) if pd.notna(r.get("Chartered_Percent")) else None,
                float(r.get("Orderbook_TEU")) if pd.notna(r.get("Orderbook_TEU")) else None,
                float(r.get("Orderbook_Ships")) if pd.notna(r.get("Orderbook_Ships")) else None,
                float(r.get("Orderbook_Percent")) if pd.notna(r.get("Orderbook_Percent")) else None,
            ))

        cur.executemany("""
            INSERT INTO snapshot_detail (
                snapshot_id,
                rank,
                operator,
                total_teu,
                market_share,
                total_ships,
                owned_teu,
                owned_ships,
                chartered_teu,
                chartered_ships,
                chartered_percent,
                orderbook_teu,
                orderbook_ships,
                orderbook_percent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

        conn.commit()
        conn.close()

        return snapshot_id

    except Exception as error:
        print(f"[Error] Failed to save snapshot: {error}")
        return None

def log_access(snapshot_id, refresh_flag, client_ip="", user_agent=""):
    try:
        conn = get_db_conn()

        conn.execute("""
            INSERT INTO access_log (
                accessed_at,
                snapshot_id,
                refresh_flag,
                client_ip,
                user_agent
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            snapshot_id,
            1 if refresh_flag else 0,
            client_ip,
            (user_agent or "")[:500],
        ))

        conn.commit()
        conn.close()

    except Exception:
        pass

def load_snapshot_detail(snapshot_id):
    if not snapshot_id:
        return pd.DataFrame()

    conn = get_db_conn()

    query = """
        SELECT
            rank AS Rank,
            operator AS Operator,
            total_teu AS Total_TEU,
            market_share AS Market_Share,
            total_ships AS Total_Ships,
            owned_teu AS Owned_TEU,
            owned_ships AS Owned_Ships,
            chartered_teu AS Chartered_TEU,
            chartered_ships AS Chartered_Ships,
            chartered_percent AS Chartered_Percent,
            orderbook_teu AS Orderbook_TEU,
            orderbook_ships AS Orderbook_Ships,
            orderbook_percent AS Orderbook_Percent
        FROM snapshot_detail
        WHERE snapshot_id = ?
        ORDER BY rank
    """

    df = pd.read_sql_query(query, conn, params=(snapshot_id,))
    conn.close()

    return df

def get_snapshot_meta(snapshot_id):
    if not snapshot_id:
        return None

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT captured_at, active_ships, total_teu
        FROM snapshot_master
        WHERE snapshot_id = ?
    """, (snapshot_id,))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "captured_at": row[0],
        "active_ships": row[1],
        "total_teu": row[2],
    }

def get_previous_snapshot_id(current_snapshot_id):
    if not current_snapshot_id:
        return None

    meta = get_snapshot_meta(current_snapshot_id)

    if not meta:
        return None

    conn = get_db_conn()
    cur = conn.cursor()

    cur.execute("""
        SELECT m.snapshot_id
        FROM snapshot_master m
        WHERE m.captured_at < ?
          AND m.total_teu IS NOT NULL
          AND m.total_teu != ''
          AND m.total_teu != 'None'
          AND (
              SELECT COUNT(*)
              FROM snapshot_detail d
              WHERE d.snapshot_id = m.snapshot_id
          ) >= 50
        ORDER BY m.captured_at DESC, m.snapshot_id DESC
        LIMIT 1
    """, (meta["captured_at"],))

    row = cur.fetchone()
    conn.close()

    return row[0] if row else None

def build_snapshot_delta(current_df, previous_df):
    if current_df is None or current_df.empty:
        return pd.DataFrame()

    if previous_df is None or previous_df.empty:
        return pd.DataFrame()

    merged = current_df.merge(
        previous_df,
        on="Operator",
        how="left",
        suffixes=("_current", "_previous")
    )

    cols = [
        "Rank",
        "Total_TEU",
        "Market_Share",
        "Total_Ships",
        "Owned_TEU",
        "Owned_Ships",
        "Chartered_TEU",
        "Chartered_Ships",
        "Chartered_Percent",
        "Orderbook_TEU",
        "Orderbook_Ships",
        "Orderbook_Percent",
    ]

    for col in cols:
        current_col = f"{col}_current"
        previous_col = f"{col}_previous"

        if current_col in merged.columns:
            merged[current_col] = pd.to_numeric(
                merged[current_col],
                errors="coerce"
            )

        if previous_col in merged.columns:
            merged[previous_col] = pd.to_numeric(
                merged[previous_col],
                errors="coerce"
            )

    merged["Delta_TEU"] = (
        merged["Total_TEU_current"].fillna(0)
        - merged["Total_TEU_previous"].fillna(0)
    )

    merged["Delta_Market_Share"] = (
        merged["Market_Share_current"].fillna(0)
        - merged["Market_Share_previous"].fillna(0)
    )

    merged["Delta_Ships"] = (
        merged["Total_Ships_current"].fillna(0)
        - merged["Total_Ships_previous"].fillna(0)
    )

    merged["Rank_Change"] = (
        merged["Rank_previous"].fillna(merged["Rank_current"])
        - merged["Rank_current"]
    )

    out = merged[[
        "Rank_current",
        "Operator",
        "Total_TEU_current",
        "Total_TEU_previous",
        "Delta_TEU",
        "Market_Share_current",
        "Market_Share_previous",
        "Delta_Market_Share",
        "Total_Ships_current",
        "Total_Ships_previous",
        "Delta_Ships",
        "Rank_Change",
    ]].copy()

    out.columns = [
        "Rank",
        "Operator",
        "Current_TEU",
        "Previous_TEU",
        "Delta_TEU",
        "Current_Market_Share",
        "Previous_Market_Share",
        "Delta_Market_Share",
        "Current_Ships",
        "Previous_Ships",
        "Delta_Ships",
        "Rank_Change",
    ]

    return out.sort_values("Rank").reset_index(drop=True)

def get_snapshot_id_by_mode(
    current_snapshot_id,
    compare_mode="previous",
    custom_date=None
):
    if compare_mode == "previous":
        return get_previous_snapshot_id(current_snapshot_id)

    conn = get_db_conn()
    cur = conn.cursor()

    current_meta = get_snapshot_meta(current_snapshot_id)

    current_captured = (
        current_meta["captured_at"]
        if current_meta
        else datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    valid_condition = """
        AND total_teu IS NOT NULL
        AND total_teu != ''
        AND total_teu != 'None'
        AND (
            SELECT COUNT(*)
            FROM snapshot_detail d
            WHERE d.snapshot_id = snapshot_master.snapshot_id
        ) >= 50
    """

    if compare_mode == "1m":
        target = (
            datetime.datetime.strptime(
                current_captured,
                "%Y-%m-%d %H:%M:%S"
            )
            - datetime.timedelta(days=30)
        ).strftime("%Y-%m-%d %H:%M:%S")

        cur.execute(f"""
            SELECT snapshot_id
            FROM snapshot_master
            WHERE captured_at <= ?
            {valid_condition}
            ORDER BY captured_at DESC
            LIMIT 1
        """, (target,))

    elif compare_mode == "3m":
        target = (
            datetime.datetime.strptime(
                current_captured,
                "%Y-%m-%d %H:%M:%S"
            )
            - datetime.timedelta(days=90)
        ).strftime("%Y-%m-%d %H:%M:%S")

        cur.execute(f"""
            SELECT snapshot_id
            FROM snapshot_master
            WHERE captured_at <= ?
            {valid_condition}
            ORDER BY captured_at DESC
            LIMIT 1
        """, (target,))

    elif compare_mode == "custom" and custom_date:
        try:
            target = datetime.datetime.strptime(
                custom_date,
                "%Y-%m-%d"
            ).strftime("%Y-%m-%d 23:59:59")

            cur.execute(f"""
                SELECT snapshot_id
                FROM snapshot_master
                WHERE captured_at <= ?
                {valid_condition}
                ORDER BY captured_at DESC
                LIMIT 1
            """, (target,))

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
    cache_key = "alphaliner_simple_bundle"

    if not force:
        cached = get_cache(cache_key)

        if cached is not None:
            return cached

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

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

                var regBlock = txt.match(
                    /Trans-Atlantic[\s\S]*?Trans-Pacific[\s\S]*?Feast-Europe
