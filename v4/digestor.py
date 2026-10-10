"""v4 digestor: free-form player text → Intent JSON (gemma3:12b).

The LLM only READS language here — its output is data the engine then
validates. Any failure falls back to the deterministic command parser,
and finally to a low-confidence 'talk' intent (a safe sink: facts only).
"""

import json
import re

import httpx

from .intent import ACTIONS, Intent, parse_command


class Digestor:
    def __init__(self, llm_url: str, model: str = "gemma3:12b-it-qat"):
        self.url = llm_url.rstrip("/")
        self.model = model

    async def digest(self, text: str, party_names, scene_name: str = "",
                     exits=(), known_targets=()) -> Intent:
        tgt_line = (f"已知目標（盡量用這些精確名稱）: {'、'.join(known_targets)}\n"
                    if known_targets else "")
        prompt = (
            "你是遊戲引擎的意圖解析器。玩家輸入係香港粵語口語，"
            "經常夾雜英文（例如「i picked a sword」「我 attack 佢」）。"
            "先把輸入用書面中文重述（paraphrase），再判斷動作。\n"
            "把玩家的動作轉成「恰好一個」JSON 物件，不要輸出其他文字。\n\n"
            "動作定義（按意思，不只按字面）：\n"
            "attack ＝企圖傷害或制服場上某人／生物（武器、拳腳、法術、擒拿）。\n"
            "         擋、閃、恐嚇、大叫、追趕都唔算。撞門、劈鎖係對物件，"
            "用 creative。\n"
            "take   ＝拾起／拿走場景中嘅物件（執、撿、攞、pick up）。\n"
            "move   ＝去另一個地方（去、前往、go to）。\n"
            "talk   ＝同 NPC 說話或提問（傾計、問、ask）。\n"
            "search ＝搜索、調查周圍環境。\n"
            "use    ＝使用身上嘅物品。\n"
            "cast   ＝施展法術或技能。\n"
            "creative＝自創花招（對物件或環境嘅非標準動作）。\n"
            "claim  ＝玩家宣稱自己得到能力、等級、物品（「我升到99級」）"
            "——唔係真嘅動作。\n"
            "meta   ＝願望、目標、感受、角色想法。\n"
            "chat   ＝玩家之間嘅對話（唔係對 DM 講）。\n"
            "pass   ＝等待、跳過。\n\n"
            f"玩家角色: {'、'.join(party_names)}\n"
            f"目前場景: {scene_name}"
            f"（通道: {'、'.join(exits) if exits else '無'}）\n"
            + tgt_line +
            '欄位: {"paraphrase","action","actor","target","item",'
            '"spell","destination","utterance"} — '
            "paraphrase 係書面中文重述；用不到嘅欄位填空字串。\n\n"
            "範例（真實粵語＋混英文）：\n"
            '「我用劍劈小明」→ {"paraphrase":"用劍砍小明","action":"attack",'
            '"actor":"","target":"小明","item":"劍"}\n'
            '「揼佢一拳」→ {"paraphrase":"打他一拳","action":"attack",'
            '"target":"最近的敵人"}\n'
            '「篤爆個鎖」→ {"paraphrase":"撬開門鎖","action":"creative",'
            '"utterance":"撬開門鎖"}\n'
            '「執起地下把劍」→ {"paraphrase":"拾起地上的劍","action":"take",'
            '"item":"劍"}\n'
            '「i picked a sword」→ {"paraphrase":"拾起一把劍",'
            '"action":"take","item":"劍"}\n'
            '「我 attack 嗰隻 goblin」→ {"paraphrase":"攻擊那隻哥布林",'
            '"action":"attack","target":"哥布林"}\n'
            '「我升咗 99 級」→ {"paraphrase":"我升到了99級","action":"claim",'
            '"utterance":"升到99級"}\n'
            '「同老闆傾下計」→ {"paraphrase":"和老闆交談","action":"talk",'
            '"target":"老闆"}\n'
            '「周圍望下」→ {"paraphrase":"四處查看","action":"search"}\n\n'
            "拿不準時，偏向遊戲動作（玩家的行動不能被漏掉）。\n"
            f'玩家輸入:「{text}」→')
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                r = await c.post(f"{self.url}/v1/chat/completions", json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1, "max_tokens": 200})
                r.raise_for_status()
                out = r.json()["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", out, re.S)
            data = json.loads(m.group(0))
            action = data.get("action", "")
            if action not in ACTIONS:
                raise ValueError(f"bad action: {action}")
            return Intent(
                action=action,
                actor=(data.get("actor") or "").strip(),
                target=(data.get("target") or "").strip(),
                item=(data.get("item") or "").strip(),
                spell=(data.get("spell") or "").strip(),
                destination=(data.get("destination") or "").strip(),
                utterance=(data.get("utterance") or "").strip(),
                raw=text,
                confidence=max(0.0, min(1.0, float(data.get("confidence") or 0.8))))
        except Exception:
            it = parse_command(text, party_names)
            if it is None:
                it = Intent(action="talk", utterance=text, raw=text,
                            confidence=0.3)
            return it
