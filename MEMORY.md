# MEMORY — ZCode 實例交接檔

> 給下一個 Zcode 開發實例的第一份讀物。讀完這份 + `V3_SUMMARY.md` 即可無縫接手。
> 交接日期：2026-10-09（v3 封存；**v4 設計已立**：引擎驅動、LLM 為週邊，見 `V4_DESIGN.md`）
> · 系統狀態：dm-bot + dnd-health 運行中（GB10，v3 程式碼）
> · v3 完整形態與第四波防護清單見 `V3_SUMMARY.md`；設計哲學見 `DESIGN.md`；
> 備份＝GB10 `campaign.db.v3-20261009.bak` ＋ 本資料夾 `dnd-dm-bot-export-20261009.tgz`。

## 30 秒現況

D&D DM 機器人（**僅 Discord** `Dungeon-and-Dragon#1869`）運行於 **GB10 DGX Spark
（10.5.28.210）**，大腦 **gemma3:27b-it-qat**（Ollama，16k ctx，~16-24s/回合），
規則雙層接地（恆常核心手冊 compendium + 2918 段 SRD 混合檢索）。Pi 為冷備援
（仍是舊版程式碼）。開發資料夾 `D:\zcode\dnd-dm-bot\` 是程式碼真相源，
與 GB10 逐檔 md5 一致（已驗）。Telegram 與共享桌功能已移除（2026-10-08）；
線上桌 BTWQS6 已併入 Discord 頻道 `1557388849959674006`。

## 必讀檔案（按順序）

0. **`DESIGN.md`** — 遊戲設計與系統架構正式存檔（2026-10-08）：設計哲學、
   架構圖、完整遊戲機制、tag 協議、資料模型、開發經驗教訓——接手先讀這份

1. **`DEV_DIARY.md`** — 完整開發史（含第七節：Discord-only + 27b + 規則雙層接地）
2. **`HANDOFF.md`** — 架構、模型選型理由、指令手冊、運維、回退流程
3. **`README.md`** — 系統概述

## 存取（金鑰在本專案內）

```
ssh -i D:\zcode\dnd-dm-bot\keys\dnd_ed25519 comfyui@10.5.28.210   # 主力
ssh -i D:\zcode\dnd-dm-bot\keys\dnd_ed25519 byronwai@192.168.79.78 # 備援
```
GB10 sudo 密碼：`comfyui`。DISCORD_TOKEN 在 GB10 `~/dnd-dm-bot/.env`。

## 本輪改造（2026-10-08 下午，全已部署驗證）

- **Discord-only**：刪 Telegram adapter；共享桌（tables/bindings/`/table_new`
  `/join`）全拆；`tools/migrate_discord_only.py` 把 BTWQS6 併入 Discord 頻道
  （GB10 上有 `campaign.db.pre-discord-only.bak`，本地有 tgz 全量備份）
- **模型升級 12b→27b**（gemma3:27b-it-qat）：盒上所有大模型都是 thinking 型
  （Qwen3.8/qwen3.5-abliterated 實測 content 空、reasoning 爆）→ gemma3 家族
  唯一可靠；27b 檢定紀律與繁中明顯更好；回退只需改 .env 一行
- **規則雙層接地**：`engine/compendium.py`（恆常核心規則，逐條對 SRD 原文驗證）
  + ingest v2（排除 Spell_Lists/A-Z 垃圾與重複、kind 分類、**title+body 一起嵌入**
  —法術/怪物名字只在標題裡，純內文向量會找不到 Fireball）+ rules.py v2
  （zh→en 詞典、同意回合跳過檢索、cosine+標題前綴+內文+kind 混合評分）
- 檢索驗證：「火球術」→Fireball、「死亡豁免」→Death Saving Throws、
  「哥布林」→Goblin、「治療藥水」→Potion of Healing 全命中

## 已知未修（無痛小事）

- 27b 回合 16-24s（12b 是 7-11s）——品質換速度，可接受；抱怨就回退 .env
- DM 偶爾重複解釋檢定卡、戰鬥輪替時延續上一個待決檢定（純外觀，引擎狀態正確）
- 審計補救觸發時該回合多 ~5-10 秒

## 架構心得（別重蹈覆轍）

**引擎判定、模型敘事**——事實（骰/DC/HP/物品/XP/成敗）全部由確定性程式決定，
LLM 只把事實寫成小說。規則接地同樣分層：**核心規則恆常注入**（不放給檢索賭運氣），
**長尾規則才檢索**（法術/怪物/物品條目）。結構性修復（title+body 嵌入、kind 分類）
比評分調參更有效——先修資料，再調分數。

## 部署紀律（本專案用血換的）

**測試斷言失敗會中斷 `&&` 鏈 → 部署靜默跳過**；scp 多檔到不存在的目錄也會
靜默失敗。部署後必跑 `md5sum` 對帳本地 vs GB10（本輪又抓到 requirements.txt
/.env.example 兩檔沒到位，已補）。

## 快速操作

```bash
# GB10 上：
sudo systemctl restart dm-bot && sudo journalctl -u dm-bot -f   # 重啟+看log
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/status.py       # 遊戲狀態
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/playtest.py     # e2e 冒煙 (~3min)
# 本地（Windows）開發循環：改 code → py_compile → 測試 → scp 到 GB10 同路徑
# → restart dm-bot → md5 對帳 → 在 /tmp 跑 playtest 類腳本驗證
```

## 下一步建議（roadmap）

1. 場景卡圖像生成（GB10 上有現成 ComfyUI！）
2. 長休／短休指令（compendium 已帶規則，只缺指令與 hit-dice 狀態）
3. 先攻跨戰鬥持續（`[[scene]]` 式標記）
4. 規則檢索快取 embedding 已做；可再考慮 zh 查詢的 class 感知加權
