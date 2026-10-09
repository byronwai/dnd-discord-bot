import asyncio
import sys
import time

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine, _is_chinese
from engine.rules import RulesIndex

SETTING = ("迷霧壟罩的濱海小鎮「霧錨鎮」：鎮民深夜聽見海底傳來詭異歌聲，"
           "碼頭出現黏液足跡，漁夫接連失蹤")


async def main():
    print("is_chinese(setting):", _is_chinese(SETTING))
    ri = RulesIndex("/home/byronwai/dnd-dm-bot/data/rules.db", "http://127.0.0.1:8081")
    e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/tmp/dnd-scene2",
                 max_tokens=400, rules_index=ri)
    e.set_party("test", "1", {"Byron": "human fighter lvl 2, STR +3, AC 16, HP 28, greatsword"})
    msgs = e._build_messages("test", "1", SETTING,
                             system_suffix="新冒險：為以下設定鋪陳開場場景：" + SETTING +
                                           "。用80至150字描寫地點與處境，帶出一個鉤子，然後問玩家要做什麼。")
    print("uses ZH system prompt:", "龍與地下城" in msgs[0]["content"])
    t0 = time.time()
    reply = await e.dm_reply(
        "test", "1", "party", SETTING,
        system_suffix="新冒險：為以下設定鋪陳開場場景：" + SETTING +
                      "。用80至150字描寫地點與處境，帶出一個鉤子，然後問玩家要做什麼。")
    print(f"({time.time() - t0:.0f}s)")
    print(reply)


asyncio.run(main())
