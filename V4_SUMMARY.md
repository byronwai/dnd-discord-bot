# V4 總結 — D&D DM Bot 第四形態（存檔文件）

> 版本：**v4-final** · 封存日期：2026-10-10 19:30（GMT+8）
> 範圍：v4 全程——2026-10-09 P1（純引擎）起，至 2026-10-10 深夜的 live 磨合馬拉松。
> 真相源＝程式碼（GitHub byronwai/dnd-discord-bot，87 commits，v4 期約 84 個）
> ＋ GB10 `data/v4_state_<channel>.json`（10-10 19:30 快照）。
> v1→v4 演進全圖見 `EVOLUTION.md`；架構圖見 `ARCHITECTURE.md`。

---

## 一、現況快照（封存時）

| 項目 | 狀態 |
|---|---|
| 服務 | dm-bot + dnd-health **active**（GB10；gemma3:12b digestor / 27b narrator） |
| 戰局 | 場景「海底祭壇」·turn 145 ·無戰鬥 ·ledger 291 條 |
| 隊伍 | 依思 Warlock Lv2 HP 9/11（L1 0/2）·大力蕉 Druid Lv2 HP 11/11（L1 2/3） |
| 劇情 | 與海妖女王結盟推進中：項鍊（巴鐸）→ 沉沒之城 → 海底祭壇救人；深海幽藍花仍未發現 |
| 情報 | NPC knows/disclosed 追蹤上線；/status 已知情報可查 |
| 指令 | 24 個（玩家 15 ·管理 9）；#dnd-health 指南 v2 已重發釘選 |
| 規則 | `gamerules.json`＝單一事實源（基調＋canon＋敘事規範），注入 narrator |

## 二、v4 的合約（相對 v3 的反轉）

**引擎做 70%、LLM 做 30%**——v3 是「模型主持、引擎攔截」；v4 反轉為
**引擎主持、模型點綴**：intent 解析（12b）與敘事潤飾（27b）是唯二的
LLM 職務，且都是**無狀態、可拔除**的。`python v4/cli.py selftest`
證明整局遊戲（戰鬥/物品/場景/檢定/技能/情報）零 LLM 可玩。
引擎是唯一寫入路徑：LLM 連作弊的介面都不存在，v3 的攔截式防禦
降級為輸出品質衛生（scrub/語言/重複/物品粗體）。

## 三、回合管線（v4 最終形）

```
/explore 自由文字（粵/英/混）或 24 個型別指令
  → scrub → digestor(12b)→Intent JSON（fallback：確定性解析器）
  → 安全網（talk空話術回填/move無目的地→meta/rest種類嗅探/使用技能名→skill）
  → resolve()：validate → 變異 → ledger（唯一事實流）
     ·待決檢定攜帶 effect+origin（wants_help 也隨行）
  → on_resolved：判謎即時貼出（引擎訊息）
  → _narrate：骨架+事實+世界塊(gamerules)+守衛 → 27b 串流
  → 逐字編輯進自己的訊息（最終編輯為準）；未完成→/continue 續寫
/roll d20：結算待決卡 → 效果套用 → 以「玩家原話+判定」自動續寫劇情
/combat 家族：/attack /use /skill /defend /flee /observe（各帶型別自動完成）
```

## 四、開發故事（84 commits 的教訓流）

**第一波（10-09）：反轉本身。** P1 純引擎＋selftest；P2 接上 12b/27b；
playground 頻道驗證後直接換掉主頻道 v3。早期教訓：空隊伍 StopIteration、
世界遷移 KeyError、`d20-2` 減號日一 bug。

**第二波（10-10 上午）：指令面重塑。** /say→/explore、對話式下拉→指令內
清單、桌邊聊天靜默、擁有權閘門、/inventory、/give-autocomplete。

**第三波（10-10 下午）：18 技能全接線。** 原本只有察覺/說服會觸發。
技能=帶效果的檢定（潛行→優勢、洞察→真實態度、醫藥→救醒、社交→態度階梯）；
/combat 的「技能」原本誤走 cast 燒法術格。

**第四波（10-10 傍晚）：Discord 工程馬拉松**（今天最密集的一段）：
1. 「該申請未受回應」三連（_announce/explore/inventory 舊匯入炸裂）→
   defer-first 紀律＋樹級錯誤處理器（指令必答）
2. 「正在思考…」幽靈——defer 後全走 channel.send，互動永無 followup →
   echo 改為 followup
3. 逐字輸出兩度失而復得：先修「串流訊息被判定覆蓋」（verdict 先貼、
   串流訊息自留），再發現 admin 路徑根本沒接 on_delta → 統一 runner
4. 休息雙 bug：長休被 LLM 路徑降級為短休（種類嗅探）；Warlock 契約格
   短休不回（5e 應全回）——依思 0 法術格之謎
5. 戰鬥紀錄：敵人反擊只進 ledger 不上屏——**擊倒依思的那一刀玩家看不見**；
   _post_rotation 改為回傳行；倒地救援三路（餵藥/醫藥/建議提示）
6. 「使用醫藥」被歸為物品使用→無中生有拒絕；use→skill 引擎側安全網＋別名
7. 千兩黃金鸚鵡——**我自己的提示詞範例毒化了輸出**（小模型照抄範例字句）；
   範例只示範結構＋防開頭重複指令
8. 幻影木雕——narrator 憑空描述的道具，引擎裡從未存在，下回合自然消失；
   記錄加寬、物品守衛上日誌、/give 不填對象改為放地上（可拾回）
9. 玩家求助得到氣氛而非資訊：digestor 增 wants_help（按語意判斷，regex
   只作無 LLM 後備）；空話術回填原話；「去哪」誤判 move→meta
10. 情報系統：knows+disclosed，成功交流一次一滴新情報，/status 已知情報，
    建議行動顯示 已問出 x/y
11. /combat UI 推倒重來：多型 target 參數做不出對應自動完成 →
    **拆成型別指令族**（玩家 6＋管理 6），/use 可在戰鬥中餵倒地隊友
12. /roll 結算以「玩家原話＋判定」自動續寫；/continue 先補完未完成敘事
13. gamerules.json 成為單一事實源（基調＋canon 注入 narrator，
    canonical-tail 強制）；dnd-health 修好頻道盲區＋指南版本化重發

## 五、機制總表（速查）

- **指令**：玩家 15（/pc /explore /confirm /attack /use /skill /defend
  /flee /observe /inventory /roll /give /status /continue /help）
  管理 9（explore/attack/use/skill/defend/flee/observe/roll/give-admin）
- **動作 19**：attack take move use cast talk skill search creative claim
  meta chat rest give check pass defend escape observe
- **技能 18**：全部有機械效果（見 §四第三波）；檢定=玩家骰（/explore）
  或自動骰（/combat 家族）
- **社交**：態度階梯 hostile→allied（引擎擁有）；情報一次一滴
- **守衛**：placeholder 名、假骰 scrub、重複偵測、s2t、輸入消毒、
  擁有權、NPC knows、物品粗體白名單、防開頭重複
- **持久化**：每頻道一檔（party/world/encounters/combat/inventory/ledger/
  unfinished）；gamerules.json 全域

## 六、誠實邊界（v4 清單）

- 死亡豁免、狀態、物品型錄、難度縮放：仍無（v5 種子）
- narrator 無狀態：世界塊減輕但延續性僅靠 ledger；敘事本體不持久化
  （僅 journald 前 200 字）
- 待決檢定不跨重啟；gamerules 尚無每頻道覆寫
- 防線偏輸出衛生（事實層免疫）；POV 漂移無防護
- 測試僅 selftest（已固定種子）；部署仍是 scp＋systemd

## 七、v5 backlog（已討論待實作）

1. `engine/itemlib.py` 物品型錄（DB 支撐）＋ Director（新型物件走型別
   模板＋引擎閘門）——幻影木雕的根治
2. 死亡豁免計數器、[cond:] 狀態、Inspiration（補償機制）
3. 團體檢定、Help 正式化為優勢來源
4. 敘事持久化（重播/審閱）、每頻道 gamerules 覆寫
5. 回合卡按鈕混合 UI、SRD 檢索接線、CR 預算
6. pytest＋fake-LLM 離線套件、狀態 schema 版本化遷移

## 八、運維速查

```bash
ssh -i keys/dnd_ed25519 <SSH_GB10>
sudo systemctl restart dm-bot dnd-health
cd ~/dnd-dm-bot && python3 v4/cli.py selftest     # 種子固定，可重放
python3 v4/cli.py                                 # 零 LLM 遊玩證明
journalctl -u dm-bot -f | grep -E "narrate|item guard"   # 敘事審閱軌跡
# 狀態：data/v4_state_<channel_id>.json；規則：gamerules.json（改檔重啟即改世界）
```

*本文件隨 v4 封存產生；v5 開發請先讀本文件 → `V5_DESIGN.md` → `EVOLUTION.md`。*
