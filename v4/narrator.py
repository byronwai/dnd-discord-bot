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
                      npc_name: str = "",
                      extra_directive: str = None,
                      on_delta=None,
                      player_input: str = "") -> str:
        """hints = engine prose skeletons; npc_knows = facts an NPC may
        reveal; extra_directive = turn-level hard rule; on_delta = streaming
        callback; player_input = the player's own words this turn — the
        first sentence of narration must respond to it.
        v3 lesson: most critical rules go at the END (canonical tail)."""
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
        # v3 lesson: the player's own words must be answered, not ignored
        player_block = ""
        if player_input:
            player_block = (
                f"\n玩家這回合做／說了：「{player_input}」\n"
                "第一句必須回應玩家這句話——如果引擎拒絕了，"
                "讓角色大聲說出這句話並讓世界反應"
                "（角色喊「我有千兩黃金」就讓他喊——世界怎樣反應是你的事）。\n")

        # v3 lesson: most critical rules at the END (small models attend
        # most to recent tokens — canonical tail principle)
        tail = (
            f"{player_block}"
            "\n=== 最後指示（最高優先）===\n"
            "· 繁體中文，絕不使用簡體字\n"
            "· 絕不寫出任何骰子數值、算式或判定結果\n"
            "· 絕不給予物品、傷害或經驗（那是引擎的工作）\n")
        if extra_directive:
            tail += f"· {extra_directive}\n"
        prompt = (
            f"場景：{scene}\n隊伍現況：{party_brief}{hint_block}\n"
            "（引擎事實，僅供核對：\n"
            + "\n".join(f"- {f}" for f in facts) + "）\n"
            f"{npc_block}"
            "你是地下城主（DM）。以「骨架句」為基礎，寫 120~200 字的"
            "繁體中文敘事。\n\n"
            "你可以做的事（DM 的創意空間）：\n"
            "· 描述環境變化（光線、聲音、氣味、天氣）\n"
            "· 讓 NPC 做出反應（表情、動作、情緒）\n"
            "· 透露玩家可能發現的細節（腳印、氣味、聲音來源）\n"
            "· 製造小插曲（奇怪的聲響、遠處的影子）\n"
            "· 鋪陳下一步的線索或危機預兆\n"
            "· 讓世界感覺活著、有反應\n\n"
            "你不可以做的事（引擎管轄）：\n"
            "· 決定成敗（骰子結果由引擎決定）\n"
            "· 給予物品或傷害（引擎追蹤）\n"
            "· 替玩家角色行動或說話\n"
            "· 直接傳送角色到新地點\n\n"
            "結尾必須留一個鉤子：未解的疑問、迫近的選擇、或暗示。"
            + tail)
        try:
            if on_delta:
                # streaming: call on_delta as tokens arrive (v3 UX)
                import json as _json
                body = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7, "max_tokens": 400,
                    "stream": True}
                text = ""
                async with httpx.AsyncClient(timeout=180) as c:
                    async with c.stream(
                        "POST", f"{self.url}/v1/chat/completions",
                        json=body
                    ) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                break
                            try:
                                delta = _json.loads(payload)[
                                    "choices"][0]["delta"].get(
                                    "content", "")
                                if delta:
                                    text += delta
                                    try:
                                        on_delta(text)
                                    except Exception:
                                        pass
                            except (KeyError, IndexError,
                                    _json.JSONDecodeError):
                                continue
                return text.strip()
            else:
                # non-streaming (CLI, tests)
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
