import json
import sqlite3


def main():
    con = sqlite3.connect('instance/database.sqlite')
    cur = con.cursor()
    row = cur.execute("SELECT value FROM app_data WHERE key = 'main_db'").fetchone()
    con.close()

    data = json.loads(row[0]) if row else {}
    spaces = data.get('spaces', {}) or {}

    ws = []
    for sid, space in spaces.items():
        if (space or {}).get('card_type') == 'websockets':
            ws.append((sid, (space or {}).get('name') or ''))

    ws.sort(key=lambda x: x[1])

    print(f'websockets spaces: {len(ws)}')
    for sid, name in ws:
        print(f'{sid}\t{name}')


if __name__ == '__main__':
    main()
