"""Consent-based checks: the DM proposes ([[check:DC|char|ABILITY]]), the
player consents, the Pi rolls and decides — deterministically.

Flow:
  1. DM reply contains [[check:13|大力蕉|STR]] → parsed by apply_state_tags,
     stored as the chat's pending check, rendered as a check card.
  2. Player replies consent ("骰", "roll", "好"…) → dm_reply resolves it:
     d20 + ability modifier (from the party sheet) vs DC, crits on nat 20/1.
  3. The verdict is injected into the DM's context; the DM must narrate per it.
"""

import re

CHECK_RE = re.compile(
    r"\[{2}\s*(check|save)\s*[:：]\s*(?:DC\s*)?(\d{1,2})\s*[|｜]\s*([^|｜\]]+?)"
    r"(?:\s*[|｜]\s*([A-Za-z\u4e00-\u9fff][A-Za-z\u4e00-\u9fff\s（）()·]{0,14}))?\s*\]{2}",
    re.IGNORECASE,
)

_ABILITY_RE = re.compile(
    r"(STR|DEX|CON|INT|WIS|CHA|力量|敏捷|體質|体质|智力|感知|魅力)", re.IGNORECASE)
_ABILITY_MAP = {
    "力量": "STR", "敏捷": "DEX", "體質": "CON", "体质": "CON",
    "智力": "INT", "感知": "WIS", "魅力": "CHA",
    # common skill shorthands the DM tends to emit -> governing ability
    "arc": "INT", "arcana": "INT", "history": "INT", "investigation": "INT",
    "nature": "INT", "religion": "INT",
    "acrobatics": "DEX", "sleight": "DEX", "stealth": "DEX",
    "athletics": "STR",
    "animal": "WIS", "insight": "WIS", "medicine": "WIS",
    "perception": "WIS", "survival": "WIS",
    "deception": "CHA", "intimidation": "CHA", "performance": "CHA",
    "persuasion": "CHA",
}

# strong consent tokens match anywhere; weak ones only in short replies
_STRONG_CONSENT = re.compile(r"骰|擲|掷|roll|同意|agree|confirm|進行檢定|进行检定", re.IGNORECASE)
_WEAK_CONSENT = re.compile(r"^(好|ok|okay|yes|來吧|来吧|上|可以|確認|确认|行)[!！。.~？? ]*$",
                           re.IGNORECASE)


def normalize_ability(text: str | None) -> str:
    if not text:
        return ""
    t = text.strip()
    if t.upper() in ("STR", "DEX", "CON", "INT", "WIS", "CHA"):
        return t.upper()
    return _ABILITY_MAP.get(t) or _ABILITY_MAP.get(t.lower()) or ""


def parse_check_ability(text: str | None) -> tuple[str, str]:
    """'感知' -> (WIS, ''); '洞察' -> (WIS, insight); 'WIS 洞察' -> same.
    Returns (ability, skill_key)."""
    from .charlib import normalize_skill
    if not text:
        return "", ""
    raw = text.strip()
    # composite "WIS 洞察" / "魅力(遊說)": take the first ability token plus
    # the trailing skill word
    ab = normalize_ability(raw.split()[0].split("(")[0])
    skill = ""
    if ab:
        rest = raw.split(None, 1)[1] if " " in raw else ""
        rest = rest.strip("（）()· ")
        _a, skill = normalize_skill(rest)
    else:
        ab, skill = normalize_skill(raw)
    return ab, skill


def is_consent(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if _STRONG_CONSENT.search(t):
        return True
    return bool(_WEAK_CONSENT.match(t))


def total_mod(entry: dict, ability: str, skill: str = "",
              kind: str = "check") -> int:
    """Full modifier for a check/save: ability mod + proficiency bonus when
    proficient (class save proficiency for saves, core-skill proficiency for
    skill checks)."""
    from . import charlib
    stats = (entry or {}).get("stats") or {}
    try:
        base = (int(stats.get(ability, 10)) - 10) // 2
    except (TypeError, ValueError):
        base = 0
    occ = (entry or {}).get("occupation", "")
    lvl = int((entry or {}).get("level", 1) or 1)
    if kind == "attack":
        prof = True  # weapon/spell attacks are always proficient
    elif kind == "save":
        prof = charlib.save_proficient(occ, ability)
    else:
        prof = bool(skill) and charlib.skill_proficient(occ, skill)
    return base + (charlib.prof_bonus(lvl) if prof else 0)


def render_card(dc: int, char: str, ability: str,
                mod: int = 0, need: int | None = None,
                kind: str = "check", skill: str = "") -> str:
    ab = f" {ability}{'%+d' % mod if mod else ''}" if ability else ""
    label = "豁免 SAVE" if kind == "save" else "檢定要求 Check"
    target = f"（需骰 ≥ **{need}**）" if need is not None else ""
    icon = "🛡" if kind == "save" else "🎯"
    m = f"{'%+d' % mod if mod else ''}"
    skill_note = f"（{skill}，含熟練加值）" if skill else ""
    return (f"{icon} **{label}**: {char}{ab}{skill_note} vs **DC {dc}**{target}\n"
            f"👉 **{char} 的玩家，現在輪你擲骰！回覆「骰」或直接執行 `/roll`**"
            f"（免填骰式——系統自動擲 d20{m} 並公告完整結果與成敗；"
            f"想改做其他行動，直接描述即可）")


def render_verdict(v: dict) -> str:
    if v.get("kind") == "attack":
        b = f"{v['bonus']:+d}" if v.get("bonus") else ""
        crit = ""
        if (v.get("crit") or 0) >= 20:
            crit = "（天然 20：暴擊！）"
        elif 0 < (v.get("crit") or 9) <= 1:
            crit = "（天然 1）"
        icon = "✅" if v["success"] else "❌"
        word = "命中" if v["success"] else "未命中"
        line = (f"🎲 {v['char']} 攻擊 {v.get('target', '?')}：d20({v['d20']})"
                f"{b} = {v['total']} vs AC {v['ac']} → {icon} **{word}**{crit}")
        if v.get("dmg_total") is not None:
            mv = f"（{v['move']}）" if v.get("move") else ""
            line += (f"\n💥 傷害 {v.get('dmg')} {mv}：{v.get('dmg_detail')}"
                     f" = **{v['dmg_total']}**")
            for name, hp, mx, dead in v.get("applied") or []:
                line += (f"\n　{'💀' if dead else '🗡'} {name} HP → "
                         f"**{hp}/{mx}**" + ("（倒下！）" if dead else ""))
            if v.get("combat_end"):
                line += "\n🏁 **戰鬥結束——敵人全滅！**"
        return line
    mod = f" {v['mod']:+d}" if v["mod"] else ""
    crit = ""
    if (v.get("crit") or 0) >= 20:
        crit = "（天然 20！）"
    elif 0 < (v.get("crit") or 9) <= 1:
        crit = "（天然 1！）"
    icon = "✅" if v["success"] else "❌"
    word = "成功" if v["success"] else "失敗"
    ab = f" {v['ability']}" if v.get("ability") else ""
    return (f"🎲 {v['char']}{ab} 檢定：d20({v['d20']}){mod} = {v['total']} "
            f"vs DC {v['dc']} → {icon} **{word}**{crit}")
