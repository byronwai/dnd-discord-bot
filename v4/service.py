"""v4 service: digestor → engine → narrator, with full state persistence.

Hosted inside the dm-bot process (one Discord token = one gateway); the
V4_CHANNEL_ID env routes a dedicated playground channel to this flow
while every other channel keeps the v3 DM.
"""

import json
import os
from dataclasses import asdict

from .cli import META, build_demo_game
from .digestor import Digestor
from .intent import Intent
from .ledger import Ledger, Entry
from .narrator import Narrator
from .rules_core import resolve
from .turn import Game
from .world import Enemy, Scene, World


class V4Service:
    def __init__(self, data_dir: str, llm_url: str,
                 digest_model: str = "gemma3:12b-it-qat",
                 narr_model: str = "gemma3:27b-it-qat"):
        self.path = os.path.join(data_dir, "v4_state.json")
        self.digestor = Digestor(llm_url, digest_model)
        self.narrator = Narrator(llm_url, narr_model)
        self.game: Game = self._load() or build_demo_game()
        self.pending = None  # Intent awaiting the player's 確認

    # ---------- persistence ----------

    def _save(self) -> None:
        g = self.game
        blob = {
            "party": g.party,
            "world": {
                "scenes": {sid: {
                    "name": s.name, "description": s.description,
                    "exits": s.exits, "npcs": s.npcs,
                    "ground_items": [list(x) for x in s.ground_items],
                    "search_dc": s.search_dc,
                    "hidden_items": [list(x) for x in s.hidden_items]}
                    for sid, s in g.world.scenes.items()},
                "current": g.world.current},
            "encounters": {sid: [asdict(f) for f in foes]
                           for sid, foes in g.encounters.items()},
            "enemies": {n: asdict(f) for n, f in g.enemies.items()},
            "combat": {"order": g.combat.order, "idx": g.combat.idx,
                       "round": g.combat.round},
            "inventory": g.inventory,
            "turn": g.ledger.turn,
            "entries": [asdict(e) for e in g.ledger.entries],
        }
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, indent=1)

    def _load(self) -> Game | None:
        if not os.path.exists(self.path):
            return None
        with open(self.path, encoding="utf-8") as f:
            blob = json.load(f)
        world = World()
        for sid, s in blob["world"]["scenes"].items():
            world.scenes[sid] = Scene(
                sid, s["name"], s["description"], exits=s["exits"],
                npcs=s.get("npcs", []),
                ground_items=[tuple(x) for x in s.get("ground_items", [])],
                search_dc=s.get("search_dc", 12),
                hidden_items=[tuple(x) for x in s.get("hidden_items", [])])
        world.current = blob["world"]["current"]
        g = Game(blob["party"], world)
        for sid, foes in blob.get("encounters", {}).items():
            g.encounters[sid] = [Enemy(**f) for f in foes]
        for n, f in blob.get("enemies", {}).items():
            g.enemies[n] = Enemy(**f)
        c = blob.get("combat") or {}
        g.combat.order = c.get("order", [])
        g.combat.idx = c.get("idx", 0)
        g.combat.round = c.get("round", 1)
        g.inventory = blob.get("inventory") or {n: [] for n in g.party}
        g.ledger.turn = blob.get("turn", 0)
        for e in blob.get("entries", []):
            g.ledger.entries.append(Entry(**e))
        return g

    # ---------- admin helpers ----------

    def admin_give(self, char: str, item: str, qty: int = 1) -> str:
        """Admin grants items directly (ledgered). Returns the render."""
        g = self.game
        if char not in g.party:
            return f"❓ 沒有角色「{char}」"
        g.give_item(char, item, max(1, int(qty or 1)))
        g.ledger.add("admin", "item", f"管理員給了 {char} {item}×{qty}")
        self._save()
        inv = "、".join(f"{n}×{q}" for n, q in g.inventory.get(char, []))
        return f"🎒 {char} 現在攜帶：{inv or '（空）'}"

    # ---------- one turn ----------

    async def handle(self, text: str, author: str = "") -> tuple[list, str]:
        """Free-form text in → (engine lines, narration). Never raises."""
        g = self.game
        t = (text or "").strip()
        if not t:
            return [], ""
        if t in META:  # deterministic queries skip the LLM entirely
            return META[t](g), ""
        it = None
        if self.pending is not None:
            low = t.lower().strip(" ！!。.")
            if low in ("確認", "确认", "confirm", "對", "对", "yes", "y",
                       "好", "ok"):
                it = self.pending
                it.args["confirmed"] = True
            self.pending = None  # anything else = changed their mind
        if it is None:
            it = await self.digestor.digest(
                t, list(g.party), g.world.here.name,
                list(g.world.here.exits.values()),
                known_targets=list(g.enemies) +
                [n["name"] for n in g.world.here.npcs] + list(g.party))
        idx0 = len(g.ledger.entries)  # this turn's slice of the ledger
        r = resolve(g, it)
        if r.confirm is not None:
            self.pending = r.confirm
        narration = ""
        if r.accepted and r.lines and self.pending is None:
            from .templates import render_hint
            hints = [h for h in (render_hint(e, g)
                                 for e in g.ledger.entries[idx0:]) if h]
            facts = [e.text for e in g.ledger.entries[idx0:]]
            brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                             for n, e in g.party.items())
            narration = await self.narrator.narrate(
                facts, g.world.here.name, brief, hints=hints)
            if not narration and hints:
                narration = "\n".join(hints)  # degraded: skeleton IS prose
        self._save()
        return r.lines, narration
