import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine

e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/home/byronwai/dnd-dm-bot/data")
P, C = "discord", "1557388849959674006"

# remove rows whose char_name isn't a real character (heredoc-mangled tests)
party = e.get_party(P, C)
real = set(party.keys())
rows = e.db.execute(
    "SELECT id, char_name FROM items WHERE platform=? AND chat_id=?",
    ("table", "BTWQS6")).fetchall()
for iid, cn in rows:
    if cn not in real:
        e.db.execute("DELETE FROM items WHERE id=?", (iid,))
e.db.commit()
print("cleaned mangled rows:", sum(1 for _, cn in rows if cn not in real))

# seed clean items (proper UTF-8 via file upload)
e.inv_add(P, C, "依思", "治療藥水", 3)
e.inv_add(P, C, "依思", "法師法杖", 1, "equipment")
e.inv_add(P, C, "大力蕉", "火把", 5)
e.inv_add(P, C, "大力蕉", "木棒", 1, "equipment")

print(e.party_text(P, C))
print("--- /inv ---")
print(e.inv_all_text(P, C))
