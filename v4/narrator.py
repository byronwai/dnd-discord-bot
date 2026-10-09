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
                      hints: list = (), npc_knows: list = None,
                      npc_name: str = "") -> str:
        """hints = engine prose skeletons; npc_knows = facts an NPC may
        reveal (None = no NPC dialogue this turn). The LLM's ONLY job is
        atmosphere — never game content, never dice, never outcomes."""
        if not facts:
            return ""
        hint_block = ""
        if hints:
            hint_block = ("\n骨架句（依序作為段落骨架，可潤飾與連接，"
                          "但不得更改其內容）：\n"
                          + "\n".join(f"- {h}" for h in hints))
        npc_block = ""
        if npc_knows is not None and npc_name:
            knows_txt = ("、".join(npc_knows) if npc_knows
                         else "（此NPC沒有可透露的情報）")
            npc_block = (
                f"\n⚠️ NPC 對話規則（{npc_name}）：你只能讓此NPC說出"
                f"以下已知事實：{knows_txt}。\n"
                "絕不得透露不在清單上的資訊（不得給予地點、物品、"
                "攻略建議、劇情推測）；可以閒聊、可以拒絕回答、"
                "可以要求交換條件。\n")
        prompt = (
            "你是地下城主。把下列「骨架句」潤飾成 80~160 字、連貫的"
            "繁體中文敘事段落。\n"
            "鐵律（絕對遵守）：\n"
            "· 骨架句裡的行動者、成敗、對象一律照抄不得更改\n"
            "· 不得新增任何判定、傷害、物品或行動\n"
            "· 不要列出數字算式\n"
            "· 只補感官細節與氛圍\n"
            "· 絕不使用簡體字\n"
            f"{npc_block}"
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
