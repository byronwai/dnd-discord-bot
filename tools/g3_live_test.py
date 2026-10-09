import asyncio
import shutil
import sys
import time

sys.path.insert(0, "/home/comfyui/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex

shutil.rmtree("/tmp/dnd-g3-test", ignore_errors=True)
P, C = "discord", "g3-live"

ri = RulesIndex("/home/comfyui/dnd-dm-bot/data/rules.db",
                "http://127.0.0.1:11434", "mxbai-embed-large")
e = DMEngine("http://127.0.0.1:11434", "gemma3:12b-it-qat", "/tmp/dnd-g3-test",
             max_history=40, max_tokens=700, rules_index=ri)
e.set_party(P, C, {"大力蕉": {
    "occupation": "Druid",
    "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12, "WIS": 17, "CHA": 9},
    "hp_now": 11, "hp_max": 11, "owner": "Ewwwww"}})
opener = ("NEW ADVENTURE: a bramble-sealed crypt door. Set the opening scene "
          "in 80-120 words, then ask the players what they do.")


async def main():
    await e.dm_reply(P, C, "party", "bramble crypt", system_suffix=opener)
    t0 = time.time()
    r = await e.dm_reply(P, C, "大力蕉", "我選(C)，直接用肩膀使勁撞開石門！")
    print(f"[action {time.time()-t0:.0f}s]")
    print(r[:420])
    ck = e.get_pending_check(P, C)
    print("PENDING:", ck)
    if not ck:
        print("!! no check card — retry once with explicit push")
        t0 = time.time()
        r = await e.dm_reply(P, C, "大力蕉", "我要立刻撞門，需要檢定就告訴我 DC。")
        print(f"[retry {time.time()-t0:.0f}s]")
        print(r[:420])
        ck = e.get_pending_check(P, C)
        print("PENDING:", ck)
    if ck:
        t0 = time.time()
        r2 = await e.dm_reply(P, C, "大力蕉", "骰！")
        print(f"\n[consent {time.time()-t0:.0f}s]")
        print(r2[:520])


asyncio.run(main())
