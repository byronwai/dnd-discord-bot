import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine
from engine.commands import roll_stats_by_class

e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/home/byronwai/dnd-dm-bot/data")
P, C = "discord", "<CHANNEL_ID>"
party = e.get_party(P, C)

party["依思"] = {
    "occupation": "Wizard",
    "stats": roll_stats_by_class("Wizard"),
    "details": "女法師, 法杖 (staff)",
    "owner": "GonJK", "owner_id": 594187886505754645,
}
party["大力蕉"] = {
    "occupation": "Fighter",
    "stats": roll_stats_by_class("Fighter"),
    "details": "(default class - re-run /pc to choose your own)",
    "owner": "Ewwwww", "owner_id": 270564798612242432,
}
e.set_party(P, C, party)
print(e.party_text(P, C))
