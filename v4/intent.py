"""v4 intent layer: structured intents + the deterministic (no-LLM) parser.

The digestor LLM (P2) will produce the same Intent objects from free-form
text; the command syntax here is the degraded-mode / test-harness path and
the ground truth for the schema.
"""

from dataclasses import dataclass, field

# canonical action vocabulary the engine understands
ACTIONS = ("attack", "move", "use", "cast", "talk", "search",
           "rest", "give", "check", "pass", "creative", "meta")

_VERBS = {
    "attack": ("攻擊", "打", "attack", "hit"),
    "move": ("去", "前往", "走到", "go", "move"),
    "use": ("使用", "用", "use"),
    "cast": ("施展", "施法", "施放", "cast"),
    "talk": ("說", "喊", "講", "say", "talk", "shout"),
    "search": ("搜索", "搜尋", "調查", "檢查", "search"),
    "rest": ("休息", "rest"),
    "give": ("給", "give"),
    "check": ("檢定", "骰", "roll", "check"),
    "pass": ("等待", "跳過", "pass", "wait"),
}


@dataclass
class Intent:
    action: str
    actor: str = ""          # "" = the caller's / current acting character
    target: str = ""
    item: str = ""
    qty: int = 1
    spell: str = ""
    destination: str = ""
    utterance: str = ""
    ability: str = ""
    skill: str = ""
    raw: str = ""
    confidence: float = 1.0  # digestor fills this; low -> engine asks back
    args: dict = field(default_factory=dict)


def _starts_with_verb(text: str):
    """(action, rest) if text opens with a known verb, else None.
    Empty tails are fine — object-less verbs like 搜索/rest/pass are legal."""
    for action, words in _VERBS.items():
        for w in words:
            if text.startswith(w):
                return action, text[len(w):].lstrip(" ：:,，,")
    return None


def parse_command(text: str, party_names=()) -> Intent | None:
    """Deterministic keyword parser (degraded mode). Grammar:
        [角色] 動詞 賓語...
    e.g.「大力蕉 攻擊 哥布林①」「依思 使用 治療藥水」「go 酒窖」
    """
    t = (text or "").strip()
    if not t:
        return None
    actor = ""
    for name in party_names:
        if t.startswith(name):
            actor = name
            t = t[len(name):].lstrip(" ：:,，,")
            break
    hit = _starts_with_verb(t)
    if hit is None:
        return None
    action, rest = hit
    it = Intent(action=action, actor=actor, raw=text)
    if action == "move":
        it.destination = rest
    elif action == "talk":
        it.utterance = rest
    elif action in ("use", "give"):
        it.item = rest
    elif action == "cast":
        it.spell = rest
    elif action in ("attack",):
        it.target = rest
    elif action == "rest":
        it.args["kind"] = "long" if rest in ("長休", "long") else "short"
    elif action == "check":
        it.ability = rest
    return it
