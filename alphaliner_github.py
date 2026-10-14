# GitHub Actions version (replace existing alphaliner.py)
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
import pandas as pd, sqlite3, hashlib, datetime, os, time

ALPHALINER_URL='https://alphaliner.axsmarine.com/PublicTop100/'
DB_FILE='alphaliner_history.db'

# Keep original create/save logic from your source file
# Only GitHub-compatible execution block changed.

def get_db_conn():
    conn=sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot_master (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,captured_at TEXT,snapshot_hash TEXT UNIQUE,active_ships TEXT,total_teu TEXT,trans_atlantic TEXT,trans_pacific TEXT,feast_europe TEXT,row_count INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot_detail (snapshot_id INTEGER, rank INTEGER, operator TEXT, total_teu REAL, market_share REAL, total_ships REAL, owned_teu REAL, owned_ships REAL, chartered_teu REAL, chartered_ships REAL, chartered_percent REAL, orderbook_teu REAL, orderbook_ships REAL, orderbook_percent REAL)")
    conn.commit(); return conn

# Replace the fetch_bundle in your current GitHub file with this one

def fetch_bundle(force=True):
    chrome_options=Options()
    chrome_options.add_argument('--headless=new')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')

    driver=webdriver.Chrome(service=Service(ChromeDriverManager().install()),options=chrome_options)
    try:
        driver.get(ALPHALINER_URL)
        time.sleep(8)
        payload=driver.execute_script(r"""
            var out = {summary:{}, regional:{}, rows:[]};
            try {
                var target = null;
                Ext.StoreManager.items.forEach(function(s){
                    try { if (!target && s.storeId === 'Top100description') target = s; } catch(e) {}
                });
                if (target) {
                    for (var i=0;i<target.getCount();i++) out.rows.push(target.getAt(i).data);
                }
            } catch(e) {}
            return out;
        """)
        return payload
    finally:
        driver.quit()

if __name__=='__main__':
    print('ALPHALINER WEEKLY UPDATE')
    payload=fetch_bundle(True)
    print('Rows collected:', len(payload.get('rows',[])))
