"""v4 rules core: validation + resolution — pure engine, no LLM.

resolve(game, intent) -> ResolveResult(rendered lines, accepted bool).
Every mechanical fact is appended to the ledger; every mutation goes
through the Game. The narrator (P2) will turn ledger entries into prose.
"""

import re

from engine.charlib import (SKILL_ABILITY, caster_kind, normalize_skill,
                            slots_for)
from engine.checks import parse_check_ability, total_mod
from engine.moves import compute_attack_moves, move_spell_level
from engine.dice import roll_expr

MOVE_SPELL_LEVEL = move_spell_level

# social ladder: index rises as an NPC warms up (engine-owned fact the
# narrator voices — the LLM may never move disposition itself)
_DISPO = ["hostile", "suspicious", "wary", "neutral",
          "negotiating", "friendly", "allied"]
# the four social skills a `talk` may ride on (SRD CHA family)
_SOCIAL = ("persuasion", "deception", "intimidation", "performance")

from .intent import Intent
from .turn import Game

_POTION = re.compile(r"治療藥水|治療药水|healing potion", re.I)
_ORD_RE = re.compile(r"第?\s*([0-9一二三四五六七八九十]+)\s*[隻个個號号]")
_ZH_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def resolve_target(g: Game, target: str):
    """(enemy, party_char) for a target string — fuzzy plus ordinal forms
    （第一隻哥布林 → 哥布林①）. The digestor normalizes most names;
    this is the engine-side safety net."""
    t = (target or "").strip()
    if not t:
        return None, None
    for n, f in g.enemies.items():
        if t == n or t in n or n in t:
            return f, None
    for n in g.party:
        if t == n or t in n or n in t:
            return None, n
    m = _ORD_RE.search(t)
    if m:
        raw = m.group(1)
        num = _ZH_NUM.get(raw, int(raw) if raw.isdigit() else 1)
        base = (t[:m.start()] + t[m.end():]).strip()
        want = base + (chr(0x2460 + num - 1) if 1 <= num <= 10 else "")
        for n, f in g.enemies.items():
            if n == want:
                return f, None
        for n, f in g.enemies.items():
            if base and base in n:
                return f, None
    return None, None


class ResolveResult:
    def __init__(self, lines=None, accepted=True, confirm=None):
        self.lines = lines or []     # engine-rendered output (degraded mode)
        self.accepted = accepted
        self.confirm = confirm       # Intent needing player confirmation


# ---------- skill machinery (all 18 SRD skills) ----------

def _dispo_step(disp: str, up: int = 1) -> str:
    i = _DISPO.index(disp) if disp in _DISPO else 3
    return _DISPO[max(0, min(len(_DISPO) - 1, i + up))]


def _dispo_zh(disp: str) -> str:
    return {"hostile": "敵視", "suspicious": "起疑", "wary": "戒備",
            "neutral": "中立", "negotiating": "願意商量",
            "friendly": "友善", "allied": "結盟"}.get(disp, disp)


def _find_npc(g: Game, target: str):
    """Scene NPC whose name fuzzy-matches the target string."""
    t = (target or "").strip()
    if not t:
        return None
    for n in g.world.here.npcs:
        if t in n["name"] or n["name"] in t:
            return n
    return None


def _find_npc_in_text(g: Game, text: str):
    """NPC mentioned anywhere in free text（「船長幫幫手啦」→ 老船長）.
    Falls back to 2-char windows of the NPC name — Chinese names are
    short and players rarely type the full form."""
    t = (text or "").strip()
    if not t:
        return None
    for n in g.world.here.npcs:
        name = n["name"]
        if name in t:
            return n
        for i in range(len(name) - 1):
            if name[i:i + 2] in t:
                return n
    return None


def _reveal_fact(g: Game, actor: str, npc: dict) -> str | None:
    """Engine-owned info drip: one NEW fact per successful exchange.
    Marks it disclosed (persisted with the NPC) and ledger-records it —
    the narrator is then directed to voice exactly this fact. Players
    see their progress, so nobody re-asks what's already answered."""
    knows = npc.get("knows", [])
    seen = npc.setdefault("disclosed", [])
    fact = next((k for k in knows if k not in seen), None)
    if fact is None:
        return None
    seen.append(fact)
    g.ledger.add(actor, "reveal",
                 f"{actor} 從 {npc['name']} 打聽到：{fact}",
                 npc=npc["name"], fact=fact)
    return fact


def _apply_check_effect(g: Game, actor: str, effect: dict | None,
                        ok: bool, lines: list[str]) -> None:
    """Mechanical outcome of a settled check. Runs whether the die came
    from the engine (auto) or the player's own /roll (pending path)."""
    if not effect or not ok:
        return
    kind = effect.get("kind", "")
    if kind == "search":
        s = g.world.here
        if s.hidden_items:
            for name, qty in s.hidden_items:
                g.give_item(actor, name, qty)
                lines.append(f"🎒 {actor} 搜到 **{name}×{qty}**")
            g.ledger.add(actor, "item", f"{actor} 搜到了 {s.hidden_items}")
            s.hidden_items = []
        else:
            lines.append("（仔細搜過，沒有特別的發現）")
    elif kind == "medicine":
        tgt = effect.get("target", "")
        e = g.party.get(tgt)
        if e is not None and not g.alive(tgt):
            e["hp_now"] = 1
            lines.append(f"🩹 {tgt} 傷勢穩定，甦醒過來（HP 1）")
            g.ledger.add(actor, "heal", f"{actor} 救醒了 {tgt}",
                         target=tgt)
    elif kind == "insight":
        npc = _find_npc(g, effect.get("target", ""))
        if npc is not None:
            knows = npc.get("knows", [])
            fact_txt = (f"{actor} 看穿了 {npc['name']} 的真實態度："
                        f"{_dispo_zh(npc.get('disposition', 'neutral'))}")
            # insight surfaces the NEXT unknown fact and marks it known
            hit = _reveal_fact(g, actor, npc)
            if hit:
                fact_txt += f"；並察覺對方知道「{hit}」"
            lines.append(f"👁 {fact_txt}")
            g.ledger.add(actor, "insight", fact_txt, npc=npc["name"],
                         disposition=npc.get("disposition", "neutral"),
                         knows=knows[:3])
    elif kind == "stealth":
        if not hasattr(g, "_stealth") or not isinstance(g._stealth, dict):
            g._stealth = {}
        g._stealth[actor] = True
        lines.append(f"🌫 {actor} 藏進陰影——下一次攻擊有優勢")
        g.ledger.add(actor, "skill", f"{actor} 潛行成功，未被發現",
                     skill="stealth")
    elif kind == "animal":
        npc = _find_npc(g, effect.get("target", ""))
        if npc is not None:
            old = npc.get("disposition", "neutral")
            npc["disposition"] = _dispo_step(old, 2)
            lines.append(f"🐎 {npc['name']} 冷靜下來了"
                         f"（{_dispo_zh(old)} → {_dispo_zh(npc['disposition'])}）")
            g.ledger.add(actor, "skill",
                         f"{actor} 安撫了 {npc['name']}",
                         skill="animal handling", npc=npc["name"])
    elif kind == "social":
        npc = _find_npc(g, effect.get("target", ""))
        if npc is not None:
            old = npc.get("disposition", "neutral")
            skill = effect.get("skill", "persuasion")
            if old in ("friendly", "allied"):
                note = f"{npc['name']} 本來就站在你們一邊"
            else:
                npc["disposition"] = _dispo_step(old, 1)
                note = (f"{npc['name']} 的態度軟化"
                        f"（{_dispo_zh(old)} → {_dispo_zh(npc['disposition'])}）")
                if skill == "intimidation":
                    note += "——但眼神裡有一絲畏懼"
            lines.append(f"🤝 {note}")
            g.ledger.add(actor, "talk",
                         f"{actor} 的{skill}奏效：{note}",
                         npc=npc["name"], skill=skill, ok=True,
                         disposition=npc["disposition"])
            # a won exchange yields one NEW fact (engine-owned drip)
            fact = _reveal_fact(g, actor, npc)
            lines.append(f"📜 打聽到：**{fact}**" if fact
                         else f"📜 {npc['name']} 沒有更多可透露的了")


# ---------- validation ----------

def validate(g: Game, it: Intent) -> str | None:
    """Return a denial reason, or None if legal."""
    actor = it.actor
    if actor and actor not in g.party:
        return f"沒有角色「{actor}」"
    if actor and not g.alive(actor):
        return f"{actor} 已倒地（HP 0），無法行動"
    if it.action == "move":
        if not g.world.find_exit(it.destination):
            here = g.world.here
            return (f"沒有通往「{it.destination}」的路。"
                    f"目前通道：{'、'.join(here.exits.values()) or '無'}")
    if it.action in ("use", "give") and actor:
        if not any(s[0] == it.item for s in g.inventory.get(actor, [])):
            return f"SLOT:item:{it.item}"  # fabrication -> snark + deny
    if it.action == "take" and actor:
        # valid only if the item exists in the scene (ground or hidden)
        s = g.world.here
        ground = [n for n, _ in s.ground_items]
        hidden = [n for n, _ in s.hidden_items]
        if not any(it.item and (it.item in n or n in it.item)
                   for n in ground + hidden):
            return f"SLOT:take:{it.item}"  # nothing to take -> snark + deny
    if it.action == "attack":
        foe, tgt_char = resolve_target(g, it.target)
        if foe is None and tgt_char is None:
            # check scene NPCs — attacking one turns them hostile
            for n in g.world.here.npcs:
                if it.target and (it.target in n["name"]
                                  or n["name"] in it.target):
                    return None  # valid: NPC target (handled in resolve)
            return f"找不到目標「{it.target}」"
    return None


# ---------- resolution ----------

def _render_check(name: str, ability: str, d20: int, mod: int, total: int,
                  dc: int, ok: bool, skill: str = "") -> str:
    from engine.charlib import SKILL_LABEL
    crit = "（天然 20！）" if d20 >= 20 else ("（天然 1！）" if d20 <= 1 else "")
    sk = (f"（{SKILL_LABEL.get(skill, skill)}）" if skill else "")
    return (f"🎲 {name} {ability}{sk} 檢定：d20({d20}){mod:+d} = {total} "
            f"vs DC {dc} → {'✅ 成功' if ok else '❌ 失敗'}{crit}")


def ability_check(g: Game, actor: str, ability: str, dc: int,
                  skill: str = "",
                  auto: bool = True,
                  effect: dict | None = None) -> tuple[bool, str] | None:
    """Roll a check. auto=True rolls immediately (combat/structured).
    auto=False shows a pending check card and waits for /roll —
    restoring v3's player-dice agency for /explore actions.
    effect: mechanical outcome ({kind, target, skill}) applied when the
    check settles — on either path, exactly once."""
    mod = total_mod(g.party[actor], ability, skill, "check")
    if not auto:
        from engine.charlib import SKILL_LABEL
        need = max(1, dc - mod)
        g._pending_check = {
            "actor": actor, "ability": ability, "skill": skill,
            "dc": dc, "mod": mod, "effect": effect or None}
        line = (f"🎯 {actor} {ability}"
                + (f"（{SKILL_LABEL.get(skill, skill)}）" if skill else "")
                + f" 檢定 vs DC {dc}（需骰 ≥ {need}）"
                + f"\n👉 用 `/roll d20` 擲骰（修正值 {mod:+d} 自動套用）")
        return None, line
    d = g.d20()
    total = d + mod
    ok = d >= 20 or (d > 1 and total >= dc)
    line = _render_check(actor, ability, d, mod, total, dc, ok, skill)
    g.ledger.add(actor, "check", line, d20=d, mod=mod, total=total,
                 dc=dc, ok=ok, skill=skill)
    out = [line]
    _apply_check_effect(g, actor, effect, ok, out)
    return ok, "\n".join(out)


def resolve_pending_check(g: Game, die: int) -> tuple[bool, str]:
    """Resolve a pending check with the player's own d20 roll."""
    p = getattr(g, "_pending_check", None)
    if not p:
        return False, ""
    g._pending_check = None
    mod = p["mod"]
    dc = p["dc"]
    total = die + mod
    ok = die >= 20 or (die > 1 and total >= dc)
    line = _render_check(p["actor"], p["ability"], die, mod, total,
                         dc, ok, p.get("skill", ""))
    g.ledger.add(p["actor"], "check", line, d20=die, mod=mod,
                 total=total, dc=dc, ok=ok, skill=p.get("skill", ""))
    out = [line]
    _apply_check_effect(g, p["actor"], p.get("effect"), ok, out)
    return ok, "\n".join(out)


def _attack(g: Game, actor: str, target: str, move: str = "") -> ResolveResult:
    lines = []
    moves = compute_attack_moves(g.party[actor].get("occupation", ""),
                                 int(g.party[actor].get("level", 1)),
                                 [("item", n, q) for n, q in g.inventory.get(actor, [])],
                                 g.max_slot(actor))
    # trailing move name: 「攻擊 哥布林① 地獄斥喝」 — peel it off the target
    for m in moves:
        if move and move != m["name"] and move.endswith(m["name"]):
            move = m["name"]
        elif not move and target.endswith(m["name"]):
            target = target[: -len(m["name"])].strip(" ：:,，,")
            move = m["name"]
            break
    mv = next((m for m in moves if m["name"] == move), None) if move else None
    ability = mv["ability"] if mv else "STR"
    dmg_expr = mv["dmg"] if mv else "1d4"
    # leveled-spell moves pay a slot first (v3 economy, engine-enforced)
    req = MOVE_SPELL_LEVEL.get(move, 0) if mv else 0
    if req and not g.spend_slot(actor, req):
        return ResolveResult([f"⚠️ {actor} 的 {req} 環法術格已用盡——改用其他招式。"],
                             accepted=False)
    # target: enemy slot first, then party (ordinal-aware)
    foe, tgt_char = resolve_target(g, target)
    if foe is None and tgt_char is None:
        return ResolveResult([f"找不到目標「{target}」"], accepted=False)
    ac = foe.ac if foe else g.ac_of(tgt_char)
    bonus = total_mod(g.party[actor], ability, kind="attack")
    # advantage sources: successful stealth (unseen attacker) or a
    # teammate's observe (weak spot found) — both one-shot
    stealthed = bool(isinstance(getattr(g, "_stealth", None), dict)
                     and g._stealth.pop(actor, False))
    aid_tgt = (foe.name if foe else tgt_char) or ""
    aided = bool(isinstance(getattr(g, "_aid", None), dict)
                 and g._aid.pop(aid_tgt, False))
    d = g.d20()
    adv_note = ""
    if stealthed or aided:
        d2 = g.d20()
        labels = ([] + (["潛行"] if stealthed else [])
                  + (["破綻"] if aided else []))
        adv_note = f"（{'＋'.join(labels)}優勢：{d}/{d2} 取高）"
        d = max(d, d2)
    total = d + bonus
    crit = d >= 20
    hit = crit or (d > 1 and total >= ac)
    tname = foe.name if foe else tgt_char
    mv_txt = f"（{move}）" if move else ""
    line = (f"🎲 {actor} 攻擊 {tname}{mv_txt}：d20({d}){bonus:+d} = {total} "
            f"vs AC {ac} → {'✅ 命中' if hit else '❌ 未命中'}"
            + adv_note
            + ("（天然 20：暴擊！）" if crit else ""))
    lines.append(line)
    g.ledger.add(actor, "attack", line, d20=d, total=total, ac=ac, hit=hit,
                 crit=crit, target=tname)
    if not hit:
        return ResolveResult(lines)
    dmg_total, dmg_detail = roll_expr(dmg_expr)
    if crit:
        t2, d2 = roll_expr(dmg_expr)
        dmg_total += t2
        dmg_detail += f"＋{d2}（暴擊骰池×2）"
    lines.append(f"💥 傷害 {dmg_expr}{mv_txt}：{dmg_detail} = **{dmg_total}**")
    g.ledger.add(actor, "damage", f"{tname} 受到 {dmg_total} 傷害",
                 target=tname, dmg=dmg_total, crit=crit)
    if foe is not None:
        foe.hp = max(0, foe.hp - dmg_total)
        lines.append(f"　🗡 {foe.name} HP → **{foe.hp}/{foe.hp_max}**")
        if foe.hp <= 0:
            foe.dead = True
            lines.append(f"💀 {foe.name} 倒下！")
            g.ledger.add(actor, "death", f"{foe.name} 倒下", target=foe.name)
        if g.check_end():
            lines.append("🏁 **戰鬥結束——敵人全滅！**")
            g.ledger.add(actor, "combat", "戰鬥結束（敵人全滅）")
    else:
        e = g.party[tgt_char]
        e["hp_now"] = max(0, int(e["hp_now"]) - dmg_total)
        lines.append(f"　💔 {tgt_char} HP → **{e['hp_now']}/{e['hp_max']}**")
    return ResolveResult(lines)


def _enemy_turn(g: Game, foe) -> list[str]:
    """Engine resolves an NPC strike — no LLM involved."""
    lines = [f"⚔️ {foe.name} 的回合："]
    targets = [n for n in g.party if g.alive(n)]
    if not targets:
        return lines
    tgt = g._random.choice(targets)  # spread damage, no deathless focus-fire
    dodging = bool(isinstance(getattr(g, "_dodge", None), dict)
                   and g._dodge.get(tgt))
    d = g.d20()
    dodge_note = ""
    if dodging:
        d2 = g.d20()
        dodge_note = f"（{tgt} 防禦中：{d}/{d2} 取低）"
        d = min(d, d2)
    total = d + foe.attack_bonus
    ac = g.ac_of(tgt)
    hit = d >= 20 or (d > 1 and total >= ac)
    lines.append(f"🎲 {foe.name} 攻擊 {tgt}：d20({d}){foe.attack_bonus:+d} "
                 f"= {total} vs AC {ac} → {'✅ 命中' if hit else '❌ 未命中'}"
                 + dodge_note)
    g.ledger.add(foe.name, "attack", lines[-1], target=tgt, hit=hit)
    if hit:
        dmg, det = roll_expr(foe.dmg)
        e = g.party[tgt]
        e["hp_now"] = max(0, int(e["hp_now"]) - dmg)
        lines.append(f"💥 {foe.name} 對 {tgt} 造成 {dmg} 傷害"
                     f"（{det}）→ 💔 HP {e['hp_now']}/{e['hp_max']}")
        g.ledger.add(foe.name, "damage", f"{tgt} 受到 {dmg} 傷害",
                     target=tgt, dmg=dmg)
    return lines


def _resolve_inner(g: Game, it: Intent) -> ResolveResult:
    """The one entry point: validate, mutate, ledger, render."""
    reason = validate(g, it)
    if reason:
        # item-from-nowhere: deny with a snark (human-DM 吐槽) — nothing
        # enters the world from thin air, but the refusal gets to be fun
        if reason.startswith("SLOT:item:"):
            item = reason.split("SLOT:item:", 1)[1]
            actor = it.actor or next(iter(g.party))
            from .templates import render_hint
            g.ledger.add(actor, "deny", f"{actor} 沒有「{item}」（無中生有）",
                         reason=f"沒有「{item}」", item=item, snark=True)
            return ResolveResult(
                [f"🚫 {actor} 沒有「{item}」",
                 "🎭 " + render_hint(g.ledger.entries[-1], g)],
                accepted=False)
        if reason.startswith("SLOT:take:"):
            item = reason.split("SLOT:take:", 1)[1]
            actor = it.actor or next(iter(g.party))
            g.ledger.add(actor, "deny",
                         f"場景中沒有「{item}」可拾取",
                         reason=f"沒有「{item}」可拾取",
                         item=item, snark=True)
            from .templates import render_hint
            return ResolveResult(
                [f"🚫 場景中沒有「{item}」可拾取",
                 "🎭 " + render_hint(g.ledger.entries[-1], g)],
                accepted=False)
        g.ledger.add(it.actor or "?", "deny", f"拒絕：{reason}",
                     reason=reason)
        # v3 lesson: never ignore the player silently — the narrator
        # must answer what the player said even when the engine denies it
        return ResolveResult([f"🚫 {reason}"], accepted=False)
    if it.action == "chat":
        # player-to-player table talk: remembered for context (ledger +
        # narrator), but the DM stays out of it — no reply, no turn spent
        g.ledger.add(it.actor or "?", "table",
                     f"桌邊：{(it.utterance or it.raw)[:80]}")
        return ResolveResult([], accepted=True)
    g.ledger.next_turn()
    cur = g.combat.current() if g.combat.active else None
    if not it.actor and cur is not None and not cur.get("npc"):
        actor = cur["name"]  # bare action in combat = the current hero acts
    else:
        actor = it.actor or next((n for n in g.party if g.alive(n)),
                                 next(iter(g.party)))
    # combat rotation is engine-enforced: only the current hero may act
    if g.combat.active and it.action not in ("pass",):
        if cur is not None and not cur.get("npc") and actor != cur["name"]:
            reason = f"現在輪到 {cur['name']}"
            g.ledger.add(actor, "deny", f"拒絕：{reason}", reason=reason)
            return ResolveResult(
                [f"⏳ {reason}——請該角色行動"], accepted=False)

    if it.action == "attack":
        # confirm only when the digestor is genuinely uncertain (freeform
        # /explore with ambiguous phrasing); /combat always sets confidence=1
        if it.confidence < 0.7 and not it.args.get("confirmed"):
            alt = ("`/combat` 選擇行動" if g.combat.active
                   else "`/explore` 描述其他行動")
            return ResolveResult(
                [f"❓ 我理解你要：**{actor} 攻擊 {it.target}**"
                 f"——用 `/confirm` 執行，或 {alt}。"],
                confirm=it)
        # attacking a scene NPC: turn them hostile and start combat
        foe, tgt_char = resolve_target(g, it.target)
        if foe is None and tgt_char is None:
            for n in g.world.here.npcs:
                if it.target and (it.target in n["name"]
                                  or n["name"] in it.target):
                    from .world import Enemy
                    enemy = Enemy.make(n["name"], 8, 13, 3, "1d6")
                    g.encounters.setdefault(g.world.current, []).append(
                        enemy)
                    if not g.combat.active:
                        g.start_encounter()
                        g.ledger.add(actor, "combat",
                                     f"{actor} 攻擊 {n['name']}——"
                                     f"{n['name']} 變得敵對！")
                    # remove the NPC from the friendly list
                    g.world.here.npcs = [
                        x for x in g.world.here.npcs
                        if x["name"] != n["name"]]
                    # re-resolve target (now in g.enemies)
                    foe, tgt_char = resolve_target(g, it.target)
                    break
        r = _attack(g, actor, it.target, it.args.get("move", ""))
        post = _post_rotation(g)
        return ResolveResult(r.lines + post, accepted=r.accepted,
                             confirm=r.confirm)

    if it.action == "move":
        sid = g.world.find_exit(it.destination)
        g.world.current = sid
        s = g.world.here
        g.ledger.add(actor, "scene", f"前往 {s.name}", scene=sid)
        lines = [f"📍 場景轉移：{s.name}", s.description]
        if s.exits:
            lines.append("通道：" + "、".join(s.exits.values()))
        # an encounter here springs automatically (engine-owned)
        if g.world.current in g.encounters and not g.combat.active \
                and g.encounters[g.world.current]:
            order = g.start_encounter()
            lines.append("⚔️ **戰鬥開始！** 先攻：" + " → ".join(
                o["name"] + ("（敵）" if o["npc"] else "") for o in order))
            g.ledger.add(actor, "combat", "戰鬥開始",
                         order=[o["name"] for o in order])
        return ResolveResult(lines)

    if it.action == "use":
        # 5e: you can administer a potion to a companion — including a
        # DOWNED one（餵藥）. The ACTOR must be alive; the target need not.
        target = (it.args.get("to") or it.target or "").strip()
        tgt = ""
        for n in g.party:
            if target and (n == target or n in target or target in n):
                tgt = n
                break
        if _POTION.search(it.item):
            heal_who = tgt or actor
            dmg, det = roll_expr("2d4+2")
            e = g.party[heal_who]
            before = int(e["hp_now"])
            e["hp_now"] = min(int(e["hp_max"]), before + dmg)
            g.take_item(actor, it.item, 1)
            if heal_who == actor:
                g.ledger.add(actor, "heal", f"{actor} 喝下治療藥水，回復 "
                             f"{e['hp_now'] - before} HP", item=it.item)
                return ResolveResult([
                    f"💚 {actor} 使用 {it.item}（{det} = {dmg}）→ "
                    f"HP {before} → **{e['hp_now']}/{e['hp_max']}**"])
            g.ledger.add(actor, "heal",
                         f"{actor} 餵 {heal_who} 喝下治療藥水，回復 "
                         f"{e['hp_now'] - before} HP", item=it.item,
                         target=heal_who)
            woke = "，甦醒過來！" if before <= 0 else ""
            return ResolveResult([
                f"💚 {actor} 餵 {heal_who} 喝下 {it.item}"
                f"（{det} = {dmg}）→ HP {before} → "
                f"**{e['hp_now']}/{e['hp_max']}**{woke}"])
        g.take_item(actor, it.item, 1)
        g.ledger.add(actor, "item", f"{actor} 使用了 {it.item}", item=it.item)
        return ResolveResult([f"🎒 {actor} 使用了 {it.item}（無機械效果）"])

    if it.action == "give":
        to = it.args.get("to", "")
        if to not in g.party or not g.take_item(actor, it.item, 1):
            return ResolveResult([f"🚫 無法轉移 {it.item}"], accepted=False)
        g.give_item(to, it.item, 1)
        g.ledger.add(actor, "item", f"{actor} 把 {it.item} 給了 {to}")
        return ResolveResult([f"🎒 {actor} → {to}：{it.item}×1"])

    if it.action == "take":
        # pick up a ground item or a revealed hidden item
        s = g.world.here
        for name, qty in s.ground_items:
            if it.item and (it.item in name or name in it.item):
                s.ground_items.remove((name, qty))
                g.give_item(actor, name, qty)
                g.ledger.add(actor, "item", f"{actor} 拾起 {name}×{qty}")
                return ResolveResult([f"🎒 {actor} 拾起 {name}×{qty}"])
        for name, qty in s.hidden_items:
            if it.item and (it.item in name or name in it.item):
                s.hidden_items.remove((name, qty))
                g.give_item(actor, name, qty)
                g.ledger.add(actor, "item", f"{actor} 拾起 {name}×{qty}")
                return ResolveResult([f"🎒 {actor} 拾起 {name}×{qty}"])
        g.ledger.add(actor, "deny", f"場景中沒有「{it.item}」")
        return ResolveResult([f"🚫 場景中沒有「{it.item}」可拾取"],
                             accepted=False)

    if it.action == "search":
        s = g.world.here
        _, line = ability_check(g, actor, "WIS", s.search_dc,
                                "perception", auto=False,
                                effect={"kind": "search"})
        return ResolveResult([line])

    if it.action == "rest":
        kind = it.args.get("kind", "short")
        lines = []
        for name, e in g.party.items():
            lvl = int(e.get("level", 1))
            occ = e.get("occupation", "")
            if kind == "long":
                e["slots"] = {str(k): v for k, v in
                              slots_for(occ, lvl).items()}
                if g.alive(name):  # a long rest never revives the downed
                    e["hp_now"] = e["hp_max"]
                lines.append(f"🌙 {name}：HP {e['hp_now']}/{e['hp_max']}，"
                             "法術格全滿")
            elif g.alive(name):
                dmg, _ = roll_expr("1d8+2")
                e["hp_now"] = min(int(e["hp_max"]), int(e["hp_now"]) + dmg)
                line = f"🌿 {name}：HP → {e['hp_now']}/{e['hp_max']}"
                # pact magic (Warlock): ALL slots return on a SHORT rest
                if caster_kind(occ) == "pact":
                    full = {str(k): v for k, v
                            in slots_for(occ, lvl).items()}
                    if full and e.get("slots") != full:
                        e["slots"] = full
                        line += "，契約法術格全滿"
                lines.append(line)
            else:
                lines.append(f"🌑 {name}：倒地中，休息無效（需救治）")
        g.ledger.add(actor, "rest", f"隊伍{'長' if kind=='long' else '短'}休")
        return ResolveResult(lines)

    if it.action == "creative":
        # human-DM ruling: a plausible improvised method gets a real ability
        # check; success writes a fact the narrator dramatizes, failure gets
        # a light 吐槽. Never grants items/damage by itself — mechanical
        # effects still go through canonical actions.
        ability = (it.ability or "DEX").upper()
        if ability not in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
            ability = "DEX"
        dc = int(it.args.get("dc", 12))
        ok, line = ability_check(g, actor, ability, dc, auto=False)
        g.ledger.add(actor, "creative",
                     f"{actor} 的花招（{it.utterance[:60]}）——"
                     + ("成功" if ok else "失敗"),
                     utterance=it.utterance[:120], ok=ok)
        return ResolveResult([line])

    if it.action == "cast":
        if not g.spend_slot(actor, 1):
            return ResolveResult([f"⚠️ {actor} 的法術格已用盡"], accepted=False)
        g.ledger.add(actor, "cast", f"{actor} 施展 {it.spell}", spell=it.spell)
        return ResolveResult([f"✨ {actor} 施展 {it.spell}"
                              "（效果由敘事層描述；機械後果走 attack/use）"])

    if it.action in ("talk",):
        # addressing a scene NPC: a real social exchange — the engine picks
        # the difficulty from disposition + social skill, success moves the
        # NPC's disposition (a fact), the narrator voices the reply
        npc = None
        if it.target:
            npc = _find_npc(g, it.target)
        if npc is None:
            # deterministic path puts everything in the utterance —
            # 「講 船長幫幫手啦」: scan it for a scene NPC's name
            npc = _find_npc_in_text(g, it.utterance)
        if npc is not None:
            disp = npc.get("disposition", "neutral")
            from engine.charlib import SKILL_LABEL
            skill = it.skill if it.skill in _SOCIAL else "persuasion"
            sk_zh = SKILL_LABEL.get(skill, skill)
            lines = [f"🗣 {actor} 向 {npc['name']}（{sk_zh}）：{it.utterance}"]
            if disp in ("hostile", "suspicious"):
                dc = 14 if skill == "persuasion" else 13
            elif disp in ("negotiating", "neutral", "wary"):
                dc = 12 if skill == "persuasion" else 11
            else:  # friendly/allied: no gate, the NPC engages willingly
                dc = 0
            if dc:
                ok, line = ability_check(g, actor, "CHA", dc, skill,
                                         auto=False,
                                         effect={"kind": "social",
                                                 "target": npc["name"],
                                                 "skill": skill})
                lines.append(line)
            else:
                ok = True
                lines.append(f"🤝 {npc['name']} 樂意回應")
                fact = _reveal_fact(g, actor, npc)
                lines.append(f"📜 打聽到：**{fact}**" if fact
                             else f"（{npc['name']} 已把知道的都說了）")
            g.ledger.add(actor, "talk",
                         f"{actor} 向 {npc['name']}：{it.utterance}",
                         utterance=it.utterance, npc=npc["name"], ok=ok,
                         disposition=disp, skill=skill)
            return ResolveResult(lines)
        g.ledger.add(actor, "talk", f"{actor}：「{it.utterance}」",
                     utterance=it.utterance)
        return ResolveResult([f"💬 {actor}：「{it.utterance}」"])

    if it.action == "skill":
        # a named skill attempt: normalize the skill word, pick a DC and
        # the mechanical effect from the skill + context (never the LLM)
        ab, sk = normalize_skill(it.skill or it.spell or it.utterance)
        if not sk:
            return ResolveResult(
                [f"❓ 未知的技能「{it.skill or it.spell or '?'}」。"
                 "可用：潛行／洞察／醫藥／運動／特技／調查／察覺／求生／"
                 "奧秘／歷史／自然／宗教／馴獸／欺瞞／恐嚇／表演／說服／手技"],
                accepted=False)
        target = it.target
        dc = 12
        effect = None
        if sk in ("perception", "investigation"):
            dc = g.world.here.search_dc
            effect = {"kind": "search"}
        elif sk == "medicine":
            dc = 10
            foe, tgt_char = resolve_target(g, target)
            if tgt_char is None or g.alive(tgt_char):
                # no downed target named: pick the most wounded ally
                hurt = [n for n in g.party if not g.alive(n)] or [
                    n for n, e in g.party.items()
                    if int(e["hp_now"]) < int(e["hp_max"])]
                tgt_char = hurt[0] if hurt else actor
            target = tgt_char
            if not g.alive(tgt_char):
                effect = {"kind": "medicine", "target": tgt_char}
            else:
                effect = None  # healing the walking is a healer's kit thing
                dc = 12
        elif sk == "insight":
            npc = _find_npc(g, target)
            if npc is None:
                return ResolveResult(
                    [f"❓ 洞悉需要一個對象——這裡的 NPC："
                     + ("、".join(n["name"] for n in g.world.here.npcs)
                        or "無")], accepted=False)
            target = npc["name"]
            effect = {"kind": "insight", "target": npc["name"]}
        elif sk == "stealth":
            effect = {"kind": "stealth"}
        elif sk == "animal handling":
            npc = _find_npc(g, target)
            if npc is None:
                return ResolveResult(
                    [f"❓ 馴獸需要一個動物對象——這裡的 NPC："
                     + ("、".join(n["name"] for n in g.world.here.npcs)
                        or "無")], accepted=False)
            target = npc["name"]
            effect = {"kind": "animal", "target": npc["name"]}
        elif sk in _SOCIAL:
            # standalone social ploy without a talk utterance
            npc = _find_npc(g, target) or (
                g.world.here.npcs[0] if g.world.here.npcs else None)
            if npc is None:
                return ResolveResult(
                    ["❓ 這裡沒有可以交涉的對象。"], accepted=False)
            target = npc["name"]
            effect = {"kind": "social", "target": npc["name"], "skill": sk}
            dc = 13 if npc.get("disposition", "neutral") in \
                ("hostile", "suspicious") else 11
        # knowledge (arcana/history/nature/religion) and physical
        # (athletics/acrobatics/sleight of hand/survival): a plain check —
        # success is a fact the narrator dramatizes, failure adds nothing
        # combat (/combat menu = structured consent) auto-rolls and burns
        # the turn; /explore keeps the player-dice pending flow
        in_combat = g.combat.active
        _, line = ability_check(g, actor, ab, dc, sk, auto=in_combat,
                                effect=effect)
        g.ledger.add(actor, "skill",
                     f"{actor} 使出 {sk}"
                     + (f"（對 {target}）" if target else ""),
                     skill=sk, target=target)
        lines = [line]
        if in_combat:
            lines += _post_rotation(g)
        return ResolveResult(lines)

    if it.action == "check":
        # plain ability check; a trailing skill word upgrades it with
        # proficiency（「檢定 洞察」→ WIS + insight proficiency）
        ab, sk = parse_check_ability(it.ability or it.skill or "")
        if not ab:
            ab = "STR"
        ok, line = ability_check(g, actor, ab, 13, sk, auto=False)
        return ResolveResult([line])

    if it.action == "pass":
        g.ledger.add(actor, "pass", f"{actor} 選擇等待")
        return ResolveResult(
            [f"⏭ {actor} 等待"] + _post_rotation(g))

    if it.action == "defend":
        # dodge: until their next turn, attacks against them have
        # disadvantage (engine-flagged, enemy rolls see it)
        if not isinstance(getattr(g, "_dodge", None), dict):
            g._dodge = {}
        g._dodge[actor] = True
        g.ledger.add(actor, "defend", f"{actor} 擺出防禦姿態（閃避）")
        return ResolveResult(
            [f"🛡 {actor} 全神貫注防守——敵人下一次攻擊他將有劣勢"]
            + _post_rotation(g))

    if it.action == "escape":
        if not g.combat.active:
            return ResolveResult(["❓ 現在沒有戰鬥，不用撤退。"],
                                 accepted=False)
        # party disengage attempt: DEX check, success = leave combat
        ok, line = ability_check(g, actor, "DEX", 12,
                                 auto=True)
        lines = [line]
        if ok:
            g.end_combat()
            g.ledger.add(actor, "combat", f"{actor} 帶領隊伍脫離戰鬥")
            lines.append("🏃 **隊伍趁亂脫離戰鬥！**")
        else:
            lines.append(f"❌ {actor} 被攔住了——戰鬥繼續")
            lines += _post_rotation(g)
        return ResolveResult(lines)

    if it.action == "observe":
        # study an enemy: find the weak spot — the next attack against
        # that target rolls with advantage (one-shot, like Help)
        foe, tgt_char = resolve_target(g, it.target)
        if foe is None:
            foes = [f for f in g.enemies.values() if not f.dead]
            if not foes:
                return ResolveResult(["❓ 沒有可觀察的目標"], accepted=False)
            foe = foes[0]
        if not isinstance(getattr(g, "_aid", None), dict):
            g._aid = {}
        g._aid[foe.name] = True
        g.ledger.add(actor, "observe",
                     f"{actor} 盯著 {foe.name} 找破綻", target=foe.name)
        return ResolveResult(
            [f"👁 {actor} 觀察 **{foe.name}**——下一個攻擊它的隊友將有優勢"]
            + _post_rotation(g))

    # unknown / meta / aspiration: don't reject — show context + options
    # and let the narrator respond in-character to what the player said
    g.ledger.add(actor, it.action or "meta",
                 f"{actor}：{it.utterance or it.raw or it.action}")
    from .templates import render_turn_context
    ctx = render_turn_context(g, actor)
    return ResolveResult(
        [f"💬 {actor}：{it.utterance or it.raw or '?'}", "", ctx],
        accepted=True)


def _post_rotation(g: Game) -> list[str]:
    """After a PC action in combat: advance, auto-resolve NPC slots, and
    never park the rotation on a downed PC. RETURNS the enemy lines —
    callers must show them (the counterattack that downs a hero must
    never be silent)."""
    out: list[str] = []
    if not g.combat.active:
        return out
    g.advance()
    guard = 0
    while g.combat.active and guard < 200:
        guard += 1
        cur = g.combat.current()
        if cur is None:
            return out
        if cur.get("npc"):
            foe = g.enemies.get(cur["name"])
            if foe is None or foe.dead:
                g.advance()
                continue
            for line in _enemy_turn(g, foe):
                out.append(line)
                g.ledger.add(foe.name, "auto", line)
            if not any(g.alive(n) for n in g.party):
                out.append("🏴 **全隊倒地——戰鬥結束（敗北）**")
                g.ledger.add("engine", "combat", "全隊倒地——戰鬥結束（敗北）")
                g.end_combat()
                return out
            g.advance()
            continue
        if not g.alive(cur["name"]):
            g.advance()  # downed PC: their turn is skipped by the engine
            continue
        # their turn arrives: the dodge stance ends (lasted one round)
        if isinstance(getattr(g, "_dodge", None), dict):
            g._dodge.pop(cur["name"], None)
        return out
    return out


def resolve(g: Game, it: Intent) -> ResolveResult:
    """The one entry point. If it's an enemy's turn, auto-resolve ALL
    consecutive NPC slots immediately (never make the player wait), then
    dispatch the player's action with the enemy results prepended.
    A pending check remembers the player's originating words so the
    /roll settle narration can answer THEM, not just the die."""
    r = _resolve_dispatch(g, it)
    p = getattr(g, "_pending_check", None)
    if p is not None and not p.get("origin"):
        p["origin"] = (it.utterance or it.raw or "")[:120]
        p["help"] = bool(getattr(it, "wants_help", False))
    return r


def _resolve_dispatch(g: Game, it: Intent) -> ResolveResult:
    if not g.combat.active or it.action == "pass":
        return _resolve_inner(g, it)
    cur = g.combat.current()
    if cur is None or not cur.get("npc"):
        return _resolve_inner(g, it)  # already a PC's turn
    # enemy turn: resolve all NPC slots now
    pre_lines = []
    guard = 0
    while g.combat.active and guard < 50:
        guard += 1
        cur = g.combat.current()
        if cur is None or not cur.get("npc"):
            break
        foe = g.enemies.get(cur["name"])
        if foe is None or foe.dead:
            g.advance()
            continue
        for line in _enemy_turn(g, foe):
            pre_lines.append(line)
            g.ledger.add(foe.name, "auto", line)
        if not any(g.alive(n) for n in g.party):
            g.ledger.add("engine", "combat",
                         "全隊倒地——戰鬥結束（敗北）")
            g.end_combat()
            return ResolveResult(pre_lines, accepted=True)
        g.advance()
    # rotation reached a PC (or combat ended) — dispatch the player action
    r = _resolve_inner(g, it)
    return ResolveResult(pre_lines + r.lines, accepted=r.accepted,
                         confirm=r.confirm)
