"""Check pro_plans in database"""
import sqlite3
import json

conn = sqlite3.connect('instance/database.sqlite')
cursor = conn.cursor()
cursor.execute("SELECT value FROM app_data WHERE key = 'main_db';")
row = cursor.fetchone()
if row:
    db = json.loads(row[0])
    pro_plans = db.get('pro_plans', [])
    print(f"Number of pro_plans: {len(pro_plans)}")
    if pro_plans:
        for p in pro_plans[:3]:
            print(f"  - {p.get('name', 'NO NAME')}: enabled={p.get('enabled')}")
    else:
        print("  pro_plans is EMPTY - Pro popup will NOT show!")
else:
    print("No database found")
conn.close()
