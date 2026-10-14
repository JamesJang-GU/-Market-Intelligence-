import sqlite3
import pandas as pd

DB_FILE = "alphaliner_history.db"

conn = sqlite3.connect(DB_FILE)

snapshot = pd.read_sql("""
SELECT *
FROM snapshot_master
ORDER BY snapshot_id DESC
LIMIT 1
""", conn)

history = pd.read_sql("""
SELECT snapshot_id, captured_at, total_teu
FROM snapshot_master
ORDER BY snapshot_id
""", conn)

conn.close()

if len(snapshot) == 0:
    raise Exception("No snapshot data found.")

latest = snapshot.iloc[0]

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Alphaliner Dashboard</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>body{{font-family:Arial;background:#f4f6f8;padding:40px}} .card{{background:white;padding:20px;border-radius:10px;margin-bottom:20px;box-shadow:0 0 10px rgba(0,0,0,.1)}}</style>
</head><body>
<h1>Alphaliner Market Dashboard</h1>
<div class="card">
<h2>Latest Snapshot</h2>
<p><b>Snapshot ID:</b> {latest['snapshot_id']}</p>
<p><b>Date:</b> {latest['captured_at']}</p>
<p><b>Total TEU:</b> {latest['total_teu']}</p>
<p><b>Active Ships:</b> {latest['active_ships']}</p>
</div>
<div class="card"><h2>Total TEU Trend</h2><div id="teuChart"></div></div>
<script>
var trace={x:{history['captured_at'].astype(str).tolist()},y:{history['total_teu'].fillna(0).tolist()},type:'scatter',mode:'lines+markers'};
Plotly.newPlot('teuChart',[trace]);
</script></body></html>
"""

with open("dashboard.html","w",encoding="utf-8") as f:
    f.write(html)

print("dashboard.html generated")
