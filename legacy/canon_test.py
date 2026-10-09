import asyncio
import shutil
import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex

shutil.rmtree("/tmp/dnd-canon-test", ignore_errors=True)
P, C = "discord", "canon-test"

ri = RulesIndex("/home/byronwai/dnd-dm-bot/data/rules.db", "http://127.0.0.1:8081")
e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/tmp/dnd-canon-test",
             max_tokens=250, rules_index=ri)
e.set_party(P, C, {
    "大力蕉": {"occupation": "Druid",
              "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12, "WIS": 17,
                        "CHA": 9},
              "hp_now": 11, "hp_max": 11, "owner": "Ewwwww"},
    "依思": {"occupation": "Warlock",
            "stats": {"STR": 11, "DEX": 14, "CON": 16, "INT": 11, "WIS": 14,
                      "CHA": 17},
            "hp_now": 9, "hp_max": 9, "owner": "GonJK"},
})
# 误导性历史：先前剧情把大力蕉叫成法師
e.log_message(P, C, "DM", "大力蕉揮動法杖施放奧術光芒……")
e.log_message(P, C, "DM", "【DM 校正】大力蕉是德魯伊（Druid），不是法師。")

async def main():
    r = await e.dm_reply(P, C, "大力蕉", "我是什麼職業？我用什麼力量戰鬥？")
    print(r[:400])

asyncio.run(main())
