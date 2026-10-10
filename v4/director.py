"""v5 Director (0a): optimistic materialization of untracked props.

The v1 lesson: players experience responsiveness. When a player
interacts with something the narration implied but the engine never
tracked（木雕）, the DEFAULT is now YES — the Director materializes it
into the scene as a mechanically inert prop, the engine validates, the
ledger records the retcon.

The boundary stays engine-owned: anything with dice, value, or plot
weight (weapons, armor, potions, treasure, jewelry, scrolls, keys,
magic) is BLOCKED from materialization — those must come from the DM
or a plot beat. The LLM never writes state; it only prompted the
attempt by describing the world.
"""

import re

# mechanically meaningful / valuable / plot-bearing -> may NOT be
# conjured from thin air (conservative by design)
_BLOCKED = re.compile(
    r"劍|刀|弓|弩|斧|槌|錘|矛|槍|炮|匕|盾|甲|鎧|盔"
    r"|藥水|藥劑|靈藥|丹"
    r"|金幣|銀兩|黃金|銅板|寶石|珍珠|珠寶|玉|瑪瑙|翡翠"
    r"|戒指|項鍊|墜飾|護符"
    r"|捲軸|書卷|法典"
    r"|魔法|魔杖|法杖|法器|神器|詛咒"
    r"|鑰匙|鎖匙"
    r"|sword|blade|axe|bow|gun|rifle|spear|hammer|dagger"
    r"|armor|armour|shield|potion|wand|staff|rod"
    r"|coin|gold|gem|jewel|ring|amulet|necklace|scroll|key"
    r"|artifact",
    re.I)

# props nobody should be able to conjure by naming them
_NON_PROP = re.compile(r"空氣|牆|地板|天空|太陽|月亮|海|水$|^水")

MAX_NAME = 10  # a prop name, not an essay


def classify(name: str) -> str:
    """"prop" if the item is safe to materialize, "blocked" if it
    carries mechanical weight（dice/value/plot）."""
    t = (name or "").strip()
    if not t or len(t) > MAX_NAME or _NON_PROP.search(t):
        return "blocked"
    return "blocked" if _BLOCKED.search(t) else "prop"


def try_materialize(g, actor: str, name: str) -> bool:
    """Engine-gated materialization: add the prop to the CURRENT scene
    and ledger the retcon. Returns False when the item is blocked."""
    if classify(name) != "prop":
        return False
    s = g.world.here
    # already tracked? then this call was unnecessary (harmless no-op)
    if any(name == n or name in n or n in name for n, _ in
           s.ground_items + s.hidden_items):
        return True
    s.ground_items.append((name, 1))
    g.ledger.add("director", "materialize",
                 f"世界補上了 {name}（回應 {actor} 的互動）",
                 item=name, scene=g.world.current, player=actor)
    return True
