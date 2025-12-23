import argparse
import json
import sqlite3
import time
import uuid
from pathlib import Path

from openai import OpenAI


def _load_main_db(db_path: Path) -> dict:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    row = cur.execute("SELECT value FROM app_data WHERE key = ?", ("main_db",)).fetchone()
    con.close()
    if not row:
        return {}
    return json.loads(row[0])


def _save_main_db(db_path: Path, data: dict) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=4)
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO app_data (key, value) VALUES (?, ?)",
        ("main_db", payload),
    )
    con.commit()
    con.close()


def _mask_key(value: str) -> str:
    if not value:
        return value
    if len(value) <= 8:
        return value
    return f"{value[:4]}...{value[-4:]}"


def _iter_netmind_spaces(db: dict):
    for sp_id, sp in (db.get("spaces") or {}).items():
        if not isinstance(sp, dict):
            continue
        if sp.get("card_type") != "netmind":
            continue
        yield sp_id, sp


def _ensure_space(db_path: Path, db: dict, name: str, model: str) -> str:
    space_id = str(uuid.uuid4())
    spaces = db.get("spaces")
    if not isinstance(spaces, dict):
        spaces = {}
    spaces[space_id] = {
        "id": space_id,
        "name": name,
        "description": "",
        "cover": "default.png",
        "cover_type": "image",
        "card_type": "netmind",
        "cerebrium_timeout_seconds": 300,
        "templates": {},
        "netmind_model": model,
        "netmind_upstream_model": model,
        "websockets_config": None,
        "demos": [],
    }
    db["spaces"] = spaces

    backup_path = db_path.with_suffix(db_path.suffix + f".bak.{int(time.time())}")
    backup_path.write_bytes(db_path.read_bytes())

    _save_main_db(db_path, db)
    return space_id


def _print_python_snippet(base_url: str, api_key: str, model: str, prompt: str, max_tokens: int):
    snippet = f"""from openai import OpenAI

client = OpenAI(
    api_key=\"{api_key}\",
    base_url=\"{base_url.rstrip('/')}\"
)

response = client.chat.completions.create(
    model=\"{model}\",
    messages=[
        {{\"role\": \"user\", \"content\": \"{prompt}\"}}
    ],
    stream=True,
    max_tokens={max_tokens}
)

for chunk in response:
    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end='')
"""
    print(snippet)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="instance/database.sqlite")
    parser.add_argument("--base-url", default="http://127.0.0.1:5001/api/v1")
    parser.add_argument("--api-key", default="")

    parser.add_argument("--list", action="store_true")

    parser.add_argument("--create-space", action="store_true")
    parser.add_argument("--space-name", default="Test NetMind Chat")
    parser.add_argument("--space-model", default="")

    parser.add_argument("--space-id", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--prompt", default="Hello!")
    parser.add_argument("--max-tokens", type=int, default=512)

    parser.add_argument("--print-snippet", action="store_true")
    parser.add_argument("--run", action="store_true")

    args = parser.parse_args()

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    db = _load_main_db(db_path)

    if args.create_space:
        model = args.space_model.strip()
        if not model:
            raise SystemExit("--create-space requires --space-model")
        new_id = _ensure_space(db_path, db, args.space_name, model)
        print(f"created_space_id={new_id}")
        return

    spaces = list(_iter_netmind_spaces(db))

    if args.list:
        print(f"netmind_spaces_count={len(spaces)}")
        for sp_id, sp in spaces:
            print(
                "space_id=", sp_id,
                "name=", sp.get("name"),
                "model=", sp.get("netmind_model"),
                "upstream_model=", sp.get("netmind_upstream_model"),
            )
        return

    resolved_model = (args.model or "").strip()
    resolved_space_id = (args.space_id or "").strip()

    if resolved_space_id:
        sp = (db.get("spaces") or {}).get(resolved_space_id)
        if not isinstance(sp, dict):
            raise SystemExit(f"space not found: {resolved_space_id}")
        if sp.get("card_type") != "netmind":
            raise SystemExit(f"space is not netmind: {resolved_space_id}")
        if not resolved_model:
            resolved_model = (sp.get("netmind_model") or "").strip()

    if not resolved_model:
        if spaces:
            resolved_model = ((spaces[0][1] or {}).get("netmind_model") or "").strip()

    if not resolved_model:
        raise SystemExit("No netmind model found. Use --list or create a netmind space first.")

    if args.print_snippet:
        api_key = args.api_key or "YOUR_API_KEY_HERE"
        _print_python_snippet(args.base_url, api_key, resolved_model, args.prompt, args.max_tokens)
        return

    if args.run:
        if not args.api_key:
            raise SystemExit("--run requires --api-key")

        client = OpenAI(api_key=args.api_key, base_url=args.base_url.rstrip("/"))
        stream = client.chat.completions.create(
            model=resolved_model,
            messages=[{"role": "user", "content": args.prompt}],
            stream=True,
            max_tokens=args.max_tokens,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                print(chunk.choices[0].delta.content, end="")
        print("\n")
        return

    print("Nothing to do. Use --list, --print-snippet, or --run.")
    if args.api_key:
        print("api_key_masked=", _mask_key(args.api_key))


if __name__ == "__main__":
    main()
