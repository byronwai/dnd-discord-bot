import sys

sys.path.insert(0, "/home/byronwai/dnd-dm-bot")
from engine.dm import DMEngine

e = DMEngine("http://127.0.0.1:8080", "dnd-dm", "/home/byronwai/dnd-dm-bot/data")
P, C = "discord", "1557388849959674006"

party = e.get_party(P, C)
print("目前職業：", {n: v.get("occupation") for n, v in party.items() if isinstance(v, dict)})

# DM 校正訊息：寫入歷史，下一回合 DM 一定會看到
note = ("【DM 校正 / CANONICAL CORRECTION】大力蕉是德魯伊（Druid），不是法師。"
        "依思是術士（Warlock）。所有先前的法師稱呼都是口誤，以後必須依照隊伍表"
        "的職業敘述。劇情其餘部分保持不變。")
e.log_message(P, C, "DM", note)
print("校正訊息已寫入歷史")
