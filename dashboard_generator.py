# -*- coding: utf-8 -*-
import os
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Alphaliner 최신 데이터 조회
def get_latest_alphaliner():
    db_path = os.path.join(BASE_DIR, 'Alphaliner', 'alphaliner_history.db')
    if not os.path.exists(db_path):
        db_path = os.path.join(BASE_DIR, 'alphaliner_history.db')
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT captured_at, active_ships, total_teu FROM snapshot_master WHERE total_teu IS NOT NULL AND total_teu != '' AND total_teu != 'None' ORDER BY snapshot_id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {"date": row[0][:10], "ships": row[1], "teu": row[2]}
    except Exception:
        pass
    return {"date": "-", "ships": "-", "teu": "-"}

# 2. TAC Index 최신 데이터 조회
def get_latest_tac():
    db_path = os.path.join(BASE_DIR, 'TAC Index', 'TAC_Index.db')
    if not os.path.exists(db_path):
        db_path = os.path.join(BASE_DIR, 'TAC_Index.db')
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT latest_date, overall_index, weekly_change FROM tac_master ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {"date": row[0], "index": row[1], "change": row[2]}
    except Exception:
        pass
    return {"date": "-", "index": "-", "change": "-"}

# 3. 통합 index.html 메인 포털 생성
def build_portal_index():
    alpha = get_latest_alphaliner()
    tac = get_latest_tac()
    updated_at = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
* {{ box-sizing: border-box; }}
body {{
  font-family: 'Segoe UI', Arial, sans-serif;
  background: var(--bg);
  color: var(--text);
  max-width: 1280px;
  margin: 0 auto;
  padding: 0 20px 40px 20px;
  line-height: 1.5;
}}

/* 상단 상시 고정 네비게이션 메뉴바 (GNB) */
.navbar {{
  background: var(--primary);
  margin: 0 -20px 30px -20px;
  padding: 14px 24px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  box-shadow: 0 2px 10px rgba(0,0,0,0.1);
  flex-wrap: wrap;
  gap: 12px;
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
  transition: all 0.2s;
}}
.nav-item a:hover, .nav-item a.active {{
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
h1 {{ margin: 0; color: var(--primary); font-size: 26px; font-weight: 800; }}
.updated {{ font-size: 13px; color: var(--muted); font-weight: 600; }}

/* 대시보드 요약 카드 그리드 */
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
  box-shadow: 0 4px 12px rgba(0,0,0,0.03);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  transition: transform 0.2s ease, box-shadow 0.2s ease;
}}
.card:hover {{
  transform: translateY(-4px);
  box-shadow: 0 8px 24px rgba(0,0,0,0.08);
}}
.card-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}}
.card-title {{ font-size: 20px; font-weight: 700; color: var(--primary); margin: 0; }}
.badge {{
  font-size: 11px;
  padding: 4px 10px;
  border-radius: 20px;
  font-weight: 700;
  text-transform: uppercase;
}}
.badge-ocean {{ background: #e0f2fe; color: #0369a1; }}
.badge-air {{ background: #f0fdf4; color: #15803d; }}

.metric-body {{ margin-bottom: 20px; }}
.metric-label {{ font-size: 13px; color: var(--muted); font-weight: 600; }}
.metric-val {{ font-size: 32px; font-weight: 800; color: var(--text); margin: 6px 0; }}
.metric-sub {{ font-size: 13px; color: var(--muted); }}

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
  transition: background 0.2s ease;
}}
.btn-link:hover {{ background: #1d4ed8; }}
</style>
</head>
<body>

  <!-- 상단 메뉴바 (메뉴에서 클릭해서 바로 이동) -->
  <nav class="navbar">
    <a href="./index.html" class="nav-brand">🌐 Market Intelligence</a>
    <ul class="nav-menu">
      <li class="nav-item"><a href="./index.html" class="active">홈 (Portal)</a></li>
      <li class="nav-item"><a href="./alphaliner.html">🚢 Alphaliner</a></li>
      <li class="nav-item"><a href="./TAC_Index.html">✈️ TAC Index</a></li>
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
    
    <!-- 🚢 1. Alphaliner (컨테이너 선대) 카드 -->
    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">Alphaliner Top 100</h2>
          <span class="badge badge-ocean">Container</span>
        </div>
        <div class="metric-body">
          <div class="metric-label">Global Fleet Capacity</div>
          <div class="metric-val">{alpha['teu']} <span style="font-size: 16px; font-weight: 600;">TEU</span></div>
          <div class="metric-sub">Active Vessels: <b>{alpha['ships']}</b> ({alpha['date']})</div>
        </div>
      </div>
      <a href="./alphaliner.html" class="btn-link">Alphaliner 상세 대시보드 →</a>
    </div>

    <!-- ✈️ 2. TAC Index (항공 운임) 카드 -->
    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">TAC Air Freight Index</h2>
          <span class="badge badge-air">Air Cargo</span>
        </div>
        <div class="metric-body">
          <div class="metric-label">Overall Rate Index</div>
          <div class="metric-val">{tac['index']}</div>
          <div class="metric-sub">Weekly Change: <b>{tac['change']}%</b> ({tac['date']})</div>
        </div>
      </div>
      <a href="./TAC_Index.html" class="btn-link">TAC Index 상세 대시보드 →</a>
    </div>

  </div>

</body>
</html>
"""

    with open(os.path.join(BASE_DIR, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html)
    print("[System] Portal index.html built successfully!")

if __name__ == '__main__':
    build_portal_index()
