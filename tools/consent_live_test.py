import asyncio
import shutil
import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex

shutil.rmtree("/tmp/dnd-consent-live", ignore_errors=True)
P, C = "discord", "consent-live"

ri = RulesIndex("/home/byronwai/dnd-dm-bot/data/rules.db", "http://127.0.0.1:8081")
e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/tmp/dnd-consent-live",
             max_tokens=500, rules_index=ri)
e.set_party(P, C, {"大力蕉": {
    "occupation": "Druid",
    "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12, "WIS": 17, "CHA": 9},
    "hp_now": 11, "hp_max": 11, "owner": "Ewwwww"}})
suffix = ("NEW ADVENTURE: opening scene: a bramble-sealed door in a drowned "
          "crypt, 80-120 words, end asking what the players do.")


async def main():
    await e.dm_reply(P, C, "party", "a drowned crypt", system_suffix=suffix)
    print("=== TURN 1: player action (should produce a check card) ===")
    r1 = await e.dm_reply(P, C, "大力蕉", "我要用力推開那扇佈滿荊棘的門。")
    print(r1[:450])
    ck = e.get_pending_check(P, C)
    print("PENDING:", ck)
    if not ck:
        print("!! no pending check — DM did not emit [[check:...]]")
        return
    print()
    print("=== TURN 2: player consents (骰) ===")
    r2 = await e.dm_reply(P, C, "大力蕉", "骰！")
    print(r2[:600])
    print("PENDING after:", e.get_pending_check(P, C))


asyncio.run(main())
