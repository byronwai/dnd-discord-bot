"""State tags: machine-applied game state changes embedded in DM narration.

Like the [[d20]] dice markers, the DM emits tags on their own lines and the
bot applies them to the database for real:

    [[hp:大力蕉:-4]]        damage / healing (clamped to 0..max)
    [[hp:大力蕉=12/12]]     set absolute current/max
    [[item:依思:+治療藥水 x1]]  item gained
    [[item:依思:-torch x2]] item lost / consumed
    [[scene:霧錨鎮·碼頭]]    party moved to a new location
    [[objective:找出漁夫]]  current goal changed

Unknown names are fuzzy-matched against the party; unparseable tags are left
untouched in the text.
"""

import re

from . import checks as checks_mod

_HP_RE = re.compile(
    r"\[{2}\s*hp\s*[:：]\s*([^:=\]/]+?)\s*[:=]\s*([+-]?\d{1,3})\s*(?:/\s*(\d{1,3}))?\s*\]{2}",
    re.IGNORECASE)
_ITEM_RE = re.compile(r"\[{2}\s*item:([^:=\]]+?)\s*:\s*([+-])\s*([^\]x×]+?)\s*(?:[x×]\s*(\d{1,3}))?\s*\]{2}", re.IGNORECASE)
_XP_RE = re.compile(r"\[{2}\s*xp:([^:\]]+?)\s*:\s*(\d{1,6})\s*\]{2}", re.IGNORECASE)
_SCENE_RE = re.compile(r"\[{2}\s*scene:([^\]]+?)\s*\]{2}", re.IGNORECASE)
_OBJ_RE = re.compile(r"\[{2}\s*objective:([^\]]+?)\s*\]{2}", re.IGNORECASE)
_COMBAT_RE = re.compile(r"\[{2}\s*combat\s*[:：]\s*([^\]]+?)\s*\]{2}", re.IGNORECASE)
_ATTACK_RE = re.compile(
    r"\[{2}\s*attack\s*[:：]\s*([^|｜:\]]+?)\s*[|｜]\s*([^|｜\]]+?)"
    r"(?:\s*[|｜]\s*([+-]?\s*[A-Za-z\u4e00-\u9fff+0-9]{1,8}))?\s*\]{2}",
    re.IGNORECASE)
_ENEMY_RE = re.compile(
    r"\*{0,2}[ \t]*\[{1,2}[ \t]*enemy[ \t]*[:：][ \t]*([^|｜\]]+?)"
    r"(?:[ \t]*[|｜][ \t]*HP[ \t]*([+-]?\d{1,3}))?"
    r"(?:[ \t]*[|｜][ \t]*AC[ \t]*([+-]?\d{1,2}))?"
    r"(?:[ \t]*[,，][ \t]*([^\]]*?))?[ \t]*\]{1,2}[ \t]*\*{0,2}",
    re.IGNORECASE)
_SPELL_RE = re.compile(
    r"\[{2}\s*spell\s*[:：]\s*([^:：\]]+?)\s*[:：]\s*(\d)\s*\]{2}", re.IGNORECASE)
_REST_RE = re.compile(
    r"\[{2}\s*rest\s*[:：]\s*(short|long|短休|長休|长休)\s*\]{2}", re.IGNORECASE)
_NPC_RE = re.compile(
    r"\*{0,2}[ \t]*\[{1,2}[ \t]*npc[ \t]*[:：][ \t]*([^|｜\]]+?)"
    r"(?:[ \t]*[|｜][ \t]*([^|｜\]]*?))?(?:[ \t]*[|｜][ \t]*([^|｜\]]*?))?[ \t]*\]{1,2}[ \t]*\*{0,2}",
    re.IGNORECASE)
_LORE_RE = re.compile(
    r"\*{0,2}[ \t]*\[{1,2}[ \t]*lore[ \t]*[:：][ \t]*([^|｜\]]+?)[ \t]*[|｜][ \t]*([^\]]+?)[ \t]*\]{1,2}[ \t]*\*{0,2}",
    re.IGNORECASE)


def _match_char(name: str, party: dict) -> str | None:
    name = name.strip()
    if name in party:
        return name
    low = name.lower().replace("-", " ").replace("_", " ")
    # registered aliases first (handles anglicized names like Elyse -> 依思)
    for cand, val in party.items():
        for alias in ((val.get("aliases") or []) if isinstance(val, dict) else []):
            if alias.lower().replace("-", " ").replace("_", " ") == low:
                return cand
    for cand in party:
        c = cand.lower()
        if c == low or low in c or c in low:
            return cand
    # token overlap for compound aliases ("banana strong" vs "大力蕉" won't hit,
    # but "banana-strong banana" style junk still resolves via alias list)
    return None


def apply_state_tags(text: str, engine, platform: str, chat_id: str,
                    actor: str = "",
                    defer_party_damage: bool = False) -> tuple[str, list[str]]:
    """Apply every recognized tag; returns (rendered_text, change_log_lines)."""
    log: list[str] = []
    party = engine.get_party(platform, chat_id)

    def resolve_char(name: str) -> str | None:
        """Match a tag's character name; fall back to the current actor."""
        m = _match_char(name, party)
        if m is None and actor and actor in party:
            return actor  # DM renamed the character; the actor is who it means
        return m

    def _enemy_hp(slot: dict, st: dict, m: re.Match) -> str:
        """Apply an [[hp:enemy:...]] tag to a combat slot; death at 0, and
        combat auto-ends when every enemy slot is down. An unregistered slot
        (no [[enemy:...|HP|AC]] yet) gets SRD-ish defaults instead of
        dying to its first point of damage."""
        raw, maxv = m.group(2), m.group(3)
        if "hp" not in slot and maxv is None:
            slot["hp"], slot["hp_max"] = 11, 11
        if maxv is not None:
            slot["hp"] = max(0, min(int(maxv), abs(int(raw))))
            slot["hp_max"] = int(maxv)
        else:
            slot["hp"] = max(0, slot.get("hp", 0) + int(raw))
        out = f"**🗡 {slot['name']} HP {slot['hp']}/{slot.get('hp_max', '?')}**"
        if slot["hp"] <= 0:
            slot["dead"] = True
            out = f"**💀 {slot['name']} 倒下！**"
        engine._combat_save(platform, chat_id, st)
        npcs = [o for o in st.get("order", []) if o.get("npc")]
        if npcs and all(o.get("hp", 1) <= 0 or o.get("dead") for o in npcs):
            engine.combat_end(platform, chat_id)
            out += "\n**🏁 戰鬥結束——敵人全滅！/ All enemies down.**"
            log.append("COMBAT AUTO-END")
        log.append(out.splitlines()[0])
        return out

    # ---- check/save request: [[check:DC|char|ABILITY]] / [[save:DC|char|AB]] ----
    def _check(m: re.Match) -> str:
        kind = (m.group(1) or "check").lower()
        dc = int(m.group(2))
        char = resolve_char(m.group(3).strip())
        if char is None and len(party) <= 1:
            char = next(iter(party))  # single-member party: map any spelling
        if char is None:
            return m.group(0)
        ability, skill = checks_mod.parse_check_ability(m.group(4))
        engine.set_pending_check(platform, chat_id,
                                 {"dc": dc, "char": char, "ability": ability,
                                  "skill": skill, "kind": kind})
        # show the d20 target: need >= dc - modifier (party sheet + proficiency)
        entry = party.get(char, {})
        mod = checks_mod.total_mod(entry, ability, skill, kind)
        need = max(1, dc - mod)
        card = checks_mod.render_card(dc, char, ability, mod=mod, need=need,
                                      kind=kind, skill=skill)
        log.append(card.splitlines()[0])
        return card

    text = checks_mod.CHECK_RE.sub(_check, text)

    # ---- combat FIRST: [[combat:...]] may arrive in the same reply as
    # [[enemy:]]/[[attack:]] tags, and those need the slots it creates ----
    def _combat(m: re.Match) -> str:
        val = m.group(1).strip()
        if val.lower() in ("end", "結束", "结束", "stop", "over"):
            ended = engine.combat_end(platform, chat_id)
            if ended:
                log.append("COMBAT END")
                return "**🏁 戰鬥結束！/ Combat over.**"
            return m.group(0)
        order = engine.combat_auto_start(platform, chat_id, val)
        if not order:
            return m.group(0)  # no party yet — leave the tag for the DM to see
        lines = ["**⚔️ 戰鬥開始！Round 1 — 先攻順序 / Initiative:**"]
        for i, o in enumerate(order, 1):
            tag = "（敵）" if o.get("npc") else ""
            lines.append(f"{i}. {o['name']}{tag} — 先攻 {o['init']}")
        first_pc = next((o["name"] for o in order if not o.get("npc")), None)
        if first_pc:
            lines.append(f"▶️ 首位玩家行動：**{first_pc}** — 直接描述你的行動"
                         "（或 `/act`）；**攻擊請用 `/attack <目標>`**，"
                         "系統直接擲骰判定！敵人回合會併入下一位玩家的敘述。")
        log.append("COMBAT START")
        return "\n".join(lines)

    text = _COMBAT_RE.sub(_combat, text)

    # ---- enemy registration: [[enemy:哥布林①|HP 7|AC 15]] (+ optional
    # trailing condition like ", 束縛"; HP <= 0 means the model is re-asserting
    # a DEAD enemy — clamp to 0 and run the all-dead check) ----
    def _enemy(m: re.Match) -> str:
        name = m.group(1).strip()
        raw_hp = int(m.group(2)) if m.group(2) else None
        ac = int(m.group(3)) if m.group(3) else 0
        cond = (m.group(4) or "").strip()[:30]
        dead_arrival = raw_hp is not None and raw_hp <= 0
        hp = max(0, raw_hp) if raw_hp is not None else 0
        # difficulty tiers shape enemy stats for real: 新手 trims AC/HP,
        # 困難 buffs them — beginner-friendly is mechanical, not prose
        adj = ""
        if raw_hp is not None and raw_hp > 0:
            diff = engine.get_difficulty(platform, chat_id)
            if diff == "easy":
                hp = max(1, round(raw_hp * 0.75))
                if ac:
                    ac = max(10, ac - 1)
                adj = "（新手難度已調低）"
            elif diff == "hard":
                hp = round(raw_hp * 1.25)
                if ac:
                    ac += 1
                adj = "（困難模式已調高）"
        slot, st = engine.combat_find(platform, chat_id, name)
        if slot is None:
            if dead_arrival:
                # re-asserting a corpse with no active combat: nothing to
                # track — drop the syntax instead of leaking it
                log.append(f"ENEMY {name} dead-reassert, no combat")
                return ""
            # no active combat: the enemy's appearance IS combat starting —
            # auto-open one so its HP can be tracked ([[hp:X:-n]] lands)
            party = engine.get_party(platform, chat_id)
            if not any(isinstance(v, dict) for v in party.values()):
                return m.group(0)
            engine.combat_auto_start(platform, chat_id, name)
            slot, st = engine.combat_find(platform, chat_id, name)
            if slot is None:
                return m.group(0)
            log.append("COMBAT START (auto, enemy tag)")
        if raw_hp is not None:
            slot["hp"], slot["hp_max"] = hp, max(hp, slot.get("hp_max", hp))
        if ac:
            slot["ac"] = ac
        if cond:
            slot["cond"] = cond
        engine._combat_save(platform, chat_id, st)
        log.append(f"ENEMY {name}")
        if dead_arrival or slot.get("hp", 1) <= 0:
            slot["dead"] = True
            slot["hp"] = 0
            engine._combat_save(platform, chat_id, st)
            out = f"**💀 {name} 倒下！（HP 歸零）**"
            npcs = [o for o in st.get("order", []) if o.get("npc")]
            if npcs and all(o.get("hp", 1) <= 0 or o.get("dead")
                            for o in npcs):
                engine.combat_end(platform, chat_id)
                out += "\n**🏁 戰鬥結束——敵人全滅！/ All enemies down.**"
                log.append("COMBAT AUTO-END")
            return out
        bits = [f"HP {slot['hp']}/{slot['hp_max']}"] if raw_hp is not None else []
        bits += [f"AC {ac}"] if ac else []
        bits += [cond] if cond else []
        bits += [adj] if adj else []
        return f"**🧟 {name} 登場：{' · '.join(bits)}**"

    text = _ENEMY_RE.sub(_enemy, text)

    # ---- attack roll: [[attack:attacker|target|+5]] → consent card, then the
    # server rolls vs the target's AC (same rhythm as checks so the DM never
    # narrates an outcome before the roll exists) ----
    def _attack(m: re.Match) -> str:
        atk_name = m.group(1).strip()
        tgt_raw = m.group(2).strip()
        spec = (m.group(3) or "").replace(" ", "")
        attacker = resolve_char(atk_name)
        # target AC: literal in tag > enemy slot > party entry > default 13
        ac_m = re.search(r"AC\s*(\d{1,2})$", tgt_raw, re.IGNORECASE)
        tgt_name = tgt_raw[:ac_m.start()].strip() if ac_m else tgt_raw
        ac = int(ac_m.group(1)) if ac_m else None
        # target: NO actor-fallback (fallback would make PCs attack themselves
        # when the DM misspells a goblin) — exact party match or enemy slot
        tgt_char = _match_char(tgt_name, party)
        tgt_slot = None
        if tgt_char is None:
            tgt_slot, _st = engine.combat_find(platform, chat_id, tgt_name)
        if ac is None and tgt_slot is not None:
            ac = tgt_slot.get("ac")
        if ac is None and tgt_char is not None:
            from . import charlib
            entry = party.get(tgt_char, {})
            try:
                ac = int(entry.get("ac") or
                         charlib.default_ac(entry.get("occupation", ""),
                                            entry.get("stats") or {}))
            except (TypeError, ValueError):
                ac = 13
        if ac is None:
            ac = 13  # unregistered target: typical SRD commoner/brigand AC
        # attack bonus: literal > ability+PB (party) > default +4 (enemy)
        bonus = None
        if spec.startswith(("+", "-")) and spec[1:].isdigit():
            bonus = int(spec)
        elif attacker is not None:
            ab = checks_mod.normalize_ability(spec)
            if ab:
                bonus = checks_mod.total_mod(party.get(attacker, {}), ab,
                                             kind="attack")
        if bonus is None:
            bonus = 4
        if attacker is None:
            attacker = atk_name  # enemy attacker: keep the name it was given
        engine.set_pending_check(platform, chat_id, {
            "kind": "attack", "char": attacker, "bonus": bonus,
            "target": tgt_name, "ac": ac, "dc": ac})
        need = max(1, ac - bonus)
        log.append(f"ATTACK {attacker}->{tgt_name} AC{ac}")
        return (f"⚔️ **攻擊要求 Attack**: {attacker} {'%+d' % bonus} vs "
                f"**{tgt_name}** AC **{ac}**（需骰 ≥ **{need}**）\n"
                f"👉 **{attacker} 的玩家，回覆「骰」或 `roll`**（或之後直接用 "
                f"`/attack {tgt_name}` 發起，系統立即擲骰）")

    text = _ATTACK_RE.sub(_attack, text)

    # ---- anti-vibe: the model sometimes HAND-WRITES a card instead of
    # emitting [[attack:...]] (mangled emoji/bold/mixed wording = telltale).
    # Convert that fake card into a REAL pending attack so the player's
    #「骰」truly rolls. Matches 攻擊要求/判定/檢定 × Attack/Check wordings.
    _FAKE_CARD_RE = re.compile(
        r"[🎲⚔️🎯]?\s*\**\s*(?:攻擊要求|攻擊判定|攻擊檢定)\s*"
        r"(?:Attack|Check)?\**\s*[:：]\s*([^\s*+，。]+?)\s*"
        r"([+-]\d{1,2})\s*vs\s*\**\s*([^*\n]+?)\s*\**\s*AC\s*\**\s*(\d{1,2})",
        re.IGNORECASE)

    def _fake_card(m: re.Match) -> str:
        if any(l.startswith("ATTACK") for l in log):
            return m.group(0)  # a real [[attack]] card already rendered
        attacker = resolve_char(m.group(1).strip()) or m.group(1).strip()
        bonus = int(m.group(2))
        tgt = m.group(3).strip()
        ac = int(m.group(4))
        # a party character "attacking itself" is almost certainly the
        # converter misreading a combat-status restatement — never mint a
        # phantom pending card from it
        if attacker in party and _match_char(tgt, party) == attacker:
            return m.group(0)
        engine.set_pending_check(platform, chat_id, {
            "kind": "attack", "char": attacker, "bonus": bonus,
            "target": tgt, "ac": ac, "dc": ac})
        need = max(1, ac - bonus)  # engine math (the model often echoes AC)
        log.append(f"ATTACK {attacker}->{tgt} AC{ac} (converted fake card)")
        return (f"⚔️ **攻擊要求 Attack**: {attacker} {'%+d' % bonus} vs "
                f"**{tgt}** AC **{ac}**（需骰 ≥ **{need}**）\n"
                f"👉 **{attacker} 的玩家，回覆「骰」或 `roll`**（或之後直接用 "
                f"`/attack {tgt}` 發起，系統立即擲骰。先前自行宣布的骰值與命中無效）")

    text = _FAKE_CARD_RE.sub(_fake_card, text)

    # ---- anti-vibe: the model HAND-WRITES a check card WITH a result
    # ("🎲 🎯 **檢定要求 Check**: 依思 CHA+2 vs **DC 20** → **15** (失敗)")
    # — the roll is fabricated. Convert into a REAL pending check: the
    # engine recomputes the modifier and the self-announced result is void.
    # Separators tolerate spaces AND markdown bold (**) anywhere — the
    # model's formatting drifts, the pattern must not care. ----
    _FAKE_CHECK_RE = re.compile(
        r"(?:[🎲🎯]\s*){0,3}\**\s*檢定要求\s*(?:Check)?\s*\**\s*[：:][\s*]*"
        r"([^\s:：+（()]{1,24}?)[\s*]*"
        r"(STR|DEX|CON|INT|WIS|CHA|力量|敏捷|體質|智力|感知|魅力)?[\s*]*"
        r"[+-]?\d{0,2}[\s*]*vs[\s*]*D?C?[\s*]*(\d{1,2})[\s*]*"
        r"(?:→|->|=)[\s*]*\**\s*(\d{1,2})\**\s*[（(]?[\s*]*(成功|失敗)",
        re.IGNORECASE)

    def _fake_check(m: re.Match) -> str:
        if any(l.startswith(("CHECK", "🛡")) for l in log):
            return m.group(0)  # a real [[check]]/[[save]] card already rendered
        char = resolve_char(m.group(1).strip())
        if char is None and len(party) <= 1:
            char = next(iter(party), None)
        if char is None:
            return ""
        ability, skill = checks_mod.parse_check_ability(m.group(2) or "")
        dc = int(m.group(3))
        engine.set_pending_check(platform, chat_id,
                                 {"dc": dc, "char": char, "ability": ability,
                                  "skill": skill, "kind": "check"})
        entry = party.get(char, {})
        mod = checks_mod.total_mod(entry, ability, skill, "check")
        need = max(1, dc - mod)
        card = checks_mod.render_card(dc, char, ability, mod=mod, need=need,
                                      kind="check", skill=skill)
        log.append(f"CHECK {char} fake-card converted (self-rolled result void)")
        return (card + "\n（⚠️ 系統偵測到 DM 自行宣布骰值——該結果**無效**，"
                "已轉為正式檢定卡，請該角色的玩家重新擲骰。）")

    text = _FAKE_CHECK_RE.sub(_fake_check, text)

    # ---- spell slots: [[spell:char:1]] (0 = cantrip, free) ----
    def _spell(m: re.Match) -> str:
        char = resolve_char(m.group(1).strip())
        lvl = int(m.group(2))
        if char is None:
            return m.group(0)
        entry = party[char]
        if lvl <= 0:
            log.append(f"SPELL {char} cantrip")
            return f"**✨ {char} 施展戲法（不消耗法術格）**"
        cur, mx = engine.char_slots(entry, char)
        key = str(lvl)
        have = int(cur.get(key, 0))
        if have <= 0:
            log.append(f"SPELL {char} L{lvl} DENIED")
            return (f"**⚠️ {char} 的 {lvl} 環法術格已用盡（{key}環 0/{mx.get(key, 0)}）"
                    "——施展失敗，改用低環法術、戲法或先休息！**")
        cur[key] = have - 1
        engine.set_party(platform, chat_id, party)
        log.append(f"SPELL {char} L{lvl}")
        return (f"**✨ {char} 施展 {lvl} 環法術（L{lvl} 剩 {cur[key]}/"
                f"{mx.get(key, 0)}）**")

    text = _SPELL_RE.sub(_spell, text)

    # ---- rests: [[rest:short]] / [[rest:long]] ----
    def _rest(m: re.Match) -> str:
        word = m.group(1).lower()
        kind = "long" if word in ("long", "長休", "长休") else "short"
        out = engine.do_rest(platform, chat_id, kind)
        log.append(f"REST {kind}")
        return out

    text = _REST_RE.sub(_rest, text)

    # ---- DM notebook: [[npc:名字|身分|狀態]] / [[lore:主題|事實]] ----
    _STATUS_WORDS = {"dead", "alive", "friendly", "hostile", "neutral",
                     "missing", "captured", "ally", "enemy", "suspicious",
                     "死亡", "死了", "存活", "友好", "敵對", "中立", "失蹤",
                     "被俘", "盟友", "敵人", "起疑"}

    def _npc(m: re.Match) -> str:
        name = m.group(1).strip()[:40]
        f2 = (m.group(2) or "").strip()[:80]
        f3 = (m.group(3) or "").strip()[:30]
        # two-field form: a lone status word ("[[npc:瑪莎|dead]]") updates
        # the status, anything else is the description
        desc, status = f2, f3
        if not f3 and f2.lower() in _STATUS_WORDS:
            desc, status = "", f2
        if not engine.npc_upsert(platform, chat_id, name, desc, status):
            return ""  # unchanged note: stay silent (anti-spam)
        log.append(f"NPC {name}")
        entry = next((n for n in engine.npc_list(platform, chat_id, limit=50)
                      if n["name"] == name), {"desc": desc, "status": status})
        bits = [b for b in (entry["desc"], entry["status"]) if b]
        return f"**📝 NPC 筆記：{name}" + \
               (f" — {'；'.join(bits)}**" if bits else " 已記錄**")

    text = _NPC_RE.sub(_npc, text)

    def _lore(m: re.Match) -> str:
        key = m.group(1).strip()[:40]
        fact = m.group(2).strip()[:160]
        if not engine.lore_upsert(platform, chat_id, key, fact):
            return ""
        log.append(f"LORE {key}")
        return f"**📜 世界情報：{key} — {fact}**"

    text = _LORE_RE.sub(_lore, text)

    # ---- HP: [[hp:Name:±N]] [[hp:Name=±N]] delta; [[hp:Name=C/M]] absolute ----
    def _hp(m: re.Match) -> str:
        raw_name = m.group(1).strip()
        # enemy slots take priority over the actor-fallback: [[hp:哥布林①:-4]]
        # must never be routed to the acting PC just because the name isn't
        # in the party (unregistered slots get defaults inside _enemy_hp)
        slot, st = engine.combat_find(platform, chat_id, raw_name)
        if slot is not None:
            return _enemy_hp(slot, st, m)
        char = resolve_char(raw_name)
        if char is None:
            log.append(f"UNRESOLVED hp tag: {m.group(1).strip()}")
            return ""  # unknown target: drop the syntax, prose still stands
        entry = party[char]
        if not isinstance(entry, dict):
            return m.group(0)
        # lazily initialize HP from stats if the entry predates HP tracking
        if "hp_max" not in entry or "hp_now" not in entry:
            try:
                from .commands import starting_hp
                hp = starting_hp(entry.get("occupation", "Fighter"),
                                 entry.get("stats", {}))
            except Exception:
                hp = 10
            entry.setdefault("hp_max", hp)
            entry.setdefault("hp_now", hp)
        raw, maxv = m.group(2), m.group(3)
        if maxv is not None:  # absolute current/max
            val = abs(int(raw))
            mx = max(1, int(maxv))
            cur = max(0, min(mx, val))
            # no-op absolute set (e.g. [[hp:X=10/10]] at full HP): drop
            # silently — rendering it teaches the model to re-emit the tag
            # every turn and the chat fills with ❤️ spam
            if cur == entry.get("hp_now") and mx == entry.get("hp_max"):
                return ""
            entry["hp_now"], entry["hp_max"] = cur, mx
            engine.set_party(platform, chat_id, party)
            log.append(f"❤️ {char} HP = {cur}/{mx}")
            return f"**❤️ {char} HP {cur}/{mx}**"
        delta = int(raw)  # signed: -4 damage, +5 healing, 4 → treat as +4
        if defer_party_damage and delta < 0:
            # a player's /attack turn: un-narrated enemy retaliation is not
            # applied here — it is queued and MUST be narrated next turn
            engine.queue_deferred(platform, chat_id, char, delta)
            log.append(f"DEFERRED {char} {delta}")
            return (f"**⏳ 敵方反擊（延後結算）：{char} 將受到 {-delta} 點傷害"
                    "——系統將於下回合開頭結算並要求補述攻擊過程。**")
        now = max(0, min(entry["hp_max"], entry["hp_now"] + delta))
        applied = now - entry["hp_now"]
        if applied == 0:
            return ""  # zero net change: drop silently (anti-spam)
        entry["hp_now"] = now
        engine.set_party(platform, chat_id, party)
        icon = "💔" if applied < 0 else ("💚" if applied > 0 else "❤️")
        log.append(f"{icon} {char} {applied:+d} HP → {now}/{entry['hp_max']}"
                   + (" ⚠️ **死亡豁免！**" if now == 0 else ""))
        return (f"**{icon} {char} {applied:+d} HP → {now}/{entry['hp_max']}**"
                + (" ⚠️ 死亡豁免！" if now == 0 else ""))

    text = _HP_RE.sub(_hp, text)

    # ---- items: [[item:Name:+item x2]] / [[item:Name:-item]] ----
    def _item(m: re.Match) -> str:
        char = resolve_char(m.group(1))
        if char is None:
            return m.group(0)
        sign, name, qty = m.group(2), m.group(3).strip(), int(m.group(4) or 1)
        if sign == "+":
            engine.inv_add(platform, chat_id, char, name, qty)
            log.append(f"🎒 {char} +{name}×{qty}")
            return f"**🎒 {char} 獲得 {name}×{qty}**"
        ok, msg = engine.inv_remove(platform, chat_id, char, name, qty)
        if not ok:
            return m.group(0)
        log.append(f"🎒 {char} -{name}×{qty}")
        return f"**🎒 {char} 失去 {name}×{qty}**"

    text = _ITEM_RE.sub(_item, text)

    # ---- XP: [[xp:Name:100]] → level-up handled deterministically ----
    def _xp(m: re.Match) -> str:
        from .commands import HIT_DICE, level_for, starting_hp
        char = resolve_char(m.group(1))
        if char is None:
            return m.group(0)
        entry = party[char]
        if not isinstance(entry, dict):
            return m.group(0)
        gain = int(m.group(2))
        old_lvl = level_for(int(entry.get("xp", 0)))
        entry["xp"] = int(entry.get("xp", 0)) + gain
        new_lvl = level_for(entry["xp"])
        lines_log = []
        if new_lvl > old_lvl:
            die = HIT_DICE.get(entry.get("occupation", ""), 8)
            con = int((entry.get("stats") or {}).get("CON", 10))
            bump = die // 2 + 1 + (con - 10) // 2
            bump = max(1, bump)
            entry["hp_max"] = int(entry.get("hp_max", bump)) + bump
            entry["hp_now"] = min(entry["hp_max"],
                                  int(entry.get("hp_now", 0)) + bump)
            entry["level"] = new_lvl
            from . import charlib
            engine.char_slots(entry, "")  # ensures slots exist
            cur, mx = entry.get("slots"), charlib.slots_for(
                entry.get("occupation", ""), new_lvl)
            for k, v in mx.items():  # newly gained slots arrive filled
                cur[str(k)] = max(int(cur.get(str(k), 0)), v)
            pb = charlib.prof_bonus(new_lvl)
            engine.set_party(platform, chat_id, party)
            slot_txt = ("，法術格提升" if mx else "")
            log.append(f"LEVELUP {char} -> Lv{new_lvl} (+{bump} HP, PB {pb:+d})")
            return (f"**✨ {char} 獲得 {gain} XP — 升級！"
                    f"Lv{new_lvl}，HP +{bump}（{entry['hp_now']}/"
                    f"{entry['hp_max']}），熟練加值 PB {pb:+d}{slot_txt}"
                    "（新特性由 DM 依 SRD 敘述）**")
        engine.set_party(platform, chat_id, party)
        log.append(f"XP {char} +{gain}")
        return f"**✨ {char} 獲得 {gain} XP**"

    text = _XP_RE.sub(_xp, text)

    # ---- scene / objective ----
    _JUNK_PLACES = {"goal", "objective", "scene", "place", "none", "-",
                    "無", "無目標", "未知"}

    def _scene(m: re.Match) -> str:
        scene = m.group(1).strip()[:120]
        current = engine._session(platform, chat_id).get("scene", "")
        # unchanged or junk placeholder ("goal"): drop silently (anti-spam)
        if not scene or scene.lower() in _JUNK_PLACES or scene == current:
            return ""
        engine.update_game_state(platform, chat_id, scene=scene)
        log.append(f"📍 場景：{scene}")
        return f"**📍 場景轉移：{scene}**"

    text = _SCENE_RE.sub(_scene, text)

    def _obj(m: re.Match) -> str:
        obj = m.group(1).strip()[:120]
        current = engine._session(platform, chat_id).get("objective", "")
        if not obj or obj.lower() in _JUNK_PLACES or obj == current:
            return ""
        engine.update_game_state(platform, chat_id, objective=obj)
        log.append(f"🎯 目標：{obj}")
        return f"**🎯 目前目標：{obj}**"

    text = _OBJ_RE.sub(_obj, text)
    return text, log
