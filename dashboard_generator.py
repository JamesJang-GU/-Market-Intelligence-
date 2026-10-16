# -*- coding: utf-8 -*-
"""
Market Intelligence 메인 포털(index.html) 생성기

실행:
    python -u dashboard_generator.py

기능:
    1. Alphaliner 및 TAC Index SQLite DB에서 최신 지표 조회
    2. 저장소 최상위 경로에 index.html 생성
"""

import os
import sys
import sqlite3
import datetime
from html import escape

# dashboard_generator.py가 있는 저장소 최상위 경로
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================================================
# 상세 대시보드 링크 설정
# ---------------------------------------------------------
# 실제 HTML 파일 위치에 맞춰 수정하세요.
#
# 예시:
# - Alphaliner/index.html 이면 ./Alphaliner/index.html
# - TAC Index/index.html 이면 ./TAC%20Index/index.html
# =========================================================
DASHBOARD_LINKS = {
    "home": "./index.html",
    "alphaliner": "./Alphaliner/index.html",
    "tac": "./TAC%20Index/index.html",
}

# =========================================================
# 공통 보조 함수
# =========================================================
def format_number(value, decimal_places=0):
    """숫자를 보기 쉬운 천 단위 구분 형식으로 변환합니다."""
    if value is None or value == "":
        return "-"

    try:
        number = float(value)

        if decimal_places == 0:
            return f"{number:,.0f}"

        return f"{number:,.{decimal_places}f}"

    except (ValueError, TypeError):
        return str(value)

def format_percent(value):
    """증감률을 안전하게 % 형식으로 변환합니다."""
    if value is None or value == "":
        return "-"

    try:
        number = float(value)
        return f"{number:+.2f}%"
    except (ValueError, TypeError):
        text = str(value)
        return text if text.endswith("%") else f"{text}%"

def format_date(value):
    """날짜/일시 문자열을 YYYY-MM-DD 형식으로 정리합니다."""
    if not value:
        return "-"

    return str(value)[:10]

def get_database_path(folder_name, database_name):
    """
    우선순위:
    1. 하위 폴더 내 DB
    2. 저장소 최상위 DB
    """
    folder_db_path = os.path.join(BASE_DIR, folder_name, database_name)
    root_db_path = os.path.join(BASE_DIR, database_name)

    if os.path.exists(folder_db_path):
        return folder_db_path

    if os.path.exists(root_db_path):
        return root_db_path

    return None

# =========================================================
# 1. Alphaliner 최신 데이터 조회
# =========================================================
def get_latest_alphaliner():
    db_path = get_database_path("Alphaliner", "alphaliner_history.db")

    default_data = {
        "date": "-",
        "ships": "-",
        "teu": "-",
    }

    if not db_path:
        print("[WARNING] Alphaliner DB file not found.", flush=True)
        return default_data

    query = """
        SELECT
            captured_at,
            active_ships,
            total_teu
        FROM snapshot_master
        WHERE total_teu IS NOT NULL
          AND total_teu != ''
          AND total_teu != 'None'
        ORDER BY snapshot_id DESC
        LIMIT 1
    """

    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(query)
            row = cur.fetchone()

        if row:
            return {
                "date": format_date(row[0]),
                "ships": format_number(row[1]),
                "teu": format_number(row[2]),
            }

        print("[WARNING] No valid Alphaliner data found in snapshot_master.", flush=True)
        return default_data

    except sqlite3.Error as error:
        print(f"[ERROR] Failed to read Alphaliner DB: {error}", flush=True)
        return default_data

    except Exception as error:
        print(f"[ERROR] Unexpected Alphaliner read error: {error}", flush=True)
        return default_data

# =========================================================
# 2. TAC Index 최신 데이터 조회
# =========================================================
def get_latest_tac():
    db_path = get_database_path("TAC Index", "TAC_Index.db")

    default_data = {
        "date": "-",
        "index": "-",
        "change": "-",
    }

    if not db_path:
        print("[WARNING] TAC Index DB file not found.", flush=True)
        return default_data

    query = """
        SELECT
            latest_date,
            overall_index,
            weekly_change
        FROM tac_master
        ORDER BY id DESC
        LIMIT 1
    """

    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(query)
            row = cur.fetchone()

        if row:
            return {
                "date": format_date(row[0]),
                "index": format_number(row[1], decimal_places=2),
                "change": format_percent(row[2]),
            }

        print("[WARNING] No TAC Index data found in tac_master.", flush=True)
        return default_data

    except sqlite3.Error as error:
        print(f"[ERROR] Failed to read TAC Index DB: {error}", flush=True)
        return default_data

    except Exception as error:
        print(f"[ERROR] Unexpected TAC Index read error: {error}", flush=True)
        return default_data

# =========================================================
# 3. 통합 index.html 메인 포털 생성
# =========================================================
def build_portal_index():
    alpha = get_latest_alphaliner()
    tac = get_latest_tac()

    updated_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # HTML 출력 시 데이터에 특수문자가 포함되어도 화면 구조가 깨지지 않도록 처리
    alpha_date = escape(str(alpha["date"]))
    alpha_ships = escape(str(alpha["ships"]))
    alpha_teu = escape(str(alpha["teu"]))

    tac_date = escape(str(tac["date"]))
    tac_index = escape(str(tac["index"]))
    tac_change = escape(str(tac["change"]))

    html = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Market Intelligence Portal</title>

<style>
:root {{
  --bg: #f8fafc;
  --surface: #ffffff;
  --border: #e2e8f0;
  --primary: #1e3a8a;
  --text: #0f172a;
  --muted: #64748b;
  --accent: #2563eb;
}}

* {{
  box-sizing: border-box;
}}

body {{
  font-family: "Segoe UI", Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  max-width: 1280px;
  margin: 0 auto;
  padding: 0 20px 40px;
  line-height: 1.5;
}}

.navbar {{
  background: var(--primary);
  margin: 0 -20px 30px;
  padding: 14px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
}}

.nav-brand {{
  color: white;
  font-size: 20px;
  font-weight: 800;
  text-decoration: none;
}}

.nav-menu {{
  display: flex;
  gap: 10px;
  list-style: none;
  margin: 0;
  padding: 0;
  flex-wrap: wrap;
}}

.nav-item a {{
  color: #e0f2fe;
  text-decoration: none;
  padding: 8px 14px;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 600;
}}

.nav-item a:hover,
.nav-item a.active {{
  background: rgba(255, 255, 255, 0.2);
  color: white;
}}

.header {{
  border-bottom: 2px solid var(--border);
  padding-bottom: 16px;
  margin-bottom: 30px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 12px;
}}

h1 {{
  margin: 0;
  color: var(--primary);
  font-size: 26px;
  font-weight: 800;
}}

.updated {{
  font-size: 13px;
  color: var(--muted);
  font-weight: 600;
}}

.grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 24px;
}}

.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 24px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.03);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}}

.card:hover {{
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.08);
}}

.card-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
  gap: 12px;
}}

.card-title {{
  font-size: 20px;
  font-weight: 700;
  color: var(--primary);
  margin: 0;
}}

.badge {{
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 20px;
  font-weight: 700;
  text-transform: uppercase;
  white-space: nowrap;
}}

.badge-ocean {{
  background: #e0f2fe;
  color: #0369a1;
}}

.badge-air {{
  background: #f0fdf4;
  color: #15803d;
}}

.metric-body {{
  margin-bottom: 20px;
}}

.metric-label {{
  font-size: 13px;
  color: var(--muted);
  font-weight: 600;
}}

.metric-val {{
  font-size: 32px;
  font-weight: 800;
  color: var(--text);
  margin: 6px 0;
}}

.metric-sub {{
  font-size: 13px;
  color: var(--muted);
}}

.btn-link {{
  display: block;
  width: 100%;
  text-align: center;
  padding: 12px 0;
  background: var(--accent);
  color: white;
  border-radius: 8px;
  text-decoration: none;
  font-weight: 700;
  font-size: 14px;
}}

.btn-link:hover {{
  background: #1d4ed8;
}}
</style>
</head>

<body>
  <nav class="navbar">
    <a href="{DASHBOARD_LINKS["home"]}" class="nav-brand">🌐 Market Intelligence</a>

    <ul class="nav-menu">
      <li class="nav-item">
        <a href="{DASHBOARD_LINKS["home"]}" class="active">홈 (Portal)</a>
      </li>
      <li class="nav-item">
        <a href="{DASHBOARD_LINKS["alphaliner"]}">🚢 Alphaliner</a>
      </li>
      <li class="nav-item">
        <a href="{DASHBOARD_LINKS["tac"]}">✈️ TAC Index</a>
      </li>
    </ul>
  </nav>

  <div class="header">
    <div>
      <h1>Market Intelligence Hub</h1>
      <div class="updated">해운, 항공, 물류 지표 통합 모니터링 포털</div>
    </div>
    <div class="updated">Last Updated: {updated_at}</div>
  </div>

  <div class="grid">

    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">Alphaliner Top 100</h2>
          <span class="badge badge-ocean">Container</span>
        </div>

        <div class="metric-body">
          <div class="metric-label">Global Fleet Capacity</div>
          <div class="metric-val">
            {alpha_teu}
            <span style="font-size: 16px; font-weight: 600;">TEU</span>
          </div>
          <div class="metric-sub">
            Active Vessels: <b>{alpha_ships}</b> ({alpha_date})
          </div>
        </div>
      </div>

      <a href="{DASHBOARD_LINKS["alphaliner"]}" class="btn-link">
        Alphaliner 상세 대시보드 →
      </a>
    </div>

    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">TAC Air Freight Index</h2>
          <span class="badge badge-air">Air Cargo</span>
        </div>

        <div class="metric-body">
          <div class="metric-label">Overall Rate Index</div>
          <div class="metric-val">{tac_index}</div>
          <div class="metric-sub">
            Weekly Change: <b>{tac_change}</b> ({tac_date})
          </div>
        </div>
      </div>

      <a href="{DASHBOARD_LINKS["tac"]}" class="btn-link">
        TAC Index 상세 대시보드 →
      </a>
    </div>

  </div>
</body>
</html>
"""

    output_path = os.path.join(BASE_DIR, "index.html")

    with open(output_path, "w", encoding="utf-8") as file:
        file.write(html)

    print(f"[SUCCESS] Portal HTML built: {output_path}", flush=True)

if __name__ == "__main__":
    try:
        build_portal_index()
    except Exception as error:
        print(f"[FATAL] Failed to generate portal HTML: {error}", flush=True)
        sys.exit(1)
