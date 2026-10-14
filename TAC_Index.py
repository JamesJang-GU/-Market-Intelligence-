import sqlite3
import pandas as pd
import requests
from datetime import datetime

def init_db(db_name="TAC_Index.db"):
    """Initialize the SQLite database for TAC Index data."""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS freight_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            route TEXT,
            rate REAL
        )
    ''')
    conn.commit()
    conn.close()

def main():
    db_name = "TAC_Index.db"
    init_db(db_name)
    print("TAC Index database initialized and ready.")

if __name__ == "__main__":
    main()
