"""One-shot migration repair on GB10.

1. Merge party + items from the Pi DB (table/BTWQS6) into the live session.
2. Recreate the shared table BTWQS6 and bind Discord + Telegram chats.
3. Move the live story (messages, summary, scene, objective) onto the table key.
4. Add name aliases so anglicized names (Elyse, Dajiao…) resolve correctly.
"""

import json
import sqlite3
import sys
import time

LIVE = "/home/<USER>/dnd-dm-bot/data/campaign.db"
PI = "/tmp/pi_campaign.db"
DISCORD_CID = "<CHANNEL_ID>"
TELEGRAM_CID = "-5521072617"
CODE = "BTWQS6"

ALIASES = {"依思": ["Elyse", "Yisi", "依絲"],
           "大力蕉": ["Banana-Strong", "Dajiao", "Dali"]}

live = sqlite3.connect(LIVE)
pi = sqlite3.connect(PI)

# party + items from the Pi shared table
row = pi.execute("SELECT party FROM sessions WHERE platform='table' AND chat_id=?",
                 (CODE,)).fetchone()
party = json.loads(row[0]) if row and row[0] else {}
for name, al in ALIASES.items():
    if name in party and isinstance(party[name], dict):
        party[name]["aliases"] = al
print("restored chars:", {n: (v.get("occupation"), v.get("hp_now"))
                          for n, v in party.items() if isinstance(v, dict)})
items = pi.execute("SELECT char_name, kind, name, qty FROM items "
                   "WHERE platform='table' AND chat_id=?", (CODE,)).fetchall()
print("items from Pi:", len(items))

# live session state to carry over
lrow = live.execute("SELECT summary, scene, objective, created_at, updated_at "
                    "FROM sessions WHERE platform='discord' AND chat_id=?",
                    (DISCORD_CID,)).fetchone()
summary, scene, objective = (lrow[0] or "", lrow[1] or "", lrow[2] or "")
msgs = live.execute("SELECT role, name, user_id, content, ts FROM messages "
                    "WHERE platform='discord' AND chat_id=? ORDER BY id",
                    (DISCORD_CID,)).fetchall()
print("moving messages:", len(msgs))

# recreate the shared table on the live DB
live.execute("INSERT OR REPLACE INTO tables (table_id, created_at) VALUES (?,?)",
             (CODE, time.time()))
live.execute("INSERT OR REPLACE INTO sessions (platform, chat_id, party, summary, "
             "created_at, updated_at, scene, objective, pending_check) "
             "VALUES (?,?,?,?,?,?,?,?,?)",
             ("table", CODE, json.dumps(party, ensure_ascii=False), summary,
              lrow[3] if lrow else time.time(), time.time(), scene, objective, ""))
live.execute("DELETE FROM messages WHERE platform='table' AND chat_id=?", (CODE,))
for role, name, uid, content, ts in msgs:
    live.execute("INSERT INTO messages (platform, chat_id, role, name, user_id, "
                 "content, ts) VALUES (?,?,?,?,?,?,?)",
                 ("table", CODE, role, name, uid or "", content, ts))
for cn, kind, nm, qty in items:
    live.execute("INSERT INTO items (platform, chat_id, char_name, kind, name, "
                 "qty, ts) VALUES (?,?,?,?,?,?,?)",
                 ("table", CODE, cn, kind, nm, qty, time.time()))
for plat, cid in (("discord", DISCORD_CID), ("telegram", TELEGRAM_CID)):
    live.execute("INSERT OR REPLACE INTO bindings (platform, chat_id, table_id) "
                 "VALUES (?,?,?)", (plat, cid, CODE))
# retire the old direct-key session row (messages already moved)
live.execute("DELETE FROM sessions WHERE platform='discord' AND chat_id=?",
             (DISCORD_CID,))
live.execute("DELETE FROM messages WHERE platform='discord' AND chat_id=?",
             (DISCORD_CID,))
live.commit()

# verify
n = live.execute("SELECT COUNT(*) FROM messages WHERE platform='table' AND "
                 "chat_id=?", (CODE,)).fetchone()[0]
b = live.execute("SELECT COUNT(*) FROM bindings WHERE table_id=?", (CODE,)).fetchone()[0]
p2 = json.loads(live.execute("SELECT party FROM sessions WHERE platform='table' "
                             "AND chat_id=?", (CODE,)).fetchone()[0])
print(f"DONE: messages={n} bindings={b} chars={list(p2)}")
pi.close()
live.close()
