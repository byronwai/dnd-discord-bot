"""Rules retrieval v2: hybrid search over the SRD index (engine/compendium.py
covers the always-on core; this retrieves the long tail — specific spells,
monsters, items, class features, situational rules).

Improvements over v1 (which embedded the raw player message and did
cosine + title-bonus only):
- zh→en glossary: player messages are often Traditional Chinese while the
  SRD is English; a deterministic term map (no extra LLM call) appends the
  English rule vocabulary to the query before embedding.
- Query gating: consent replies ("骰", "roll"), option picks ("A", "ok") and
  other sub-4-char messages skip retrieval entirely — no embedding call.
- Hybrid scoring: cosine + title-token bonus + body-token overlap (computed
  only on the top cosine candidates, so it stays cheap) + a kind boost when
  the query clearly asks for a spell/item/monster.
- k=3 with (title, src) dedup and a wider 850-char window per chunk.

Schema v2 (tools/ingest_srd.py): chunks(id, title, path, kind, src, text, vec).
The old schema (title, text, vec) still loads — path/kind default to ''.
"""

import logging
import os
import re
import sqlite3
from collections import OrderedDict

import httpx
import numpy as np

log = logging.getLogger("rules")

QUERY_PREFIX = os.environ.get("EMBED_PREFIX",
    "Represent this sentence for searching relevant passages: ")
_TOKEN_RE = re.compile(r"[a-z0-9]{3,}")

# Deterministic zh→en rule vocabulary: matched anywhere in the query, the
# English terms are appended before embedding (kept — not replaced — since
# mxbai has partial cross-lingual ability).
_ZH_EN_GLOSSARY = [
    ("力量", "strength STR"), ("敏捷", "dexterity DEX"), ("體質", "constitution CON"),
    ("智力", "intelligence INT"), ("感知", "wisdom WIS"), ("魅力", "charisma CHA"),
    ("檢定", "ability check"), ("豁免", "saving throw"), ("先攻", "initiative"),
    ("優勢", "advantage"), ("劣勢", "disadvantage"), ("暴擊", "critical hit"),
    ("攻擊", "attack"), ("傷害", "damage"), ("治療", "healing"), ("恢復", "healing"),
    ("施法", "cast a spell"), ("施放", "cast a spell"), ("施展", "cast a spell"),
    ("法術", "spell"), ("咒語", "spell"),
    ("魔法飛彈", "magic missile"), ("火球", "fireball"), ("閃電", "lightning bolt"),
    ("治療真言", "healing word"), ("治癒", "cure wounds"), ("睡眠", "sleep spell"),
    ("藥水", "potion"), ("火把", "torch"), ("照明的", "light"),
    ("隱形", "invisibility"), ("變形", "wild shape shapeshift"),
    ("潛行", "stealth"), ("躲藏", "hide stealth"), ("隱匿", "stealth"),
    ("說服", "persuasion"), ("欺騙", "deception"), ("恐嚇", "intimidation"),
    ("調查", "investigation"), ("察覺", "perception"), ("醫藥", "medicine"),
    ("運動", "athletics"), ("特技", "acrobatics"), ("馴獸", "animal handling"),
    ("自然", "nature"), ("宗教", "religion"), ("歷史", "history"), ("奧秘", "arcana"),
    ("摔", "grapple"), ("抓住", "grapple"), ("推開", "shove"), ("撞開", "shove"),
    ("衝刺", "dash action"), ("防禦", "dodge"), ("撤離", "disengage"),
    ("協助", "help action"), ("準備動作", "ready action"), ("搜索", "search"),
    ("短休", "short rest"), ("長休", "long rest"), ("休息", "rest"),
    ("死亡豁免", "death saving throw"), ("昏迷", "unconscious"),
    ("瀕死", "death saving throw"), ("中毒", "poisoned"), ("麻痺", "paralyzed"),
    ("恐懼", "frightened"), ("目盲", "blinded"), ("倒地", "prone"),
    ("專注", "concentration"), ("掩護", "cover"), ("護甲", "armor"), ("盾", "shield"),
    ("武器", "weapon"), ("劍", "sword"), ("弓", "bow"), ("箭", "arrow"),
    ("怪物", "monster"), ("哥布林", "goblin"), ("龍", "dragon"), ("狼", "wolf"),
    ("蜘蛛", "spider"), ("僵屍", "zombie"), ("不死生物", "undead"),
    ("盜賊", "rogue sneak attack"), ("遊俠", "ranger"), ("法師", "wizard"),
    ("牧師", "cleric"), ("德魯伊", "druid"), ("野蠻人", "barbarian rage"),
    ("吟遊詩人", "bard"), ("聖武士", "paladin"), ("武僧", "monk"),
    ("術士", "sorcerer"), ("魔契師", "warlock"), ("戰士", "fighter"),
]

# query words that pull toward a chunk kind (bonus, not a filter)
_KIND_HINTS = {
    "spell": re.compile(r"spell|cast|cantrip|magic missile|fireball|hex|"
                        r"eldritch|wild shape|concentration", re.I),
    "item": re.compile(r"item|potion|armor|shield|weapon|sword|bow|arrow|"
                       r"torch|rope|gold|treasure|equipment|wand|scroll", re.I),
    "monster": re.compile(r"monster|goblin|dragon|wolf|spider|zombie|undead|"
                          r"skeleton|orc|kobold|bandit|stat block|cr [0-9]", re.I),
    "class": re.compile(r"barbarian|bard|cleric|druid|fighter|monk|paladin|"
                        r"ranger|rogue|sorcerer|warlock|wizard|class|level up|"
                        r"subclass|archetype|feat", re.I),
}


def _tokens(text: str) -> frozenset:
    return frozenset(_TOKEN_RE.findall(text.lower()))


def _overlap(a: frozenset, b: frozenset) -> int:
    """Token overlap with prefix credit: 'grapple' matches the title token
    'grappling' (exact intersection first; prefixes only if none)."""
    n = len(a & b)
    if n:
        return n
    return sum(1 for x in a for y in b
               if min(len(x), len(y)) >= 5
               and (x.startswith(y) or y.startswith(x)))


def _zh_to_en(query: str) -> str:
    """Append English rule vocabulary for Chinese terms found in the query."""
    extra = [en for zh, en in _ZH_EN_GLOSSARY if zh in query]
    return query + (" " + " ".join(dict.fromkeys(
        t for e in extra for t in e.split())) if extra else "")


_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")


def wants_rules(query: str) -> bool:
    """False for messages that can't benefit from rule lookup (consent
    replies, option picks, dice-only strings) — skips the embedding call.
    CJK is denser than Latin: 2+ Chinese characters already form a real
    query (火球術/哥布林 are 3-char spell & monster names)."""
    t = (query or "").strip()
    if not t:
        return False
    if t in ("骰", "骰！", "roll", "roll!", "Roll", "骰。"):
        return False
    if len(_CJK_CHAR_RE.findall(t)) >= 2:
        return True
    return len(t) >= 4


class RulesIndex:
    def __init__(self, db_path: str, embed_url: str, model: str = ""):
        self.embed_url = embed_url.rstrip("/")
        self.model = model
        self._vectors = None  # lazy: (mat, rows, title_toks)
        self._qcache: OrderedDict[str, np.ndarray] = OrderedDict()  # LRU
        self.db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    def _load(self):
        if self._vectors is not None:
            return
        cur = self.db.execute(
            "SELECT title, text, vec FROM chunks ORDER BY id")
        rows = cur.fetchall()
        if not rows:
            self._vectors = (np.zeros((0, 384), dtype=np.float32), [], [], [])
            return
        titles_texts = [(r[0], r[1]) for r in rows]
        mat = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32)
        mat = mat.reshape(len(rows), -1).copy()  # copy: frombuffer is read-only
        mat /= (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
        toks = [_tokens(t) for t, _txt in titles_texts]
        try:  # v2 schema only; v1 index files have no kind column
            kinds = [r[0] or "" for r in self.db.execute(
                "SELECT kind FROM chunks ORDER BY id").fetchall()]
        except sqlite3.OperationalError:
            kinds = [""] * len(rows)
        self._vectors = (mat, titles_texts, toks, kinds)

    def _embed(self, text: str) -> np.ndarray:
        hit = self._qcache.get(text)
        if hit is not None:
            self._qcache.move_to_end(text)
            return hit
        with httpx.Client(timeout=60.0) as client:
            body = {"input": text}
            if self.model:
                body["model"] = self.model
            resp = client.post(f"{self.embed_url}/v1/embeddings", json=body)
            resp.raise_for_status()
            vec = np.asarray(resp.json()["data"][0]["embedding"],
                             dtype=np.float32)
        vec /= (np.linalg.norm(vec) + 1e-9)
        self._qcache[text] = vec
        self._qcache.move_to_end(text)
        while len(self._qcache) > 64:
            self._qcache.popitem(last=False)
        return vec

    def search(self, query: str, k: int = 3, min_score: float = 0.75,
               max_chars: int = 850) -> list[dict]:
        """Up to k relevant SRD chunks ([] when gated, index empty or the
        embedder is down — the DM then leans on the core compendium)."""
        try:
            if not wants_rules(query):
                return []
            self._load()
            mat, titles_texts, title_toks, kinds = self._vectors
            if len(titles_texts) == 0:
                return []
            mapped = _zh_to_en(query)
            q = self._embed(QUERY_PREFIX + mapped)
            scores = mat @ q
            q_toks = _tokens(mapped)
            for i, ctoks in enumerate(title_toks):
                ov = _overlap(q_toks, ctoks)
                if ov:
                    scores[i] += min(0.30, 0.15 * ov)
            # body-token overlap on the top cosine candidates only (cheap):
            # discriminates "Fireball" the spell from "wand of fireballs"
            pre = np.argsort(scores)[::-1][:64]
            body_bonus = {}
            for i in pre:
                btoks = _tokens(titles_texts[i][1])
                ov = len(q_toks & btoks)
                if ov >= 3:
                    body_bonus[i] = min(0.12, 0.04 * (ov - 2))
            kinds_out = kinds  # loaded in _load (v2 schema) or all-'' (v1)
            kind_boost = {}
            for kind, rx in _KIND_HINTS.items():
                if rx.search(mapped):
                    kind_boost = {i: 0.05 for i, kd in enumerate(kinds_out)
                                  if kd == kind}
                    break
            for i in pre:
                scores[i] += body_bonus.get(i, 0.0) + kind_boost.get(i, 0.0)
            top = np.argsort(scores)[::-1][:k * 3]
            out, seen = [], set()
            for i in top:
                if scores[i] < min_score:
                    continue
                title, text = titles_texts[i]
                key = (kinds_out[i], title) if i < len(kinds_out) else (title,)
                if key in seen:
                    continue  # one entry per (kind, title): multi-part chunks
                seen.add(key)
                out.append({"title": title, "text": text[:max_chars],
                            "score": round(float(scores[i]), 3),
                            "kind": kinds_out[i] if i < len(kinds_out) else ""})
                if len(out) >= k:
                    break
            return out
        except Exception as e:
            log.warning("rules retrieval unavailable: %s", e)
            return []

    def chunks(self) -> int:
        try:
            self._load()
            return len(self._vectors[1])
        except Exception:
            return 0

    def format(self, results: list[dict]) -> str:
        if not results:
            return "(no specific entry retrieved — follow CORE MECHANICS)"
        parts = []
        for r in results:
            label = r["title"] + (f" ({r['kind']})" if r.get("kind") else "")
            parts.append(f"[SRD 5e · {label}]\n{r['text']}")
        return "\n\n".join(parts)
