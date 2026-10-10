"""v5 A1: item catalog + affordances.

Items carry typed capabilities (affordances) so creative actions map to
REAL mechanical effects instead of a generic check with no consequence:
throw anything (improvised attack / it lands somewhere), ignite
flammables (the scene catches fire — engine state that ticks damage).

Defaults are optimistic (v5 0a): anything unlisted can be thrown —
that's physics, not treasure. Weapons/valuables still only exist if
the DM or the Director's prop gate put them in the world.
"""

# affordance tags:
#   throwable — improvised ranged attack (1d4, DEX); lands in the scene
#               (or the sea) afterwards
#   flammable — can serve as tinder; burning it sets the scene on fire
#   light     — a fire source (torch): ignites things without burning up
#   drinkable — consumable
CATALOG = {
    "治療藥水": {"aff": ["drinkable"]},
    "火把": {"aff": ["light", "flammable", "throwable"]},
    "繩索": {"aff": ["throwable", "flammable"]},
    "匕首": {"aff": ["throwable", "pry"]},
    "長劍": {"aff": ["pry"]},
    "魚叉": {"aff": ["throwable"]},
    "木盾": {"aff": ["throwable"]},
    "草藥包": {"aff": ["drinkable"]},
    "木雕": {"aff": ["throwable", "flammable"]},
    "布條": {"aff": ["flammable", "throwable"]},
    "木牌": {"aff": ["flammable", "throwable"]},
    "船槳": {"aff": ["throwable", "flammable"]},
    "空瓶": {"aff": ["throwable"]},
    "木炭": {"aff": ["flammable", "throwable"]},
    "貝殼": {"aff": ["throwable"]},
    "石頭": {"aff": ["throwable"]},
}

# flammable by material — unlisted items made of these catch fire
_FLAMMATILE = ("木", "布", "繩", "紙", "草", "油", "帆", "coal", "炭")

_DEFAULT = {"throwable"}  # physics: anything can be thrown


def lookup(name: str) -> dict | None:
    """Catalog entry for an exact/fuzzy name, or None."""
    t = (name or "").strip()
    if not t:
        return None
    if t in CATALOG:
        return CATALOG[t]
    for k, v in CATALOG.items():
        if k in t or t in k:
            return v
    return None


def affordances(name: str) -> set:
    """Affordance tags for an item — optimistic defaults (v5 0a)."""
    entry = lookup(name)
    if entry:
        return set(entry.get("aff", ()))
    if any(m in (name or "") for m in _FLAMMATILE):
        return {"throwable", "flammable"}
    return set(_DEFAULT)
