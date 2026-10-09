import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine
from engine.commands import starting_hp

e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/home/byronwai/dnd-dm-bot/data")
fixed = 0
for platform, cid in e.db.execute("SELECT platform, chat_id FROM sessions").fetchall():
    party = e.get_party(platform, cid)
    changed = False
    for name, entry in party.items():
        if isinstance(entry, dict) and "hp_now" not in entry:
            hp = starting_hp(entry.get("occupation", "Fighter"),
                             entry.get("stats") or {})
            entry["hp_now"] = entry["hp_max"] = hp
            fixed += 1
            changed = True
            print(f"HP init: {name} [{entry.get('occupation', '?')}] = {hp}/{hp}")
    if changed:
        e.set_party(platform, cid, party)
print("characters fixed:", fixed)
