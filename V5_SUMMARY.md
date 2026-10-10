# V5 總結 — D&D DM Bot 第五形態（存檔文件）

> 版本：**v5-final** · 封存日期：2026-10-10 23:00（GMT+8）
> 範圍：v5 全程——2026-10-10 一日內完成（第零層→支柱 A/B→種子 3-6→收尾）。
> 真相源＝GitHub（v5 期 commits：`2d2b32e`→`b0d95dd`→`7f3674a`→`a25a6ee`
> →`5032f74`→`6f221d4`→`17ac318`→`dab8316`→`1a31ab8`＋收尾）＋
> GB10 `data/v4_state_*.json.bak-v5`（遷移前快照）。
> 設計稿：`V5_DESIGN.md`（含第零痛點全文）；v4 存檔：`V4_SUMMARY.md`。

---

## 一、現況快照（封存時）

| 項目 | 狀態 |
|---|---|
| 服務 | dm-bot + dnd-health **active**（GB10） |
| 模型 | digestor `qwen2.5:7b-instruct`（<1s）·narrator `qwen3.5:35b` think=off（串流 2.5s）——gemma 全數移除 |
| 戰局 | 主頻道：海底祭壇 ·turn 145 ·291 entries；副頻道：起點小鎮廣場 ·turn 3 |
| 指令 | 26 個（玩家 16 ·管理 10）；UX 已全樹審計 |
| 規則 | gamerules.json（＋每頻道覆寫）·plot.json（時鐘+觸發器+結局）·rules.db（/rules 中文別名檢索） |
| 測試 | `python -m pytest tests/ -q`＝18 項 0.35s 零 LLM ·selftest 固定種子 |

## 二、v5 的合約（相對 v4 的回答）

v4 把判定權交還引擎，玩家的回饋卻是「**v1 更好玩**」——玩家體驗的
不是公正，是回應。v5 的等式：**幻覺自由 × 真實後果（回應 9 × 意義 9）**
——把 v1 的手感偷回來，把 v4 的帳本藏起來；假骰假物品假等級不偷，
因為 v1 的樂趣崩潰正因為什麼都無意義。

## 三、五個機制層（全部引擎擁有）

1. **第零層（安靜引擎）**：被動分自動成功（10+mod≥低DC 不骰）、
   空場景搜索不出卡、拒絕有敘事（吐槽）——摩擦只留給真懸念。
2. **Director（預設說好）**：未追蹤的惰性道具即時物質化（木雕們）；
   骰值/價值/劇情物仍擋。LLM 只描述世界，引擎驗收入世。
3. **Affordance（A1）**：itemlib 型別能力——掟（臨時武器攻擊／落地
   可拾／落海永逝）、燒（場景火海=引擎狀態，每回合燒敵 1 點）。
   有機搜索（A3）：空場景 30% 掉平凡小物。
4. **劇情脊柱（B）**：plot.json 時鐘+觸發器+beat（npc_move/spawn/
   disposition/cond/inspire/結局），beat 進同一回合的判定與敘事——
   預生成處境，臨場生成場景，劇情追著玩家跑。
5. **存亡+檢定完備（種子 3/4）**：死亡豁免（三敗真死、天然 20 站起）、
   狀態（中毒劣勢）、靈感（補償性一次性優勢，/inspire-admin）、
   團體潛行（過半全隊）、Help（下個檢定雙骰取高，含玩家 /roll 結算）。

## 四、開發故事（一日，9+ commits）

- 模型換血：gemma3:27b 33s→qwen3.5:35b 2.5s（13×）；qwen3.5 當
  digestor 5/6 誤路由→qwen2.5:7b 9/9 全對；思考型模型必須走原生
  /api/chat 關 think（OpenAI 端點不認）；順手修 POV 漂移（第三人稱入尾律）。
- 千兩黃金鸚鵡：自己的提示詞範例毒化輸出——範例只示範結構＋防開頭
  重複指令；幻影木雕：narrator 虛構道具→Director 根治＋記錄加寬。
- 教訓流：helper 函數截斷函數體（語法合法的死代碼，結構探針抓）、
  drop_cond 把 None 值（無限期條件）當不存在（靈感被靜默吞）、
  物質化漏從地上移除（複製 bug）。
- 工程化：pytest 18 項離線回歸；rest 種類嗅探下沉引擎；敘事全文入
  ledger（kind=narration）→#dnd-health 可審閱。
- 收尾：CR 預算（遭遇隨隊伍等級縮放；plot spawn 可引 CR 帶）、
  /rules SRD 檢索（中文別名→英文標題，零 LLM）、26 指令 UX 全樹
  審計、雙頻道狀態遷移驗證（.bak-v5 快照在手）。

## 五、誠實邊界（v5 清單）

- 劇情脊柱的內容仍手寫（plot.json）；時鐘只在戰鬥外每回合 +1，
  沒有「現實時間」推進
- 狀態系統只有中毒/靈感兩個一等公民；無掩蔽/擒抱等完整戰術
- /rules 是 LIKE 檢索＋固定別名表，非語義檢索（向量庫在，未接）
- 敘事 ledger 只存 400 字截斷；重複偵測仍比對骨架而非全文
- 每頻道 plot 覆寫未做（gamerules 有了）；按鈕混合 UI 未做

## 六、v6 backlog

1. 語義 SRD 檢索（mxbai 向量庫接線）＋規則引用進敘事約束
2. 每頻道 plot.json 覆寫；時鐘支援現實時間推進
3. 狀態系統完備（掩蔽/擒抱/專注）；死亡豁免改玩家骰
4. 按鈕混合 UI（回合卡快捷）；敘事全文持久化（去截斷）
5. CI（pytest on push）；狀態 schema 版本欄位

## 七、運維速查

```bash
ssh -i keys/dnd_ed25519 <SSH_GB10>
sudo systemctl restart dm-bot dnd-health
cd ~/dnd-dm-bot && python -m pytest tests/ -q && python3 v4/cli.py selftest
# 模型：.env 的 V4_DIGEST_MODEL / V4_NARR_MODEL / V4_NARR_THINK=off
# 世界：gamerules.json；劇情：plot.json；遷移前快照：data/*.bak-v5
```

*本文件隨 v5 封存產生；v6 請先讀本文件 → `V5_DESIGN.md` → `EVOLUTION.md`。*
