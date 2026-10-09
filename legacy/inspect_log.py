import sys

sys.path.insert(0, "/home/<USER>/dnd-dm-bot")
from engine.dm import DMEngine

e = DMEngine("http://127.0.0.1:11434", "gemma3:12b-it-qat",
             "/home/<USER>/dnd-dm-bot/data")
rows = e.db.execute(
    "SELECT role, name, substr(content,1,240), datetime(ts,'unixepoch','localtime') "
    "FROM messages WHERE platform='table' AND chat_id='BTWQS6' "
    "ORDER BY id DESC LIMIT 14").fetchall()
if not rows:
    n = e.db.execute("SELECT COUNT(*) FROM messages WHERE platform='table' "
                     "AND chat_id='BTWQS6'").fetchone()[0]
    print("no rows; count =", n)
for r in reversed(rows):
    print(r[3], "|", r[0], "|", r[1], "|", (r[2] or "").replace("\n", " ")[:220])
print("PENDING:", e.get_pending_check("discord", "<CHANNEL_ID>"))
