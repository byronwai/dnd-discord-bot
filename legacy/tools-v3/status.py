"""Check game status directly on the Pi (game server).

Usage:
    ~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/status.py

Shows backend health, the rules index size, and every session's
message count, party sheet, and adventure log.
"""

import asyncio
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import httpx  # noqa: E402

from engine.dm import DMEngine  # noqa: E402
from engine.rules import RulesIndex  # noqa: E402


def _health(url: str) -> str:
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.get(f"{url.rstrip('/')}/health")
            return "OK" if resp.status_code == 200 else f"HTTP {resp.status_code}"
    except Exception:
        return "DOWN"


def main():
    data_dir = os.path.join(BASE, "data")
    llm_url = os.environ.get("LLM_URL", "http://127.0.0.1:8080")
    embed_url = os.environ.get("EMBED_URL", "http://127.0.0.1:8081")

    engine = DMEngine(llm_url=llm_url, model="dnd-dm", data_dir=data_dir)
    engine.embed_url = embed_url
    rules_db = os.path.join(data_dir, "rules.db")
    rules = RulesIndex(rules_db, embed_url) if os.path.exists(rules_db) else None

    print("=== D&D DM Bot — server status ===")
    print(f"LLM server      : {_health(llm_url)}  ({llm_url})")
    print(f"Embedding server: {_health(embed_url)}  ({embed_url})")
    print(f"Rules index     : {rules.chunks() if rules else 0} chunks")

    sessions = engine.db.execute(
        "SELECT platform, chat_id, created_at, updated_at FROM sessions "
        "ORDER BY updated_at DESC").fetchall()
    if not sessions:
        print("\n(no sessions yet — nobody has started playing)")
        return

    for platform, chat_id, created, updated in sessions:
        info = engine.session_info(platform, chat_id)
        print("\n" + "=" * 60)
        print(f"session: {platform} / chat {chat_id}")
        print(f"  started    : {__import__('time').strftime('%Y-%m-%d %H:%M', __import__('time').localtime(created))}")
        print(f"  last turn  : {__import__('time').strftime('%Y-%m-%d %H:%M', __import__('time').localtime(updated))}")
        print(f"  messages   : {info['messages']}")
        print("  party:")
        for line in engine.party_text(platform, chat_id).splitlines():
            print(f"    - {line}")
        summary = (info["summary"] or "(no adventure log yet)").strip()
        print(f"  story so far: {summary[:400]}{'…' if len(summary) > 400 else ''}")


if __name__ == "__main__":
    main()
