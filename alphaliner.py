# -*- coding: utf-8 -*-
import os, time, sqlite3, hashlib, datetime
import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

ALPHALINER_URL='https://alphaliner.axsmarine.com/PublicTop100/'
BASE_DIR=os.path.dirname(os.path.abspath(__file__))
DB_FILE=os.path.join(BASE_DIR,'alphaliner_history.db')


def get_db_conn():
    conn=sqlite3.connect(DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS snapshot_master (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,captured_at TEXT,snapshot_hash TEXT UNIQUE,row_count INTEGER)")
    conn.commit()
    return conn


def make_snapshot_hash(df):
    return hashlib.sha256(df.fillna('').astype(str).to_csv(index=False).encode()).hexdigest()


def save_snapshot(df):
    conn=get_db_conn();cur=conn.cursor()
    snap_hash=make_snapshot_hash(df)
    cur.execute('SELECT snapshot_id FROM snapshot_master WHERE snapshot_hash=?',(snap_hash,))
    row=cur.fetchone()
    if row:
        print(f'Existing snapshot: {row[0]}')
        conn.close(); return row[0]
    cur.execute('INSERT INTO snapshot_master(captured_at,snapshot_hash,row_count) VALUES (?,?,?)',
                (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),snap_hash,len(df)))
    conn.commit(); sid=cur.lastrowid; conn.close(); return sid


def fetch_bundle():
    opts=Options()
    opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    driver=webdriver.Chrome(service=Service(ChromeDriverManager().install()),options=opts)
    try:
        driver.get(ALPHALINER_URL)
        time.sleep(8)
        title=driver.title
        df=pd.DataFrame([{'page_title':title}])
        return df
    finally:
        driver.quit()


if __name__=='__main__':
    print('ALPHALINER WEEKLY UPDATE')
    df=fetch_bundle()
    sid=save_snapshot(df)
    print(f'Snapshot Saved: {sid}')
