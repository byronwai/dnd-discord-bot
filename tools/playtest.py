import asyncio
import shutil
import sys
import time

sys.path.insert(0, "/home/comfyui/dnd-dm-bot")
from engine.dm import DMEngine
from engine.rules import RulesIndex
from engine.status import build_status

shutil.rmtree("/tmp/dnd-playtest", ignore_errors=True)
P, C = "discord", "playtest-001"

ri = RulesIndex("/home/comfyui/dnd-dm-bot/data/rules.db",
                "http://127.0.0.1:11434", "mxbai-embed-large")
e = DMEngine("http://127.0.0.1:11434", "gemma3:27b-it-qat", "/tmp/dnd-playtest",
             max_history=40, max_tokens=700, rules_index=ri)
e.set_party(P, C, {
    "依思": {"occupation": "Warlock", "aliases": ["Elyse"],
            "stats": {"STR": 11, "DEX": 14, "CON": 16, "INT": 11, "WIS": 14,
                      "CHA": 17},
            "hp_now": 9, "hp_max": 9, "owner": "GonJK", "xp": 0},
    "大力蕉": {"occupation": "Druid", "aliases": ["Banana-Strong"],
              "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12, "WIS": 17,
                        "CHA": 9},
              "hp_now": 11, "hp_max": 11, "owner": "Ewwwww", "xp": 0},
})
OPENER = ("NEW ADVENTURE: a bramble-sealed crypt entrance in a misty marsh. "
          "Set the opening scene in 80-120 words, then ask the players what they do.")


def hdr(t):
    print(f"\n{'='*62}\n{t}\n{'='*62}")


async def main():
    # T1 opener
    hdr("T1 /new 開場")
    t0 = time.time()
    r = await e.dm_reply(P, C, "party", "misty marsh crypt", system_suffix=OPENER)
    print(f"[{time.time()-t0:.0f}s]", r[:300])
    si = e.session_info(P, C)
    print(">> scene:", si["scene"])

    # T2 action -> expect check card
    hdr("T2 行動：大力蕉撞門（期待檢定卡）")
    t0 = time.time()
    r = await e.dm_reply(P, C, "大力蕉", "我用肩膀使勁撞開那扇佈滿荊棘的石門！")
    print(f"[{time.time()-t0:.0f}s]", r[:400])
    ck = e.get_pending_check(P, C)
    print(">> pending:", ck)
    assert "[[dice]]" not in r and "dice roll result" not in r.lower(), "FAKE DICE!"
    print(">> 無假骰 ✓")

    # T3 consent -> verdict + narration
    if ck:
        hdr("T3 同意：骰！")
        t0 = time.time()
        r = await e.dm_reply(P, C, ck["char"], "骰！")
        print(f"[{time.time()-t0:.0f}s]", r[:500])
        assert e.get_pending_check(P, C) is None, "pending not cleared"

    # T4 中文行動（語言檢查）
    hdr("T4 依思中文行動（語言）")
    t0 = time.time()
    r = await e.dm_reply(P, C, "依思", "我舉起法杖，對荊棘噴出艾德雷德之火！")
    print(f"[{time.time()-t0:.0f}s]", r[:400])

    # T5 XP + status
    hdr("T5 XP / status")
    from engine.state import apply_state_tags
    out, log = apply_state_tags("[[xp:大力蕉:300]]", e, P, C)
    print(">>", out)
    print(">>", e.party_text(P, C).replace("\n", " | "))

    # T6 combat
    hdr("T6 戰鬥開始 + 輪替")
    order = e.combat_start(P, C, ["依思", "大力蕉"])
    print(">> 先攻:", [(o["name"], o["init"]) for o in order])
    cur = e.combat_current(P, C)
    r = await e.dm_reply(P, C, cur["name"],
                         "我小心靠近，用武器試探眼前的黑影！")
    print(f"[{cur['name']} 行動]", r[:300])
    nxt = e.combat_current(P, C)
    print(">> 下一手:", nxt["name"], "| round:", e.combat_get(P, C)["round"])

    # T7 status 全貌
    hdr("T7 /status")
    print((await build_status(e, P, C))[:800])

    # T8 HP 標記
    hdr("T8 傷害標記")
    out, log = apply_state_tags("[[hp:依思:-4]]", e, P, C)
    print(">>", out, "| DB:", e.get_party(P, C)["依思"]["hp_now"], "/9")

    print("\nPLAYTEST COMPLETE — 全流程無例外")


asyncio.run(main())
