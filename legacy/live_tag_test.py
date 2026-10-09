import asyncio
import shutil
import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex

SHUTIL = shutil.rmtree("/tmp/dnd-live", ignore_errors=True)

# throwaway chat (NOT the real table BTWQS6)
P, C = "discord", "testchat-999"


async def main():
    ri = RulesIndex("/home/byronwai/dnd-dm-bot/data/rules.db", "http://127.0.0.1:8081")
    e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/tmp/dnd-live",
                 max_tokens=400, rules_index=ri)
    e.set_party(P, C, {"大力蕉": {
        "occupation": "Fighter",
        "stats": {"STR": 16, "DEX": 13, "CON": 14, "INT": 10, "WIS": 12, "CHA": 9},
        "hp_now": 12, "hp_max": 12, "owner": "Tester"}})
    suffix = ("NEW ADVENTURE: set the opening scene for: a ruined tavern infested "
              "by goblins. 80-150 words, a hook, then ask the players what they do.")
    await e.dm_reply(P, C, "party", "a ruined tavern", system_suffix=suffix)

    reply = await e.dm_reply(P, C, "大力蕉", "我揮劍砍向最近的哥布林！")
    print("=== TURN REPLY ===")
    print(reply[:900])
    print("=== STATE AFTER ===")
    print(e.party_text(P, C))
    si = e.session_info(P, C)
    print("scene:", si["scene"], "| objective:", si["objective"])

asyncio.run(main())
