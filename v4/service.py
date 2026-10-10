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
                 narr_model: str = "gemma3:27b-it-qat",
                 channel_id: str = ""):
        # per-channel state: each Discord table gets its own game
        fname = f"v4_state_{channel_id}.json" if channel_id else "v4_state.json"
        self.path = os.path.join(data_dir, fname)
        self.channel_id = channel_id
        self.digestor = Digestor(llm_url, digest_model)
        self.narrator = Narrator(llm_url, narr_model)
        self.game: Game = self._load() or build_demo_game()
        self.pending = None  # Intent awaiting the player's /confirm
        self._recent_narrations: list[str] = []  # v3: repetition guard

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

    def table_talk(self, text: str, author: str = "") -> None:
        """Plain channel chatter: ignore completely. No ledger, no save."""
        pass

    # ---------- one turn ----------

    async def handle(self, text: str, author: str = "",
                     user_id: str = "",
                     structured: bool = False,
                     on_delta=None) -> tuple[list, str]:
        """Free-form text in → (engine lines, narration). Never raises.
        user_id: Discord uid — the engine enforces that only the character's
        owner can act as that character.
        structured: True = the text came from /combat (dropdown selections
        are already unambiguous) — skip the digestor entirely, construct
        the Intent deterministically, confidence=1.0, no confirmation."""
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
        # scrub input BEFORE the digestor sees it (fix: was after)
        from .guards import scrub_input
        clean_t, was_scrubbed = scrub_input(t)
        if was_scrubbed:
            g.ledger.add(author or "?", "deny",
                         "已過濾可疑指令文字", reason="injection")
        t = clean_t
        if t.startswith(("((", "//")):  # explicit out-of-character
            from .intent import Intent
            it = Intent(action="chat", utterance=t.lstrip("(/ "), raw=t)
        elif structured:
            # /combat selections: deterministic parse, confidence=1.0
            from .intent import parse_command
            it = parse_command(t, party_names=list(g.party))
            if it is not None:
                it.confidence = 1.0
            else:
                it = await self.digestor.digest(
                    t, list(g.party), g.world.here.name,
                    list(g.world.here.exits.values()),
                    known_targets=list(g.enemies) +
                    [n["name"] for n in g.world.here.npcs] + list(g.party))
                it.confidence = 1.0  # structured origin — trust it
        else:
            it = await self.digestor.digest(
                    t, list(g.party), g.world.here.name,
                    list(g.world.here.exits.values()),
                    known_targets=list(g.enemies) +
                    [n["name"] for n in g.world.here.npcs] + list(g.party))
        # ownership: only the character's owner may act as that character
        if user_id and it:
            # no actor specified → default to the CALLER's character
            if not it.actor:
                it.actor = next(
                    (n for n, v in g.party.items()
                     if str(v.get("owner_id", "")) == str(user_id)),
                    next(iter(g.party), None))
                if it.actor is None:
                    return (["❓ 還沒有角色——先用 `/pc name:名字 "
                             "occupation:職業` 建立角色。"], "")
            if it.actor and it.actor in g.party:
                oid = str(g.party[it.actor].get("owner_id", ""))
                if oid and oid != str(user_id):
                    g.ledger.add(it.actor, "deny",
                                 f"拒絕：{it.actor} 屬於其他玩家",
                                 reason="ownership")
                    return ([f"🚫 {it.actor} 屬於其他玩家——"
                             "你不能控制這個角色。"], "")
        idx0 = len(g.ledger.entries)  # this turn's slice of the ledger
        r = resolve(g, it)
        if r.confirm is not None:
            self.pending = r.confirm
        narration = ""
        if r.accepted and r.lines and self.pending is None:
            from .templates import render_hint
            from .guards import (make_placeholder_map, make_restore_map,
                                 map_out, map_in, scrub_narration,
                                 is_repetition, is_chinese)
            hints = [h for h in (render_hint(e, g)
                                 for e in g.ledger.entries[idx0:]) if h]
            facts = [e.text for e in g.ledger.entries[idx0:]
                     if e.kind != "table"]
            brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                             for n, e in g.party.items())
            # v3 lesson: the narrator must answer the player's own words,
            # especially when the engine denied something
            player_input = it.raw or it.utterance or ""
            if not player_input:
                player_input = text[:120]
            # pass NPC knowledge constraints if this was a talk turn
            npc_knows, npc_name = None, ""
            for e in g.ledger.entries[idx0:]:
                if e.kind in ("talk", "insight") and e.data.get("npc"):
                    npc_name = e.data["npc"]
                    if e.kind == "insight" and e.data.get("knows"):
                        # the engine already picked what insight reveals
                        npc_knows = e.data["knows"]
                        break
                    for n in g.world.here.npcs:
                        if n["name"] == npc_name:
                            npc_knows = n.get("knows", [])
                            break
            # v3 guard: placeholder names — narrator never sees real names
            pmap = make_placeholder_map(list(g.party))
            rmap = make_restore_map(list(g.party))
            safe_hints = [map_out(h, pmap) for h in hints]
            safe_facts = [map_out(f, pmap) for f in facts]
            safe_brief = map_out(brief, pmap)
            safe_knows = ([map_out(k, pmap) for k in npc_knows]
                          if npc_knows else npc_knows)
            # v3 guard: repetition — if the last narrations were near-
            # identical, inject a hard break directive
            rep_hint = None
            if is_repetition(" ".join(safe_hints), self._recent_narrations):
                rep_hint = ("⚠️ 你最近的敘述幾乎相同——這次必須完全不同。"
                            "換一個場景細節、感官或節奏。")
            # item inventory: what the narrator may mention
            s = g.world.here
            scene_items = ([n for n, _ in s.ground_items]
                           + [n for n, _ in s.hidden_items])
            party_inv = {ch: [n for n, _ in stacks]
                         for ch, stacks in g.inventory.items()}
            narration = await self.narrator.narrate(
                safe_facts, g.world.here.name, safe_brief, hints=safe_hints,
                npc_knows=safe_knows, npc_name=npc_name,
                extra_directive=rep_hint, on_delta=on_delta,
                player_input=map_out(player_input, pmap),
                scene_items=scene_items, party_inventory=party_inv)
            # force Traditional Chinese (models skew Simplified)
            try:
                from opencc import OpenCC
                narration = OpenCC("s2t").convert(narration)
            except ImportError:
                pass
            # v3 guard: scrub fake dice/verdicts from narration
            narration, was_scrubbed_n = scrub_narration(narration)
            # item guard: bolded items must exist in the inventory
            from .guards import validate_narration_items
            all_known = scene_items + [i for items in party_inv.values()
                                       for i in items]
            narration, found_items = validate_narration_items(
                narration, all_known)
            for fi in found_items:
                if fi not in scene_items:
                    scene_items.append(fi)
            if was_scrubbed_n:
                g.ledger.add("engine", "guard",
                             "已從敘事中清除假骰/判定文字")
            # v3 guard: language check — if still not Chinese, degrade
            if narration and not is_chinese(narration):
                narration = ""  # reject non-Chinese output
            # v3 guard: restore real names before showing to players
            narration = map_in(narration, rmap)
            if not narration and hints:
                narration = map_in("\n".join(hints), rmap)  # degraded
            # track for repetition guard
            if narration:
                self._recent_narrations.append(narration)
                self._recent_narrations = self._recent_narrations[-5:]
        self._save()
        # v3 lesson: every turn ends with a hook — scene, status, options
        if r.accepted and r.lines:
            from .templates import render_turn_context
            context = render_turn_context(g, it.actor or "")
            r.lines.append("")
            r.lines.append(context)
        return r.lines, narration
