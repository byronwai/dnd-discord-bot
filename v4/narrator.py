"""v4 narrator: ledger facts → 繁中 prose (gemma3:27b).

Decoration only — the engine's lines are already the complete truth of the
turn; this adds novel-flavoured rendering on top. Any failure returns ""
and the game loses nothing (degraded mode by construction).
"""

import httpx


class Narrator:
    def __init__(self, llm_url: str, model: str = "gemma3:27b-it-qat"):
        self.url = llm_url.rstrip("/")
        self.model = model

    async def narrate(self, facts: list, scene: str, party_brief: str,
                      hints: list = ()) -> str:
        """hints = the engine's pre-rendered prose skeletons (templates.py).
        The LLM's ONLY job is to polish them into one flowing paragraph —
        no new facts, no numbers of its own, 60~100 字."""
        if not facts:
            return ""
        hint_block = ""
        if hints:
            hint_block = ("\n骨架句（依序作為段落骨架，可潤飾與連接，"
                          "但不得更改其內容）：\n"
                          + "\n".join(f"- {h}" for h in hints))
        prompt = (
            "你是地下城主。把下列「骨架句」潤飾成 80~160 字、連貫的"
            "繁體中文敘事段落。\n"
            "鐵律：骨架句裡的行動者、成敗、對象一律照抄不得更改；"
            "不得新增任何判定、傷害或行動；不要列出數字算式；"
            "只補感官細節與氛圍。\n"
            "例外（NPC 對話）：若骨架是「向某 NPC 交談」，你可以替該 NPC "
            "發言——依其身分與態度說出符合劇情的回應（可透露線索、提出要求、"
            "拒絕或反問），但不得給予物品、傷害或任何機械效果。\n"
            f"場景：{scene}\n隊伍現況：{party_brief}{hint_block}\n"
            "（引擎事實，僅供核對，不要複述：\n"
            + "\n".join(f"- {f}" for f in facts) + "）")
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{self.url}/v1/chat/completions", json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7, "max_tokens": 400})
                r.raise_for_status()
                text = (r.json()["choices"][0]["message"]["content"] or "").strip()
            return text
        except Exception:
            return ""  # degraded mode: engine lines stand alone
