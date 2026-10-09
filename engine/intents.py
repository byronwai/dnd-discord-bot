"""Natural-language intent detection for meta-commands (English + 繁體中文).

When a player @mentions the bot with something like "roll a d20" or
"開新團：血月城堡", map it to the real command instead of feeding it to the
DM as story text. Anything unmatched stays story input — most tagged
messages ARE actions and belong to the story.

Conservative by design: a pattern must clearly be a meta-command, otherwise
it falls through to narration.
"""

import re

# roll / 擲骰: keyword + dice payload; validated by the dice engine afterwards
_ROLL_RE = re.compile(
    r"^(?:please\s+|幫我|請|幫)?\s*"
    r"(?:roll|throw|擲骰|擲|骰|投擲|投)\s*"
    r"(?:a\s+|an\s+|the\s+|一下|一個|个|個)?\s*"
    r"(?P<expr>[^\s，。！？!?].*)$",
    re.IGNORECASE,
)
# new adventure with optional setting
_NEW_RE = re.compile(
    r"^(?:new|start|begin)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?"
    r"(?:adventure|campaign|game|story)(?:[：:，,]?\s*(?P<arg>.+))?\s*[!！.。]*$",
    re.IGNORECASE,
)
_NEW_ZH_RE = re.compile(
    r"^(?:開始|來開|開)?(?:新冒險|新遊戲|新團|開新團|開團)"
    r"(?:[：:，,]?\s*(?P<arg>.+))?\s*[!！.。]*$"
)
_STATUS_RE = re.compile(r"^(?:status|game\s*status|狀態|遊戲狀態|戰況)\s*[!！.。]*$", re.IGNORECASE)
_PARTY_RE = re.compile(r"^(?:party|party\s*sheet|隊伍|隊伍一覽|隊伍列表|我方隊伍)\s*[!！.。]*$", re.IGNORECASE)
_CLASSES_RE = re.compile(r"^(?:classes|職業|職業一覽|職業列表|有哪些職業)\s*[?？!！.。]*$", re.IGNORECASE)
_INV_RE = re.compile(r"^(?:inv|inventory|items|物品|道具|物品欄|背包|物品一覽|道具一覽)\s*[?？!！.。]*$", re.IGNORECASE)
_ROLLSTATS_RE = re.compile(r"^(?:rollstats|擲屬性|擲屬性骰|屬性擲骰|屬性骰)\s*[?？!！.。]*$", re.IGNORECASE)
_HELP_RE = re.compile(r"^(?:help|commands?|指令|指令說明|說明|幫助)\s*[!！.。]*$", re.IGNORECASE)


def parse_intent(text: str) -> tuple[str, str] | None:
    """Return (command, arg) for clear meta-commands, else None.

    commands: roll, new, status, party, classes, inv, rollstats, help
    """
    t = (text or "").strip()
    if not t:
        return None

    # rollstats before the roll regex (which would swallow "rollstats")
    if _ROLLSTATS_RE.match(t):
        return ("rollstats", "")

    m = _ROLL_RE.match(t)
    if m:
        expr = m.group("expr").strip()
        # validate the payload is really dice — "roll down the hill" is a story
        from .dice import roll_expr
        try:
            roll_expr(expr)
            return ("roll", expr)
        except ValueError:
            return None

    m = _NEW_RE.match(t) or _NEW_ZH_RE.match(t)
    if m:
        return ("new", (m.groupdict().get("arg") or "").strip())

    if _STATUS_RE.match(t):
        return ("status", "")
    if _PARTY_RE.match(t):
        return ("party", "")
    if _CLASSES_RE.match(t):
        return ("classes", "")
    if _INV_RE.match(t):
        return ("inv", "")
    if _ROLLSTATS_RE.match(t):
        return ("rollstats", "")
    if _HELP_RE.match(t):
        return ("help", "")
    return None
