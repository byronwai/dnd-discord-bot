"""v4 rules core: validation + resolution — pure engine, no LLM.

resolve(game, intent) -> ResolveResult(rendered lines, accepted bool).
Every mechanical fact is appended to the ledger; every mutation goes
through the Game. The narrator (P2) will turn ledger entries into prose.
"""

import re

from engine.charlib import slots_for
from engine.checks import total_mod
from engine.dm import DMEngine, compute_attack_moves
from engine.dice import roll_expr

MOVE_SPELL_LEVEL = DMEngine.MOVE_SPELL_LEVEL

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
            return f"{actor} 沒有「{it.item}」"
    if it.action == "attack":
        foe, tgt_char = resolve_target(g, it.target)
        if foe is None and tgt_char is None:
            return f"找不到目標「{it.target}」"
    return None


# ---------- resolution ----------

def _render_check(name: str, ability: str, d20: int, mod: int, total: int,
                  dc: int, ok: bool) -> str:
    crit = "（天然 20！）" if d20 >= 20 else ("（天然 1！）" if d20 <= 1 else "")
    return (f"🎲 {name} {ability} 檢定：d20({d20}){mod:+d} = {total} "
            f"vs DC {dc} → {'✅ 成功' if ok else '❌ 失敗'}{crit}")


def ability_check(g: Game, actor: str, ability: str, dc: int,
                  skill: str = "") -> tuple[bool, str]:
    mod = total_mod(g.party[actor], ability, skill, "check")
    d = g.d20()
    total = d + mod
    ok = d >= 20 or (d > 1 and total >= dc)
    line = _render_check(actor, ability, d, mod, total, dc, ok)
    g.ledger.add(actor, "check", line, d20=d, mod=mod, total=total,
                 dc=dc, ok=ok)
    return ok, line


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
    d = g.d20()
    total = d + bonus
    crit = d >= 20
    hit = crit or (d > 1 and total >= ac)
    tname = foe.name if foe else tgt_char
    mv_txt = f"（{move}）" if move else ""
    line = (f"🎲 {actor} 攻擊 {tname}{mv_txt}：d20({d}){bonus:+d} = {total} "
            f"vs AC {ac} → {'✅ 命中' if hit else '❌ 未命中'}"
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
    d = g.d20()
    total = d + foe.attack_bonus
    ac = g.ac_of(tgt)
    hit = d >= 20 or (d > 1 and total >= ac)
    lines.append(f"🎲 {foe.name} 攻擊 {tgt}：d20({d}){foe.attack_bonus:+d} "
                 f"= {total} vs AC {ac} → {'✅ 命中' if hit else '❌ 未命中'}")
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


def resolve(g: Game, it: Intent) -> ResolveResult:
    """The one entry point: validate, mutate, ledger, render."""
    reason = validate(g, it)
    if reason:
        g.ledger.add(it.actor or "?", "deny", f"拒絕：{reason}", reason=reason)
        return ResolveResult([f"🚫 {reason}"], accepted=False)
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
            reason = f"現在輪到 {cur['name']}（戰鬥輪替）"
            g.ledger.add(actor, "deny", f"拒絕：{reason}", reason=reason)
            return ResolveResult(
                [f"⏳ {reason}——請該角色行動，或等待"], accepted=False)
        if cur is not None and cur.get("npc"):
            reason = f"敵方回合（{cur['name']}）由引擎自動結算——稍候或 pass"
            g.ledger.add(actor, "deny", f"拒絕：{reason}", reason=reason)
            return ResolveResult([f"⏳ {reason}"], accepted=False)

    if it.action == "attack":
        # risky action: engine confirms the parsed intent once
        if it.confidence < 1.0 and not it.args.get("confirmed"):
            return ResolveResult(
                [f"❓ 我理解你要：**{actor} 攻擊 {it.target}**"
                 "——回覆「確認」執行，或描述其他行動。"],
                confirm=it)
        r = _attack(g, actor, it.target, it.args.get("move", ""))
        _post_rotation(g)
        return r

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
        if _POTION.search(it.item):
            dmg, det = roll_expr("2d4+2")
            e = g.party[actor]
            before = int(e["hp_now"])
            e["hp_now"] = min(int(e["hp_max"]), before + dmg)
            g.take_item(actor, it.item, 1)
            g.ledger.add(actor, "heal", f"{actor} 喝下治療藥水，回復 "
                         f"{e['hp_now'] - before} HP", item=it.item)
            return ResolveResult([
                f"💚 {actor} 使用 {it.item}（{det} = {dmg}）→ "
                f"HP {before} → **{e['hp_now']}/{e['hp_max']}**"])
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

    if it.action == "search":
        s = g.world.here
        ok, line = ability_check(g, actor, "WIS", s.search_dc, "perception")
        lines = [line]
        if ok and s.hidden_items:
            for name, qty in s.hidden_items:
                g.give_item(actor, name, qty)
                lines.append(f"🎒 {actor} 搜到 **{name}×{qty}**")
            g.ledger.add(actor, "item", f"{actor} 搜到了 {s.hidden_items}")
            s.hidden_items = []
        elif ok:
            lines.append("（仔細搜過，沒有特別的發現）")
        return ResolveResult(lines)

    if it.action == "rest":
        kind = it.args.get("kind", "short")
        lines = []
        for name, e in g.party.items():
            lvl = int(e.get("level", 1))
            if kind == "long":
                e["slots"] = {str(k): v for k, v in
                              slots_for(e.get("occupation", ""), lvl).items()}
                if g.alive(name):  # a long rest never revives the downed
                    e["hp_now"] = e["hp_max"]
                lines.append(f"🌙 {name}：HP {e['hp_now']}/{e['hp_max']}，"
                             "法術格全滿")
            elif g.alive(name):
                dmg, _ = roll_expr("1d8+2")
                e["hp_now"] = min(int(e["hp_max"]), int(e["hp_now"]) + dmg)
                lines.append(f"🌿 {name}：HP → {e['hp_now']}/{e['hp_max']}")
            else:
                lines.append(f"🌑 {name}：倒地中，休息無效（需救治）")
        g.ledger.add(actor, "rest", f"隊伍{'長' if kind=='long' else '短'}休")
        return ResolveResult(lines)

    if it.action == "cast":
        if not g.spend_slot(actor, 1):
            return ResolveResult([f"⚠️ {actor} 的法術格已用盡"], accepted=False)
        g.ledger.add(actor, "cast", f"{actor} 施展 {it.spell}", spell=it.spell)
        return ResolveResult([f"✨ {actor} 施展 {it.spell}"
                              "（效果由敘事層描述；機械後果走 attack/use）"])

    if it.action in ("talk",):
        g.ledger.add(actor, "talk", f"{actor}：「{it.utterance}」",
                     utterance=it.utterance)
        return ResolveResult([f"💬 {actor}：「{it.utterance}」"])

    if it.action == "check":
        ok, line = ability_check(g, actor, it.ability or "STR", 13)
        return ResolveResult([line])

    if it.action == "pass":
        g.ledger.add(actor, "pass", f"{actor} 選擇等待")
        _post_rotation(g)
        return ResolveResult([f"⏭ {actor} 等待"])

    return ResolveResult([f"🤔 引擎還不認得這個動作（{it.action}）"],
                         accepted=False)


def _post_rotation(g: Game) -> None:
    """After a PC action in combat: advance, auto-resolve NPC slots, and
    never park the rotation on a downed PC."""
    if not g.combat.active:
        return
    g.advance()
    guard = 0
    while g.combat.active and guard < 200:
        guard += 1
        cur = g.combat.current()
        if cur is None:
            return
        if cur.get("npc"):
            foe = g.enemies.get(cur["name"])
            if foe is None or foe.dead:
                g.advance()
                continue
            for line in _enemy_turn(g, foe):
                g.ledger.add(foe.name, "auto", line)
            if not any(g.alive(n) for n in g.party):
                g.ledger.add("engine", "combat", "全隊倒地——戰鬥結束（敗北）")
                g.end_combat()
                return
            g.advance()
            continue
        if not g.alive(cur["name"]):
            g.advance()  # downed PC: their turn is skipped by the engine
            continue
        return
