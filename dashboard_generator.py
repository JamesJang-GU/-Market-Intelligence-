# -*- coding: utf-8 -*-
import os
import sqlite3
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 1. Alphaliner 최신 데이터 가져오기
def get_latest_alphaliner():
    db_path = os.path.join(BASE_DIR, 'Alphaliner', 'alphaliner_history.db')
    if not os.path.exists(db_path):
        db_path = os.path.join(BASE_DIR, 'alphaliner_history.db')
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT captured_at, active_ships, total_teu FROM snapshot_master WHERE total_teu IS NOT NULL AND total_teu != '' ORDER BY snapshot_id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return {"date": row[0][:10], "ships": row[1], "teu": row[2]}
    except Exception:
        pass
    return {"date": "-", "ships": "-", "teu": "-"}

# 2. TAC Index 최신 데이터 가져오기
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

# 3. 통합 index.html 빌드
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
  margin: 30px auto;
  padding: 0 20px;
  line-height: 1.5;
}}
.header {{
  border-bottom: 3px solid var(--primary);
  padding-bottom: 16px;
  margin-bottom: 30px;
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  flex-wrap: wrap;
  gap: 12px;
}}
h1 {{ margin: 0; color: var(--primary); font-size: 28px; font-weight: 800; }}
.updated {{ font-size: 13px; color: var(--muted); font-weight: 600; }}
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
.badge-econ {{ background: #fef3c7; color: #b45309; }}

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

  <div class="header">
    <div>
      <h1>🌐 Market Intelligence Portal</h1>
      <div class="updated">해운 · 항공 · 거시경제 통합 대시보드</div>
    </div>
    <div class="updated">Last Updated: {updated_at}</div>
  </div>

  <div class="grid">
    
    <!-- 🚢 1. Alphaliner (컨테이너 선대) -->
    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">Alphaliner Top 100</h2>
          <span class="badge badge-ocean">Ocean Container</span>
        </div>
        <div class="metric-body">
          <div class="metric-label">Global Fleet Capacity</div>
          <div class="metric-val">{alpha['teu']} <span style="font-size: 16px; font-weight: 600;">TEU</span></div>
          <div class="metric-sub">Active Ships: <b>{alpha['ships']}</b> vessels ({alpha['date']})</div>
        </div>
      </div>
      <a href="./alphaliner.html" class="btn-link">Alphaliner 대시보드 보기 →</a>
    </div>

    <!-- ✈️ 2. TAC Index (항공 운임) -->
    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">TAC Air Freight Index</h2>
          <span class="badge badge-air">Air Cargo</span>
        </div>
        <div class="metric-body">
          <div class="metric-label">Overall Freight Rate Index</div>
          <div class="metric-val">{tac['index']}</div>
          <div class="metric-sub">Weekly Change: <b>{tac['change']}%</b> ({tac['date']})</div>
        </div>
      </div>
      <a href="./TAC_Index.html" class="btn-link">TAC Index 대시보드 보기 →</a>
    </div>

    <!-- ⚓ 3. Shipping Index (SCFI / BDI) - 추가 시 자동 연동 가능 -->
    <div class="card">
      <div>
        <div class="card-header">
          <h2 class="card-title">Shipping Freight Index</h2>
          <span class="badge badge-ocean">SCFI / BDI</span>
        </div>
        <div class="metric-body">
          <div class="metric-label">Container & Dry Bulk Rate</div>
          <div class="metric-val">SCFI & BDI</div>
          <div class="metric-sub">상하이 컨테이너 & 발틱 건화물 운임지수</div>
        </div>
      </div>
      <a href="./shipping_index.html" class="btn-link">운임지수 대시보드 보기 →</a>
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
