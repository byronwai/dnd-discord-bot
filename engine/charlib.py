"""Character math from the SRD: proficiency bonus, default AC per class,
saving-throw & core-skill proficiencies, and spell-slot tables.

Lazily applied — older party entries without these fields are computed on
the fly, so existing characters pick everything up without re-creation.
"""

# proficiency bonus by level (SRD): +2 at 1-4, +3 at 5-8, ...
def prof_bonus(level: int) -> int:
    return 2 + (max(1, int(level or 1)) - 1) // 4


# class saving-throw proficiencies (SRD)
SAVE_PROF = {
    "Barbarian": ["STR", "CON"], "Bard": ["DEX", "CHA"], "Cleric": ["WIS", "CHA"],
    "Druid": ["INT", "WIS"], "Fighter": ["STR", "CON"], "Monk": ["STR", "DEX"],
    "Paladin": ["WIS", "CHA"], "Ranger": ["STR", "DEX"], "Rogue": ["DEX", "INT"],
    "Sorcerer": ["CON", "CHA"], "Warlock": ["WIS", "CHA"], "Wizard": ["INT", "WIS"],
}

# the 18 skills -> governing ability (SRD)
SKILL_ABILITY = {
    "athletics": "STR",
    "acrobatics": "DEX", "sleight of hand": "DEX", "stealth": "DEX",
    "arcana": "INT", "history": "INT", "investigation": "INT",
    "nature": "INT", "religion": "INT",
    "animal handling": "WIS", "insight": "WIS", "medicine": "WIS",
    "perception": "WIS", "survival": "WIS",
    "deception": "CHA", "intimidation": "CHA", "performance": "CHA",
    "persuasion": "CHA",
}
# zh aliases (deterministic mapping before lookup)
SKILL_ZH = {
    "運動": "athletics",
    "特技": "acrobatics", "手技": "sleight of hand", "潛行": "stealth",
    "隱匿": "stealth",
    "奧秘": "arcana", "歷史": "history", "調查": "investigation",
    "自然": "nature", "宗教": "religion",
    "馴獸": "animal handling", "洞悉": "insight", "洞察": "insight",
    "醫藥": "medicine", "察覺": "perception", "求生": "survival",
    "欺瞞": "deception", "欺騙": "deception", "恐嚇": "intimidation",
    "表演": "performance", "說服": "persuasion",
}

# canonical zh display name per skill (for menus and sheets)
SKILL_LABEL = {
    "athletics": "運動", "acrobatics": "特技", "sleight of hand": "手技",
    "stealth": "潛行", "arcana": "奧秘", "history": "歷史",
    "investigation": "調查", "nature": "自然", "religion": "宗教",
    "animal handling": "馴獸", "insight": "洞察", "medicine": "醫藥",
    "perception": "察覺", "survival": "求生", "deception": "欺瞞",
    "intimidation": "恐嚇", "performance": "表演", "persuasion": "說服",
}

# core skills per class (SRD quick-build picks — our house standard): a
# check tagged with one of these applies the proficiency bonus
CORE_SKILLS = {
    "Barbarian": ["athletics", "intimidation", "survival"],
    "Bard": ["persuasion", "performance", "deception"],
    "Cleric": ["insight", "medicine", "religion"],
    "Druid": ["animal handling", "insight", "nature", "perception"],
    "Fighter": ["athletics", "intimidation", "perception"],
    "Monk": ["acrobatics", "insight", "religion"],
    "Paladin": ["athletics", "insight", "persuasion"],
    "Ranger": ["survival", "perception", "stealth"],
    "Rogue": ["stealth", "acrobatics", "investigation", "deception"],
    "Sorcerer": ["arcana", "deception", "persuasion"],
    "Warlock": ["arcana", "deception", "investigation"],
    "Wizard": ["arcana", "history", "investigation"],
}


def normalize_skill(text: str | None) -> tuple[str, str]:
    """'洞察'/'insight'/'WIS' -> (ability, skill or ''). Deterministic."""
    if not text:
        return "", ""
    t = text.strip().lower()
    if t in SKILL_ZH:
        sk = SKILL_ZH[t]
        return SKILL_ABILITY[sk], sk
    if t in SKILL_ABILITY:
        return SKILL_ABILITY[t], t
    return "", ""


def skill_proficient(occupation: str, skill: str) -> bool:
    return skill in CORE_SKILLS.get(occupation, [])


def save_proficient(occupation: str, ability: str) -> bool:
    return ability in SAVE_PROF.get(occupation, [])


def default_ac(occupation: str, stats: dict) -> int:
    """Starting-armor AC approximation from the SRD quick builds."""
    def m(k):
        try:
            return (int(stats.get(k, 10)) - 10) // 2
        except (TypeError, ValueError):
            return 0
    dex = m("DEX")
    if occupation in ("Fighter", "Paladin"):
        return 18            # chain mail + shield
    if occupation == "Cleric":
        return 17            # scale mail + shield
    if occupation == "Ranger":
        return 14 + min(dex, 2)
    if occupation == "Druid":
        return 13 + min(dex, 2)
    if occupation == "Barbarian":
        return 10 + dex + m("CON")
    if occupation == "Monk":
        return 10 + dex + m("WIS")
    if occupation in ("Bard", "Rogue", "Warlock"):
        return 11 + dex      # leather
    return 10 + dex          # Sorcerer, Wizard: unarmored


# --- spell slots ------------------------------------------------------------

# full-caster table (Bard Cleric Druid Sorcerer Wizard): slots per level 1..9
_FULL = [
    [2], [3], [4, 2], [4, 3], [4, 3, 2], [4, 3, 3], [4, 3, 3, 1], [4, 3, 3, 2],
    [4, 3, 3, 3, 1], [4, 3, 3, 3, 2], [4, 3, 3, 3, 2, 1], [4, 3, 3, 3, 2, 1],
    [4, 3, 3, 3, 2, 1, 1], [4, 3, 3, 3, 2, 1, 1], [4, 3, 3, 3, 2, 1, 1, 1],
    [4, 3, 3, 3, 2, 1, 1, 1], [4, 3, 3, 3, 2, 1, 1, 1, 1],
    [4, 3, 3, 3, 2, 1, 1, 1, 1], [4, 3, 3, 3, 3, 1, 1, 1, 1],
    [4, 3, 3, 3, 3, 2, 2, 2, 2],
]
# half-caster (Paladin Ranger): slots 1..5
_HALF = [
    [], [2], [3], [3], [4, 2], [4, 2], [4, 3], [4, 3], [4, 3, 2], [4, 3, 2],
    [4, 3, 3], [4, 3, 3], [4, 3, 3, 1], [4, 3, 3, 1], [4, 3, 3, 2],
    [4, 3, 3, 2], [4, 3, 3, 3, 1], [4, 3, 3, 3, 1], [4, 3, 3, 3, 2],
    [4, 3, 3, 3, 2],
]


def caster_kind(occupation: str) -> str:
    """'full' | 'half' | 'pact' | '' (no casting)."""
    if occupation in ("Bard", "Cleric", "Druid", "Sorcerer", "Wizard"):
        return "full"
    if occupation in ("Paladin", "Ranger"):
        return "half"
    if occupation == "Warlock":
        return "pact"
    return ""


def slots_for(occupation: str, level: int) -> dict[int, int]:
    """{slot_level: count} for the class at this level (SRD).

    Warlock pact magic: few slots, all at the pact slot level."""
    lvl = max(1, min(20, int(level or 1)))
    kind = caster_kind(occupation)
    if kind == "full":
        return {i + 1: n for i, n in enumerate(_FULL[lvl - 1]) if n}
    if kind == "half":
        return {i + 1: n for i, n in enumerate(_HALF[lvl - 1]) if n}
    if kind == "pact":
        count = 1 if lvl == 1 else (3 if lvl >= 11 else 2)
        slot_lvl = min(5, (lvl + 1) // 2)
        return {slot_lvl: count}
    return {}


def is_caster(occupation: str) -> bool:
    return bool(caster_kind(occupation))
