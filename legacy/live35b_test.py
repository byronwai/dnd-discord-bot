import asyncio
import shutil
import sys
import time

sys.path.insert(0, "/home/comfyui/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex

shutil.rmtree("/tmp/dnd-35b-test", ignore_errors=True)
P, C = "discord", "live-35b"

ri = RulesIndex("/home/comfyui/dnd-dm-bot/data/rules.db",
                "http://127.0.0.1:11434", "mxbai-embed-large")
e = DMEngine("http://127.0.0.1:11434", "qwen3.5:35b", "/tmp/dnd-35b-test",
             max_history=40, max_tokens=2000, rules_index=ri)
e.set_party(P, C, {"大力蕉": {
    "occupation": "Druid",
    "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12, "WIS": 17, "CHA": 9},
    "hp_now": 11, "hp_max": 11, "owner": "Ewwwww"}})


async def main():
    t0 = time.time()
    r1 = await e.dm_reply(P, C, "大力蕉", "我要用力推開那扇佈滿荊棘的門。",
                          system_suffix="NEW ADVENTURE: a bramble-sealed crypt "
                          "door. Set the scene briefly then handle the action.")
    print(f"[turn1 {time.time()-t0:.0f}s]")
    print(r1[:500])
    ck = e.get_pending_check(P, C)
    print("PENDING:", ck)
    if not ck:
        print("!! DM 未發檢定卡")
        return
    t0 = time.time()
    r2 = await e.dm_reply(P, C, "大力蕉", "骰！")
    print(f"\n[turn2 {time.time()-t0:.0f}s]")
    print(r2[:700])


asyncio.run(main())
