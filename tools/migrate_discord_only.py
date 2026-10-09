"""One-off migration: fold shared-table sessions into their Discord chat and
drop Telegram + table machinery (Discord-only bot).

Before removal, live games lived under the storage key ('table', CODE) with
bindings mapping each platform chat to the code. This script:
  1. backs up campaign.db -> campaign.db.pre-discord-only.bak
  2. re-keys every shared table's rows (sessions/messages/items/backups) to
     the Discord chat bound to it (Telegram bindings are ignored and their
     rows deleted);
  3. deletes all remaining telegram rows;
  4. drops the bindings/tables tables.

Idempotent: safe to re-run (no-op once migrated).

Usage: ~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/migrate_discord_only.py
"""

import os
import shutil
import sqlite3
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "campaign.db")


def main():
    if not os.path.exists(DB):
        print(f"no campaign db at {DB}")
        sys.exit(1)
    db = sqlite3.connect(DB)
    tables = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "bindings" not in tables or "tables" not in tables:
        print("already migrated (no bindings/tables) — nothing to do")
        # still purge stray telegram rows if any
        db.execute("DELETE FROM messages WHERE platform='telegram'")
        db.execute("DELETE FROM items WHERE platform='telegram'")
        db.execute("DELETE FROM sessions WHERE platform='telegram'")
        db.commit()
        db.close()
        return

    bak = DB + ".pre-discord-only.bak"
    shutil.copy2(DB, bak)
    print(f"backup -> {bak}")

    bindings = db.execute(
        "SELECT platform, chat_id, table_id FROM bindings").fetchall()
    by_table: dict[str, list[tuple[str, str]]] = {}
    for platform, chat_id, table_id in bindings:
        by_table.setdefault(table_id, []).append((platform, chat_id))

    for code, members in sorted(by_table.items()):
        discord = [c for p, c in members if p == "discord"]
        if not discord:
            print(f"table {code}: NO discord member "
                  f"({members}) — rows dropped, no destination")
            continue
        target = discord[0]  # a table had at most one binding per platform
        for tbl, pk_cols in (("sessions", None), ("messages", None),
                             ("items", None), ("backups", None)):
            rows = db.execute(
                f"SELECT * FROM {tbl} WHERE platform='table' AND chat_id=?",
                (code,)).fetchall()
            if not rows:
                continue
            cols = [d[1] for d in db.execute(
                f"PRAGMA table_info({tbl})").fetchall()]
            if tbl == "sessions":
                # PK (platform, chat_id): replace any row already at target,
                # then drop the original table-keyed row
                for row in rows:
                    d = dict(zip(cols, row))
                    d["platform"], d["chat_id"] = "discord", target
                    fields = ", ".join(cols)
                    marks = ", ".join("?" * len(cols))
                    db.execute(
                        f"INSERT OR REPLACE INTO sessions ({fields}) "
                        f"VALUES ({marks})",
                        [d[c] for c in cols])
                db.execute(
                    "DELETE FROM sessions WHERE platform='table' AND chat_id=?",
                    (code,))
            else:
                db.execute(
                    f"UPDATE {tbl} SET platform='discord', chat_id=? "
                    f"WHERE platform='table' AND chat_id=?", (target, code))
            print(f"table {code} -> discord/{target}: {tbl} {len(rows)} rows")

    for tbl in ("messages", "items", "sessions", "backups"):
        cur = db.execute(f"DELETE FROM {tbl} WHERE platform='telegram'")
        if cur.rowcount:
            print(f"deleted {cur.rowcount} telegram rows from {tbl}")
    db.execute("DROP TABLE IF EXISTS bindings")
    db.execute("DROP TABLE IF EXISTS tables")
    db.commit()
    db.close()
    print("migration complete:", time.strftime("%Y-%m-%d %H:%M"))


if __name__ == "__main__":
    main()
