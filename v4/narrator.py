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

    async def narrate(self, facts: list, scene: str, party_brief: str) -> str:
        if not facts:
            return ""
        prompt = (
            "你是地下城主。根據下列「引擎事實」寫 80~150 字的繁體中文敘事。\n"
            "鐵律：所有數字、成敗、目標一律照事實，不得更改、不得新增任何判定；"
            "絕不替玩家角色行動或說話；不要列出數字算式；結尾可留一個鉤子。\n"
            f"場景：{scene}\n隊伍現況：{party_brief}\n本回合事實：\n"
            + "\n".join(f"- {f}" for f in facts))
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r = await c.post(f"{self.url}/v1/chat/completions", json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7, "max_tokens": 500})
                r.raise_for_status()
                text = (r.json()["choices"][0]["message"]["content"] or "").strip()
            return text
        except Exception:
            return ""  # degraded mode: engine lines stand alone
