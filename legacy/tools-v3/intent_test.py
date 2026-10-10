import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine.intents import parse_intent

cases = [
    ("roll a d20", ("roll", "d20")),
    ("roll 2d6+3", ("roll", "2d6+3")),
    ("Roll adv", ("roll", "adv")),
    ("擲骰 4d6kh3", ("roll", "4d6kh3")),
    ("骰 d20", ("roll", "d20")),
    ("幫我擲個 2d6", ("roll", "2d6")),
    ("roll down the hill", None),          # story, not dice
    ("I attack the goblin", None),         # story
    ("new adventure a haunted mine", ("new", "a haunted mine")),
    ("start a new campaign", ("new", "")),
    ("開新團：血月城堡", ("new", "血月城堡")),
    ("新冒險 迷霧壟罩的小鎮", ("new", "迷霧壟罩的小鎮")),
    ("開團", ("new", "")),
    ("status", ("status", "")),
    ("遊戲狀態", ("status", "")),
    ("party", ("party", "")),
    ("隊伍", ("party", "")),
    ("classes", ("classes", "")),
    ("職業", ("classes", "")),
    ("有哪些職業？", ("classes", "")),
    ("inv", ("inv", "")),
    ("背包", ("inv", "")),
    ("物品", ("inv", "")),
    ("rollstats", ("rollstats", "")),
    ("擲屬性", ("rollstats", "")),
    ("help", ("help", "")),
    ("指令", ("help", "")),
    ("我搜查酒館", None),                        # stays story
    ("I search the tavern for goblins", None),   # stays story
    ("我撲向最靠近的哥布林", None),                # stays story
    ("我把火把放進背包", None),                    # stays story ("背包" mid-sentence)
]

fails = 0
for text, want in cases:
    got = parse_intent(text)
    ok = got == want
    if not ok:
        fails += 1
    print(("PASS" if ok else "FAIL"), repr(text), "->", got)
print("FAILURES:", fails)
