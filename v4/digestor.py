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
            "你是遊戲引擎的意圖解析器。把玩家的動作轉成「恰好一個」JSON 物件，"
            "不要輸出其他文字。\n"
            f"可用動作: {'|'.join(ACTIONS)}\n"
            f"玩家角色: {'、'.join(party_names)}\n"
            f"目前場景: {scene_name}"
            f"（通道: {'、'.join(exits) if exits else '無'}）\n"
            + tgt_line +
            '欄位: {"action","actor","target","item","spell","destination",'
            '"utterance","confidence"} — 用不到的欄位填空字串；'
            "confidence 是 0~1 的把握值。\n"
            '範例:「大力蕉，幫我砍那隻哥布林」→ '
            '{"action":"attack","actor":"大力蕉","target":"哥布林","item":"",'
            '"spell":"","destination":"","utterance":"","confidence":0.9}\n'
            '範例:「我們去酒館吧」→ {"action":"move","actor":"",'
            '"target":"","item":"","spell":"","destination":"酒館",'
            '"utterance":"","confidence":0.9}\n'
            "規則：若玩家提出「自創的花招或新方法」（不符標準動作、但故事上"
            "合理可行，例如用魚叉勾住桅杆盪過去、把火把丟進水裡製造蒸汽），"
            '輸出 {"action":"creative","utterance":"方法摘要",'
            '"ability":"建議屬性(STR/DEX/CON/INT/WIS/CHA)","confidence":把握值}。\n'
            "若玩家「無中生有掏出沒有的物品」（例如突然拿出火箭筒），照常輸出 "
            "use+item——引擎會拒絕並吐槽。\n"
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
