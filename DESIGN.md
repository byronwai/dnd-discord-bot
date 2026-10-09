# D&D DM Bot — 遊戲設計與系統架構文件（Game Design & Architecture）

> 版本：2026-10-08（第三波改造後的完整形態）
> 本文件是當前遊戲設計的正式存檔（archive），同時記錄開發經驗。
> 部署與運維細節見 `HANDOFF.md`；開發史見 `DEV_DIARY.md`。

---

## 一、核心設計哲學（The One Rule）

> **引擎判定、模型敘事** — Engine decides, model narrates.

所有「事實」由確定性程式碼決定：骰值、DC、AC、命中、傷害、HP、物品、XP、法術格、
死亡。LLM 只負責把事實寫成好看的小說。每一次模型作弊被抓到，我們就把一塊判定權
從模型手上收回引擎。到最終形態，模型即使想騙，也沒有可下手的表面——它看不到角色
真名（placeholder 制），擲不了骰（同意制＋/attack 全引擎結算），改不了 HP（tag 制），
甚至連「敵人偷偷咬你一口」都會被系統延後並強制補述。

由此推導的三條實作鐵律：
1. **結構性防護 > 列舉式防護**。別名表追不完生成式模型的變體；把名字換成代號、
   把擲骰收進引擎、把傷害改成系統結算，才是釜底抽薪。
2. **渲染即教材**。任何餵回模型的文字（渲染結果、摘要、事件）都會被它模仿——
   渲染「沒有變化的狀態」會教會它重複發 tag；內部摘要要明示「不得模仿此格式」。
3. **提示詞分層**：常憲法（system prompt）管長期紀律，回合級指示（directive）
   管當下行為。27b 對後者的服從度遠高於前者。

---

## 二、系統架構（Architecture）

```
Discord（單一伺服器，兩個 bot）
├─ Dungeon-and-Dragon（dm-bot，主遊戲）
│    adapters/discord_bot.py  ── 指令、freeform 對話、權限、語言、戰鬥輪替
│    │
│    engine/（確定性遊戲引擎）
│    ├─ dm.py        DMEngine：持久化、提示詞組裝、LLM 串流、檢定/攻擊結算、
│    │               戰鬥、休息、法術格、筆記、綁定、事件圖譜、延後結算
│    ├─ charlib.py   角色數學：PB、預設 AC、豁免熟練、核心技能、法術格表
│    ├─ checks.py    同意制檢定/豁免/攻擊卡、判定渲染、total_mod
│    ├─ state.py     標記機器：狀態/戰鬥/敵人/法術/休息/筆記 tag 解析＋防偽
│    ├─ dice.py      骰式引擎（4d6kh3、adv、行內 [[骰式]]）
│    ├─ commands.py  職業資料、公平創角、雙語指令手冊、難度詞解析
│    ├─ intents.py   自然語言意圖（骰 d20／開新團／狀態…）
│    ├─ compendium.py 恆常核心規則（SRD 蒸餾，逐條對照原文）
│    ├─ rules.py     SRD 檢索 v2：zh→en 詞典、閘門、混合評分、(kind,title) 去重
│    └─ status.py    /status 報告
│
└─ dnd-health（dnd-health 服務，獨立 bot）
     health/health_board.py ── 每 2 秒監看 messages 表，逐回推送更新
                                #dnd-health 頻道的單一健康看板訊息

GB10（DGX Spark, 128GB 統一記憶體）
├─ Ollama :11434 ─ gemma3:27b-it-qat（非思考模型，16k ctx）＋ mxbai-embed-large
└─ SQLite
   ├─ campaign.db ─ sessions / messages / items / backups / npcs / lore /
   │                events / char_bonds（見第五節）
   └─ rules.db    ─ 2918 段 SRD chunk（title+body 聯合嵌入，kind 分類）
```

模型選型結論（血淚）：盒上所有大模型（qwen3.5/Qwen3.8/CyberStrike/deepseek-r1）
皆為思考型（content 空、reasoning 爆量），聊天節奏不可用。gemma3 家族是唯一驗證過
的非思考血統；27b 對 tag 協議、繁中與檢定紀律明顯優於 12b，代價是 16–24s/回合。

---

## 三、遊戲設計（Game Design）

### 3.1 角色（Character）
- 六屬性：伺服器擲 4d6kh3×6 依職業優先序分配；自填僅限標準組（防全 20，真抓過）。
- AC：職業起始護甲預設（Fighter 18 鏈甲+盾、Barbarian 10+DEX+CON、Rogue 11+DEX…），
  舊角色惰性計算，无需重建。
- 熟練加值 PB：等級表自動套用——攻擊恆熟練、豁免依職業豁免表、技能依職業核心技能表
  （zh 技能名直譯：洞悉/察覺/潛行/說服…）。
- 法術格：SRD 全施法/半施法/魔契（pact）表；[[spell:角色:環]] 消耗、0=戲法免費、
  空格可見地失敗；/slots 檢視；長休全恢復。
- 生命骰：職業骰 d6–d12；短休花 1 顆（骰+CON）；長休全滿＋回一半。
- 升級：HP bump＋新環法術格（滿格）＋PB 顯示；新特性由 DM 依 SRD 敘述。

### 3.2 判定分工（Adjudication）
| 情境 | 標記 | 流程 |
|---|---|---|
| 屬性檢定 | `[[check:DC|角色|WIS 洞悉]]` | 卡（顯示 需骰≥N）→玩家「骰」→引擎擲 d20+全修正→判定注入為 FINAL |
| 豁免 | `[[save:DC|角色|DEX]]` | 同上（自動加職業豁免熟練） |
| 攻擊 | `[[attack:角色|目標|+5]]` 或玩家 `/attack` | 卡→骰→vs AC；**/attack 由玩家發起=同意，引擎立即全程結算** |
| 傷害 | 行內 `[[2d10]]` | 引擎擲骰並渲染（含明細）；/attack 的傷害由引擎直接擲+套用 |
| 治療/戰利品 | `[[item:]]` `[[hp:]]` | 引擎套用進 DB |

- 暴擊：天然 20（攻擊骰池×2，引擎擲雙池並標註）；天然 1 自動失敗。
- 攻擊目標 AC 來源：敵人槽（[[enemy:]] 登記）> 玩家角色表 > 字面「AC 15」> 預設 13。

### 3.3 戰鬥（Combat v2 — 敘事驅動）
- 開戰：DM 在劇情中發 `[[combat:哥布林 x2、狼]]` → 引擎為全體（含敵人槽 哥布林①②…，
  上限 8）擲先攻並渲染順序卡；`[[combat:end]]` 結束；`/combat start [enemies]` 手動備援。
- 敵人實體：`[[enemy:哥布林①|HP 7|AC 15, 束縛]]`（容錯：負 HP=死亡並觸發全滅檢查、
  單括號、粗體包裹、尾綴狀態詞）；`[[hp:哥布林①:-4]]` 直接扣敵人血；全滅自動收戰。
  戰鬥未開時 [[enemy:]] 自動開戰；屍體重登記靜默吸收。
- 一玩家一行動：輪到你時**直接說話即行動**（freeform 由擁有者身分判定）或 `/move`；
  敵人回合壓縮進下一位玩家的敘述；輪替只在回應成功後推進（LLM 失敗不燒回合）；
  停滯 >10 分鐘由下一位玩家行動時自動跳過；/voteskip、/takeover、/attack-override、
  /say-override 為管理救場手段。
- **延後反擊（deferred retaliation）**：玩家 /attack 回合中，模型對玩家角色造成的
  傷害一律不即時生效——渲染「⏳ 敵方反擊（延後結算）」，下一回合開頭由引擎結算
  （⚔️ 敵方反擊結算：X -6 HP→4/10）並強制 DM 先補述攻擊過程。未敘述的傷害=不存在。
- 招式制 /attack：`target`（即時敵人清單+HP）＋`move`（該角色的招式：職業簽名攻擊
  ＋攜帶武器；決定屬性/熟練/建議傷害骰/是否範圍）＋`bonus` 覆寫。
  名稱帶（範圍）=AoE：單目標擲命中（僅決定暴擊），傷害濺射全體敵人。
- 引擎全程結算鏈：d20 → 命中/暴擊 → 傷害骰（逐骰顯示後加總）→ HP 條更新 →
  💀擊倒 → 🏁全滅收戰，一氣呵成，模型只許照著寫小說（指令明文禁止重擲/重算）。

### 3.4 DM 筆記（Notebook-on-paper）
- `npcs` 表：`[[npc:瑪莎|酒館老闆娘|friendly]]`（單欄狀態詞更新；重複不變=靜默）。
- `lore` 表：`[[lore:海妖女王|族人被困在海底祭壇]]`（同鍵更新）。
- 每個提示詞注入「DM 筆記」段（NPC≤8＋情報≤8，按提及新近度排序——recency 圖走訪）；
  /status 可見；/new 清空（新冒險=新世界）。憲法明文「沒寫進筆記的細節你就不該記得」，
  且 /new 開場指示要求逐 NPC/情報記錄（directive 級才有效）。

### 3.5 上下文圖譜（Context graph）
- 原文窗口：僅最後 10 則訊息逐字進提示詞（DM_RAW_WINDOW，敘事聲音用）。
- 事件邊：每回合一條**確定性**事件行（行動者＋動作摘要＋引擎結算事實），存 `events`
  表；舊回合以「近期事件摘要」注入（依 msg_id 精確切界，不重疊；明示內部格式禁模仿）。
- 節點：party/npc/lore/場景/目標皆為 DB 節點；注入按相關性（提及浮升）裁剪。
- 實測：20 回合歷史下提示詞縮 30%，滿窗趨近 60%；27b prompt token 減少=回應更快。
- 長期：原有的 LLM 摘要器保留為「故事 so far」風味層。

### 3.6 語言與難度政策
- **繁中優先**：所有回合用全中文系統提示；模糊訊息預設 zh；模型全英文回應→翻譯回繁中；
  玩家英文輸入→繁中敘述＋結尾一行英文摘要；OpenCC s2t 兜底（曾漏裝，已補）。
- **難度三檔**（/new 選擇或「血月堡（困難）」解析，存 session，/status 可見）：
  - 新手：檢定 DC 8–13；失敗不卡關（製造新情況）；**敵人登記時機械調整 HP×0.75、AC−1**；
    戰鬥可贏、敵人有士氣；XP 大方。
  - 標準：5e 原味（10/13/15/18）。
  - 困難：DC 13–18；敵人 HP×1.25、AC+1；致命但公平（無預警即死禁止、撤退永遠可選）。
- 難度是機械的不是作文——登記卡會標註「（新手難度已調低）」。

### 3.7 擁有權、安全與反作弊（縱深防禦）
- **指令層（硬）**：/pc /give /take /equip /move /attack 全比對 owner_id（Discord uid），
  非擁有者🚫；倒地（HP≤0，注意 falsy-zero）角色全路徑禁行動；冒名防護（display-name
  對撞）；/bonds 綁定稽核（char_bonds 表：bind/rebind/update/unbind 全歷史）。
- **敘事層（強軟）**：每回 OWNERSHIP 指令——「你」只能是發言者的角色；他人角色僅
  被動反應；涉及他人未完行動→總結現況+邀請該玩家；未註冊發言者不得以「你」指涉任何 PC。
- **名諱防漂移（placeholder 制）**：真名永不進提示詞（party 表/歷史/判定/事件全映射
  [PCn]）；玩家輸入的 PCn 猜測與注入語（SYSTEM CHECK VERDICT 等）一律中性化＋
  ⚠️ 已過濾公告。槽位持久（移除保留、全 reset 清空）。
- **反 vibe 骰網**：模型自寫的骰句（dice roll result is N／骰出N／🎲 N／🎲 N+M=T／
  🎲骰式→數字（無明細））全數清除；手寫攻擊卡（攻擊要求/判定/檢定×Attack/Check
  任意混搭）→解析轉換為真 pending 卡（引擎重算 need，聲明自宣數值無效）；
  stat-block 表格清除；傷害數字必須來自 [[骰式]]。
- **玩家輸入防護**：scrub_player_input（[PCn] 假冒、SYSTEM VERDICT 偽造、
  ignore instructions／忽略先前的指示 等中英注入語→〔已過濾〕＋⚠️ 公告）；
  玩家打的 [[hp]]/[[check]] tag 永不執行（僅解析 DM 回應）。

### 3.8 玩家指令面（25 個 guild 指令）
遊戲：/new（難度） /say /attack /attack-override /say-override /move /takeover
/voteskip /combat；角色：/pc /party /bonds /rollstats /classes；資源：/inv /slots
/rest /give /take /equip；雜項：/roll /status /reset /here /help。
自然語言：@提及說「骰 d20」「開新團：血月堡（困難）」「狀態」「隊伍」「職業」「物品」
「擲屬性」即執行。指令僅 guild 範圍（避免全域+guild 重複顯示），guild copy 即時生效。

### 3.9 健康看板（dnd-health 獨立 bot）
2 秒心跳監看 messages 表（MAX(id) 變化=逐回推送，繞過節流）、30 秒全量兜底；
單一訊息就地編輯：HP 條（▓░）、狀態詞（健康/受傷/危急/昏迷）、場景/難度/最後回合
時間、待決檢定、戰鬥回合+敵人條、📜 最近事件（events 表）；只讀 DB，不碰主 bot。

---

## 四、提示詞結構（Prompt anatomy，每回合）

1. 全中文系統提示（憲法）：風格、判定分工（含「建議行動≠已宣告行動」）、tag 文法、
   代號制、筆記習慣、難度方針塊（{difficulty} 動態注入）、死亡規則（HP0 不行動）、
   語言政策、禁 AI 自白。
2. 規則參考 = CORE MECHANICS（恆常，compendium.py，逐條對 SRD 原文驗證）
   ＋ SRD LOOKUP（本回合檢索 k=3）。
3. 隊伍表（AC/PB/屬性/HP/法術格/物品；人名已代號化）＋冒險日誌（摘要）
   ＋場景/目標＋難度。
4. DM 筆記（NPC/情報，recency 截斷）＋戰鬥塊（回合/順序/當前行動者）
   ＋近期事件摘要（舊回合事實行）＋最近 10 則原文（代號化、狀態行已除噪）。
5. 尾端：CANONICAL 準則（代號=職業）＋回合級指示（OWNERSHIP／攻擊判定與已套用
   傷害／延後結算補述／NEW ADVENTURE 開場含筆記指令／英文輸入→zh 主體+en 摘要行）。

---

## 五、資料模型（campaign.db）

| 表 | 內容 |
|---|---|
| sessions | party(JSON) summary scene objective difficulty pending_check(JSON) combat(JSON) slots(JSON 代號槽) deferred(JSON 延後傷害佇列) |
| messages | 完整對話（role/name/user_id/content/ts）——原文層 |
| events | 每回合一條確定性事件行（msg_id 對齊原文窗口切界）——圖譜邊 |
| items | 隊伍物品（kind=item/equipment；名稱進入時清洗 tag 殘渣） |
| npcs / lore | DM 筆記節點（upsert、無變化靜默） |
| char_bonds | 玩家↔角色綁定稽核（bind/rebind/update/unbind＋時間戳） |
| backups | reset/new 前快照（party+items+summary），restore_backup() 可復原 |

rules.db：chunks(title, path, kind, src, text, vec)——title+body 聯合嵌入
（法術/怪物名字只在標題，純 body 向量永遠找不到 Fireball——本專案最重要單一修復）、
排除了 Spell_Lists 名稱索引與 Spells_A-Z 重複（3479→2918）。

---

## 六、開發經驗（Development Experience）

### 6.1 血淚教訓（按傷害排序）
1. **思考型模型不可用於節奏遊戲**：content 空、reasoning 爆量。選模型先實測
   `content` 是否直接串流，別看參數量。
2. **渲染回饋迴圈**：無變化的 HP tag 渲染成 ❤️ 行→進歷史→模型學會每回合重發→
   訊息指數膨脹。修法=無變化靜默＋歷史除噪。任何會進提示詞的渲染都要想「模型會
   不會模仿」。
3. **未敘述的狀態變化=UX 犯罪**：鯊魚無聲咬人事件→延後結算+強制補述。遊戲的
   可信度建立在「每個數字都有畫面」。
4. **falsy-zero**：`hp or 1` 把 0 變 1，倒地守門整組失效。比較一律用顯式
   `is not None`＋不等號（`<=0`、`>=20`），不用精確相等——這是玩家明確要求的
   紀律，且真的連續抓到兩個這類 bug。
5. **互動雙回應**：_announce 用掉 response 後再 defer → InteractionResponded，
   /attack 靜默死三連。回應前先 `response.is_done()`。
6. **靜默部署失敗**：&& 鏈斷在測試、scp 到不存在的目錄、ALTER 語句被腳本吃掉
   DEFAULT 關鍵字而被 try/except 吞掉——每次都靠 md5 對帳抓回來。部署後必驗。
7. **危險的自動修補腳本**：行拼接把 prepend 插錯分支、字串斷行——大改用錨點
   Edit，寧可慢，改完必 py_compile+行為測試。

### 6.2 模型行為學（27b 實測）
- 憲法寫十遍不如回合指示一遍（筆記習慣、開場記錄、AoE 說明全靠 directive 落地）。
- 模型會**模仿引擎的一切格式**：卡面、骰行、事件條列——防禦要「識別其變體並
  轉換/清除」，且變體會漂移（Attack→Check、要求→判定），regex 要覆蓋詞形族。
- 模型自帶帳本（敵人 HP 自己算、常算錯成負數）——讓引擎帳本成為唯一真相，
  並把模型的再登記轉為同步操作。
- 建議清單裡的選項會被它當成已宣告行動執行——「建議≠行動」須明文。

### 6.3 補償原則
系統出錯超過兩回合未修，補償要走在修復前面：鯊魚正式擊敗、全隊 +300 XP 升級、
藥水返還、錯損的 HP 上限修復——全部走引擎真實流程（升級機器、事件圖譜、頻道公告），
讓帳本與道歉同時成立。

---

## 七、現況與已知邊界

- 已運行：25 指令、全 5e 機械層（AC/PB/技能/豁免/攻擊/傷害/法術格/休息/升級/敵人
  實體/AoE/延後反擊）、DM 筆記、事件圖譜、繁中政策、三檔難度、健康看板。
- 誠實邊界（設計取捨，非 bug）：敘事層守門是強軟（模型仍偶爾越線，引擎事實層免疫）；
  技能熟練是職業固定表（玩家不可自選）；死亡豁免/狀態持續有規則無計數器；
  無網格/距離；遭遇非 CR 預算；等級特性由 DM 敘述非機械授予。
- 下一步候選：死亡豁免計數器、[[cond:目標|束縛]] 狀態 tag、玩家自選技能、
  金幣帳本、劇本模組載入。

*本文件由 2026-10-08 的完整系統審視生成；真相源=程式碼與 GB10 部署（md5 逐檔一致）。*
