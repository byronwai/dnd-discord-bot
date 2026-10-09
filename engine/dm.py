"""D&D DM engine: persistence, prompt building, LLM calls (streaming)."""

import asyncio
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass

import httpx

from .dice import resolve_llm_rolls, roll_expr
from .state import apply_state_tags
from .compendium import CORE_RULES
from . import checks as checks_mod

# words that suggest the narration changed game state without emitting tags
_STATE_HINT_RE = re.compile(
    r"HP|生命|血量|受傷|治療|恢復|場景|轉移|獲得|失去|撿到|拾獲|喝下|拿起|用掉|掉落|"
    r"damage|heal|heals|gains?|loses?|lost|picks? up|moves? to|enters?", re.IGNORECASE)

_EXTRACT_PROMPT = (
    "From this D&D narration, output ONLY state-change tags, one per line, no prose. "
    "Formats: [[hp:Name:-4]] damage, [[hp:Name:+5]] healing, [[hp:Name=cur/max]] "
    "absolute; [[item:Name:+item xN]] gained, [[item:Name:-item xN]] lost; "
    "[[scene:place]] if the party moved; [[objective:goal]] if the goal changed; "
    "[[xp:Name:100]] experience awarded. "
    "Use exact character names from the party sheet. If nothing changed, "
    "output nothing at all.\n\nNarration:\n"
)

# strips language labels small models sometimes echo at the start of a reply
_LANG_LABEL_RE = re.compile(
    r"^\s*(繁體中文|简体中文|Traditional Chinese|Simplified Chinese|English|中文|DM|遊戲主持人)\s*[:：\-—]*\s*"
)
_FAKE_DICE_RE = re.compile(
    r"\s*\*{0,2}(?:the )?(?:dice roll result|die result|roll result) is? \d{1,3}\*{0,2}"
    r"|\s*\[\[dice\]\]"
    r"|\brolls?(?:ed)? (?:a |an )?\d{1,2}\b(?!\s*(?:meters|metres|feet|ft|times|rounds))"
    r"|你(?:骰|擲)(?:出)?了?[:：]?\s*\d{1,3}|骰子?結果[:：]?\s*\d{1,3}"
    r"|(?:系統|我|你)(?:骰|擲)出[:：]?\s*\d{1,3}|(?:骰|擲)出(?:了)?[:：]?\s*\d{1,3}\s*點", re.IGNORECASE)
# bare "🎲 17" lines are the model imitating the engine's dice format; the
# real renders always carry the expression ("🎲 *d20* → **17**")
_FAKE_DICE_LINE_RE = re.compile(r"^[ \t]*🎲\s*\d{1,3}\s*$", re.MULTILINE)
# model-written roll arithmetic ("🎲 攻擊判定：18 + 5 = 23！命中！") — vibe
# dice; only the engine's canonical verdicts and 🎲 *expr* renders are real
_FAKE_VERDICT_RE = re.compile(
    r"^[ \t]*🎲?\s*(?:攻擊判定|擲骰結果|攻擊骰|檢定結果)[：:]\s*"
    r"\d{1,2}\s*\+\s*\d{1,2}\s*=\s*\d{1,2}[^\n]*$"
    r"|^[ \t]*🎲\s*\d{1,2}\s*\+\s*\d{1,2}\s*=\s*\d{1,2}[^\n]*$"
    # dice-arrow lines with NO breakdown ("🎲 1d20+5 → 17", "🎲 傷害骰
    # 1d6+3 → 5") — every real engine render ends with "(... breakdown)"
    r"|^[ \t]*🎲?\s*(?:傷害骰|攻擊骰|擲骰)?[ \t]*\d*d\d{1,3}"
    r"(?:[ \t]*[+-][ \t]*\d{1,3})*[ \t]*(?:→|->|=)[ \t]*\d{1,4}[ \t]*$",
    re.MULTILINE)
# SRD stat-block tables the model sometimes pastes whole — narration never
# needs markdown tables, so 2+ consecutive | rows are dropped
_TABLE_BLOCK_RE = re.compile(r"(?:^[ \t]*\|.*\|[ \t]*$\n?){2,}", re.MULTILINE)
# engine-rendered status echoes (❤️ HP lines etc.) inside PAST replies are
# stripped when history is replayed: leaving them in teaches the model to
# re-emit no-op tags every turn (the HP-spam feedback loop)
_STATUS_LINE_RE = re.compile(
    r"^[ \t]*\*\*(?:❤️|💚|💔|🎒|📍|🎯|✨)[^*]*\*\*[ \t]*$", re.MULTILINE)
# engine-only footer vocabulary (⏳ 等待…/🎯 待決檢定仍在等待…) — the model
# imitates these from replayed history (渲染即教材); strip from output AND
# from history so only the engine may ever state them
_ENGINE_FOOTER_RE = re.compile(
    r"^[ \t]*(?:⏳ 等待|🎯 待決檢定仍在等待)[^\n]*$", re.MULTILINE)
def _near_dup(a: str, b: str) -> bool:
    """Two assistant replies are 'the same narration' when their openings
    nearly match — the model's repetition death-spiral produces verbatim
    or near-verbatim repeats. Short replies are exempt: similarity ratios
    are meaningless on them and would false-positive normal dialogue."""
    from difflib import SequenceMatcher
    if len(a) < 100 or len(b) < 100:
        return False
    return SequenceMatcher(None, a[:400], b[:400]).ratio() > 0.85


_LATIN_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")
_GAME_TERMS = {"STR", "DEX", "CON", "INT", "WIS", "CHA", "AC", "DC", "HP",
               "PB", "XP", "SRD", "AoE", "NPC", "PC", "DM", "Lv", "Lvl",
               "roll", "Roll", "adv", "dis"}


def _latin_leaks(text: str) -> list:
    """Stray English words inside Chinese prose (家族傳 heirlooms).
    [[...]] tags are masked so the tag protocol never counts as a leak."""
    masked = re.sub(r"\[{2}.*?\]{2}", " ", text)
    return [t for t in _LATIN_TOKEN_RE.findall(masked) if t not in _GAME_TERMS]


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# input hardening: player text that tries to spoof engine channels. The
# placeholder tokens and verdict preamble are machine-only vocabulary; a
# player typing them is guessing or injecting, so they get neutralized and
# the table sees a visible warning.
_INJECTION_PATTERNS = [
    (re.compile(r"(?:\[\s*|\b)PC\s*\d{1,2}(?:\s*\]|\b)", re.IGNORECASE), "〔玩家角色〕"),
    (re.compile(r"SYSTEM\s*CHECK\s*VERDICT", re.IGNORECASE), "〔已過濾〕"),
    (re.compile(r"FINAL\s*[-—:]?\s*VERDICT", re.IGNORECASE), "〔已過濾〕"),
    (re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+"
                r"(?:instructions?|rules?|prompts?)", re.IGNORECASE), "〔filtered〕"),
    (re.compile(r"忽略(?:上面|以上|先前|之前|之前所有)的?(?:指示|指令|規則|設定|提示)"), "〔已過濾〕"),
    (re.compile(r"你現在是(?:一名)??(?:新的)?(?:AI|助手|DM|管理員)"), "〔已過濾〕"),
]


def scrub_player_input(text: str) -> tuple[str, bool]:
    """Neutralize placeholder/verdict/injection spoofing in player text.
    Returns (clean_text, was_scrubbed)."""
    t = text or ""
    changed = False
    for rx, rep in _INJECTION_PATTERNS:
        new = rx.sub(rep, t)
        if new != t:
            changed = True
            t = new
    return t, changed


def _clip_name(name: str, limit: int = 24) -> str:
    n = (name or "").strip()
    return n if len(n) <= limit else n[: limit - 1] + "…"


def _is_chinese(text: str) -> bool:
    """Chinese-dominant detection: CJK letters must outnumber Latin letters
    (single stray Chinese words in English sentences don't count)."""
    if not text:
        return False
    return len(_CJK_RE.findall(text)) > len(_LATIN_RE.findall(text))

try:  # optional: force Traditional Chinese output (pure-python package)
    from opencc import OpenCC
    _s2t = OpenCC("s2t").convert
except ImportError:  # pragma: no cover
    _s2t = None

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    party      TEXT NOT NULL DEFAULT '{}',
    summary    TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (platform, chat_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL,
    chat_id   TEXT NOT NULL,
    role      TEXT NOT NULL,
    name      TEXT NOT NULL DEFAULT '',
    user_id   TEXT NOT NULL DEFAULT '',
    content   TEXT NOT NULL,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (platform, chat_id, id);
CREATE TABLE IF NOT EXISTS backups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    party      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    ts         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    platform  TEXT NOT NULL,
    chat_id   TEXT NOT NULL,
    char_name TEXT NOT NULL,
    kind      TEXT NOT NULL DEFAULT 'item',  -- 'item' or 'equipment'
    name      TEXT NOT NULL,
    qty       INTEGER NOT NULL DEFAULT 1,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_char ON items (platform, chat_id, char_name);
CREATE TABLE IF NOT EXISTS npcs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id  TEXT NOT NULL,
    name     TEXT NOT NULL,
    desc     TEXT NOT NULL DEFAULT '',
    status   TEXT NOT NULL DEFAULT '',
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npcs_chat ON npcs (platform, chat_id, name);
CREATE TABLE IF NOT EXISTS lore (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id  TEXT NOT NULL,
    key      TEXT NOT NULL,
    fact     TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lore_chat ON lore (platform, chat_id, key);
CREATE TABLE IF NOT EXISTS char_bonds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    chat_id     TEXT NOT NULL,
    char_name   TEXT NOT NULL,
    player_name TEXT NOT NULL,
    player_id   TEXT NOT NULL,
    action      TEXT NOT NULL DEFAULT 'bind',  -- bind|rebind|update|unbind
    ts          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bonds_chat ON char_bonds (platform, chat_id);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    chat_id  TEXT NOT NULL,
    msg_id   INTEGER NOT NULL DEFAULT 0,  -- the assistant message it summarizes
    line     TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_chat ON events (platform, chat_id, msg_id);
"""

SYSTEM_PROMPT = """You are the Dungeon Master (DM) for a tabletop Dungeons & Dragons 5e game played over chat.

Your style:
- Vivid but concise narration: 80-180 words per turn. Use sensory detail, keep momentum.
- Play all NPCs with distinct voices.
- CHARACTER OWNERSHIP: every player controls ONLY the character(s) listed with them
  as owner in the party sheet. NEVER narrate, decide, or describe the actions, words,
  or thoughts of any player's character other than the current speaker's own. The
  only exception: the owner has explicitly stated in this session that another
  player may control that character.
- Track the party state given below (HP, inventory, locations) and stay consistent with it.
- CHECKS NEED PLAYER CONSENT: for any ability check, attack roll, or saving
  throw, NEVER roll it yourself. Copy CharacterName EXACTLY as written
  in the party sheet (Chinese characters as-is, never translate or
  romanize names). Instead, on its own line, emit
  [[check:DC|CharacterName|ABILITY]] with the DC you choose (5e scale: 10 easy,
  15 medium, 20 hard) and briefly state what success/failure means. The system
  shows the player a check card and waits; if they consent, the server rolls
  d20 + the character's real ability modifier and hands you the verdict.
  Narrate the outcome EXACTLY per that verdict, then apply [[hp:...]] /
  [[item:...]] effects. Inline [[expr]] auto-rolls are ONLY for damage, healing
  or loot AFTER success is already decided — never for checks.
- All dice are rolled by the bot: when you write [[dice]] the system rolls it and
  shows the real result. NEVER invent, predict, or calculate dice outcomes yourself,
  and NEVER do arithmetic in your narration (totals, HP, damage sums, gold) — the
  system performs every calculation. Only write the [[expression]].
- STATE TRACKING: the party sheet shows each character's current/max HP and inventory.
  When your narration changes state, emit a tag on its own line and the system applies
  it to the database for real:
  [[hp:Name:-4]] or [[hp:Name=-4]] damage, [[hp:Name:+5]] healing,
  [[hp:Name=12/12]] set absolute current/max
  [[item:Name:+healing potion x1]] gained, [[item:Name:-torch x2]] lost/consumed
  [[scene:tavern cellar]] when the party moves, [[objective:find the missing fishermen]]
  when the goal changes, [[xp:Name:100]] when you award experience (milestone or combat). Use character names exactly as in the party sheet. Never
  narrate a state change without its tag; never emit tags for unchanged state.
- CANONICAL FACTS: the PARTY SHEET below is the single source of truth. Each
  character's class/occupation listed there is FINAL — narrate 大力蕉 as a Druid and
  依思 as a Warlock if so listed. NEVER rename or reinterpret a character's class,
  race, or owned items; if unsure, read the sheet again.
- ABSOLUTE BAN: never write any sentence stating a dice result yourself — "The dice
  roll result is N", "she rolled a 14", 你骰了N. The ONLY dice that exist are the
  system-rendered 🎲 lines. Every check/attack/save MUST go through [[check:...]]
  consent; you may never resolve an uncertain action by narrating its outcome
  directly. If you catch yourself writing a number as a roll, stop and emit a
  check tag instead.
- NAMES: use each character's name EXACTLY as written in the party sheet (Chinese
  characters stay Chinese) in ALL narration — never translate or romanize names.
- CONTINUITY (critical): dice results already shown in the conversation are FINAL —
  never re-roll the same check, never ask a player to roll again, and never resolve
  the same event twice with different outcomes. If the player's reply affirms a
  pending action ("yes", "I eat it", "吃"), resolve it immediately and move the story
  forward this turn. Never stall by re-asking a question they already answered.
- MOMENTUM: end EVERY turn with a hook - a question, a looming choice, or 2-3 concrete
  suggested actions in parentheses (beginners need hints). Point at interactable details
  in the environment and useful items in the party's inventory. Give NPCs names and
  voices, foreshadow the next danger, and reward creative play.
- On death/danger, follow 5e conventions (death saves at 0 HP, etc.); at 0 HP the
  character falls unconscious and must make death saving throws.
- If a player writes in Chinese, reply in Traditional Chinese (繁體中文), never
  Simplified. If they write in English, reply in English.
- The RULES REFERENCE is in English: when narrating in Chinese, still follow it
  and explain its mechanics in Traditional Chinese.
- Never mention that you are an AI or an LLM. You are the DM.

RULES REFERENCE (D&D 5e SRD). The CORE MECHANICS section is always in force;
the SRD LOOKUP entries were retrieved for this turn's situation — use what is
relevant, ignore the rest, never contradict either. When a rule here is
relevant, ground your mechanics in it explicitly — name the exact check,
DC, or contested roll the rule specifies:
{rules}

PARTY SHEET (owner = the player who controls that character):
{party}

ADVENTURE LOG (story so far):
{summary}

FINAL INSTRUCTION: Always write your narration in the language of the player's
last message. If the player writes Chinese, answer entirely in Traditional
Chinese characters (繁體中文, never Simplified). If the player writes English,
answer in English. The per-message directive above this section states the
required language for this turn — always obey it. Never answer a Chinese
message in English or vice versa."""

# Full Chinese system prompt: small models answer in the language of the
# system prompt, so Chinese turns get this instead of the English one.
SYSTEM_PROMPT_ZH = """你是一場以聊天進行的《龍與地下城》5e桌上角色扮演遊戲的地下城主（DM）。

你的風格：
- 生動但精簡的敘述：每回合 80 至 150 字（系統會截斷更長的輸出——在字數內自然收尾）。用感官細節營造氣氛，保持節奏。
- 用鮮明的聲音扮演所有 NPC。
- 角色所有權：每位玩家只能控制派對表中所有者為自己的角色。絕不敘述、決定或描述
  其他玩家的角色的行動、台詞或想法。唯一例外：該角色的所有者已在本團中明確聲明
  由該玩家代管。
- 追蹤下方隊伍狀態（HP、物品、位置），保持前後一致。
- 判定分工（關鍵——駭客分權，各有專屬標記）：
  · 屬性檢定：[[check:DC|角色名|屬性]]；可指定技能 [[check:13|角色名|WIS 洞悉]]，
    系統自動加熟練加值。需要玩家同意（檢定卡）。
  · 豁免骰：[[save:DC|角色名|屬性]]（系統自動加職業豁免熟練）。需要玩家同意。
  · 攻擊骰：[[attack:角色名|目標|+5]]——與檢定同節奏：系統顯示攻擊卡，玩家
    回覆「骰」後，伺服器對目標 AC 擲骰並公告命中/暴擊，你再敘述後果與傷害
    （[[1d8+3]]）。+5 也可寫 STR/DEX/敏捷 由系統計算（含熟練加值）。目標 AC
    來自 [[enemy:...]] 登記、玩家角色（系統已知）或明寫「目標 AC 15」。
    戰鬥爆發時先發 [[combat:...]]，再為敵人發 [[enemy:...]]，然後才攻擊。
    攻擊卡只能由 [[attack:...]] 標記產生——絕不自己手寫卡面、宣布骰值、
    或在同一回公告攻擊結果；等玩家「骰」之後的系統判定才算數。
    玩家有 `/attack <目標>` 指令（系統直接擲骰判定）——每當戰鬥開始、
    或你邀請/建議玩家攻擊時，必須明確寫出這句提示：
    「用 `/attack <目標>` 發起攻擊（系統會擲骰判定）」。
    玩家已用 /attack 的回合，直接依系統判定敘述，不再另發攻擊卡。
  DC 依下方「難度方針」選擇。行內 [[骰式]] 自動擲骰僅用於傷害、治療或戰利品。
  傷害與治療的數字一律來自 [[骰式]]（系統擲骰）——絕不自己寫死任何傷害數字。
  檢定／豁免卡出現後，玩家回覆「骰」，伺服器擲 d20＋完整修正值（含熟練），
  判定即事實——你必須完全依照判定敘述結果，再套用 [[hp]]/[[item]] 效果。
  發出檢定／豁免／攻擊卡後，本回合就停在「等待該角色擲骰」——結尾絕不
  把行動權交給其他玩家（不可再寫「XX，輪到你了」給別的角色）；判定結束
  後才繼續推進劇情或換人。
  結尾括號中的「建議行動」只是提示選項：玩家尚未親自宣告前，絕不替玩家
  執行這些行動，更不得敘述其成敗——等玩家自己說了才算數。
- 所有骰子由系統擲出：你寫出 [[骰式]] 後系統會顯示真實結果。絕不自行編造、
  預測或計算骰子結果，也絕不在敘述中做任何算術（總和、HP、傷害、金錢）——
  所有計算都由伺服器上的程式完成，你只寫出 [[骰式]]。
- 狀態追蹤：隊伍表顯示每個角色的目前／最大 HP 與物品。當你的敘述改變狀態時，
  自成一行發出標記，系統會真實寫入資料庫：
  [[hp:名字:-4]] 或 [[hp:名字=-4]] 傷害、[[hp:名字:+5]] 治療、[[hp:名字=12/12]] 直接設定
  [[item:名字:+治療藥水 x1]] 獲得、[[item:名字:-火把 x2]] 失去或消耗
  [[scene:酒窖]] 場景轉移、[[objective:找出失蹤漁夫]] 目標變更。
  [[combat:哥布林 x2、狼]] 戰鬥開始（寫敵人描述與數量，系統會為全體擲先攻並
  顯示順序）；[[combat:end]] 戰鬥結束。
  [[enemy:哥布林①|HP 7|AC 15]] 為每個參戰敵人登記（戰鬥開始後立刻逐一發出；
  之後 [[hp:哥布林①:-4]] 就能扣敵人血，敵人全滅時系統自動結束戰鬥）。
  [[spell:角色:1]] 施展法術（消耗法術格；0=戲法免費；法術格不足會顯示失敗，
  請據實敘述施法失敗或改用其他行動）。
  [[rest:short]]／[[rest:long]] 隊伍休息（生命骰、HP、法術格由系統結算）。
  [[npc:瑪莎|酒館老闆娘|friendly]] 記錄或更新 NPC（有名字的角色首次出現、
  或身分/態度改變時發；狀態如 friendly/hostile/dead/失蹤）。
  [[lore:海妖女王|族人被困在海底祭壇]] 記錄確立的世界事實（重要劇情線索、
  地名、組織、秘密；有新進展時更新同一條）。
  你的筆記會持續注入你的記憶——重複出現的 NPC 名字與既有事實必須一致。
  筆記習慣（重要）：任何有名字的 NPC 一登場就立刻發 [[npc:...]]（這是
  你的紙上筆記）；任何確立的重要世界事實（線索、地名、組織、秘密、
  承諾、交易）立刻發 [[lore:...]]。沒寫進筆記的細節，你就不該記得。
  標記裡的角色一律寫代號（[PC1]、[PC2]…，與隊伍表相同）。狀態改變必伴隨標記；
  沒有改變就不要發標記。
- 正典事實：下方隊伍表是唯一事實來源。表中每個角色的職業是定案——例如
  大力蕉是德魯伊（Druid）、依思是術士（Warlock）時就必須如此敘述。
  絕不改動或重新詮釋角色的職業、種族或持有物；不確定時重讀隊伍表。
- 絕對禁止：絕不親自寫出任何骰子結果句——「The dice roll result is N」「她骰了
  14」「你擲出 N」都不可以。唯一存在的骰子是系統顯示的 🎲 行。所有檢定、攻擊、
  豁免一律透過 [[check:...]] 徵求玩家同意；絕不直接敘述不確定行動的成敗結果。
- 名字忠實（代號制）：玩家角色一律以隊伍表中的代號 [PC1]、[PC2]… 稱呼。
  代號必須逐字複製（含方括號），絕不翻譯、音譯、縮寫或自創名字——系統會
  自動把代號換回角色真名。你不會知道真名，也不需要知道。NPC 可自行命名，
  但絕不把代號當成名字來發音或變形。
- 連貫性（關鍵）：對話中已出現的骰子結果就是最終結果——絕不重骰同一個檢定、
  絕不要求玩家再骰一次、同一事件絕不得出兩種不同結局。若玩家的回覆是對待決
  行動的肯定（「吃」、「好」、「我做」），立即結算並在本回合把劇情推進。
  絕不反覆追問玩家已回答過的問題。
- 推進動力：每一回合結尾都要有鉤子——一個問題、一個迫近的抉擇，或 2-3 個
  具體的建議行動（放括號內，新手需要提示）。指出環境中可互動的細節與隊伍
  物品欄裡可用的東西。NPC 要有名字與個性，鋪陳下一個危機，獎勵有創意的玩法。
{difficulty}
- 涉及生死時遵循 5e 規則；HP 歸零時角色昏迷並需進行死亡豁免。昏迷（HP 0）的角色絕不行動、施法或攻擊——只敘述其死亡豁免與同伴的救治（系統也會擋下其行動）。
- 主要語言（本桌政策）：所有輸出一律以繁體中文為主——敘述、檢定說明、選項、
  標記渲染都是繁體中文。只有當玩家「該回合」以英文輸入時，才在繁體中文敘述的
  結尾額外加一行簡短英文摘要（1-2 句，點出關鍵資訊或行動選項）。其餘情況
  絕不使用英文長段敘述。
- 規則參考為英文資料，請依其機制裁定，但敘述與解釋一律使用繁體中文。
  檢索到的怪物資料僅供你裁決（AC、HP、攻擊加值）——絕不把屬性表、
  stat block 或任何表格貼進敘述；玩家只需要故事與後果。

規則參考（取自 D&D 5e SRD）。CORE MECHANICS 段落恆常生效；SRD LOOKUP 條目
是為本回合情況檢索的——與當前情況相關才使用，其餘忽略，且不得與其矛盾。
相關時請明確依規則裁定——寫出規則指定的檢定、DC 或對抗骰：
{rules}

隊伍資料（JSON，玩家建立角色前可能為空）：
{party}

冒險日誌（目前為止的劇情）：
{summary}

最後指示：全程使用繁體中文（正體字，絕不使用簡體字）敘述與回應。"""

# Difficulty policies injected into the system prompt ({difficulty}); the
# table picks one at /new time and it persists per session.
DIFFICULTY_POLICIES = {
    "easy": """- 難度方針（新手友善，優先於你的其他偏好）：
  · DC 偏低：多數檢定 8-13，15 已是高難度，16+ 只留給玩家自找的高風險炫技。
  · 失敗不卡關：檢定失敗時製造「新的情況」（線索、轉機、小麻煩），絕不讓
    主線依賴單一檢定成功；玩家卡住時主動給提示或替代路線。
  · 戰鬥偏寬鬆：敵人數量與強度壓低（有張力但能打贏）；敵人有士氣、會潰逃
    或接受談判，不追擊倒地角色；有人瀕死時給挽回契機（NPC 支援、敵人轉移
    目標）。
  · 多給正面回饋：獎勵創意解法，讓新手覺得自己的選擇很聰明；XP 給得大方。""",
    "normal": """- 難度方針（標準 5e）：
  · DC 依 5e 標準：10 簡單、13 中等、15 困難、18 極難。
  · 失敗有後果（受傷、時間、線索損失），但主線通常有替代路線；卡關一段
    時間後給提示。
  · 戰鬥按遭遇原設計強度進行；敵人行動合理（會用戰術但不特別集火）。
  · XP 依 5e 標準發放。""",
    "hard": """- 難度方針（困難模式，優先於你的其他偏好）：
  · DC 偏高：多數檢定 13-18；敵人狡猾，會設陷阱、會集火弱者。
  · 失敗有真實代價：資源消耗、重傷、被俘、任務失敗都可能——但絕不無預警
    即死；致命危險事先有跡可循，玩家永遠有撤退或另尋他路的選項。
  · 戰鬥致命：敵人數量與戰術佔上風、會追擊、可能攻擊倒地者；先制與地形
    很重要。
  · 資源緊張：能安全休息的機會少；XP 給得謹慎，獎勵事前計畫與創意。""",
}


@dataclass
class StreamEvent:
    text: str = ""      # accumulated text so far
    done: bool = False


class DMEngine:
    def __init__(self, llm_url: str, model: str, data_dir: str, max_history: int = 24,
                 max_tokens: int = 400, rules_index=None):
        self.llm_url = llm_url.rstrip("/")
        self.model = model
        self.max_history = max_history
        self.max_tokens = max_tokens
        # narration hard cap: latency ∝ generated tokens; ~150 字 ceiling
        self.narr_tokens = max(120, int(os.environ.get("DM_NARR_TOKENS", "400")))
        self.rules_index = rules_index  # engine.rules.RulesIndex or None
        # context graph: how many recent messages ride verbatim; older turns
        # enter the prompt as one-line event records (engine/events table)
        self.raw_window = max(4, int(os.environ.get("DM_RAW_WINDOW", "10")))
        os.makedirs(data_dir, exist_ok=True)
        self.db = sqlite3.connect(os.path.join(data_dir, "campaign.db"))
        self.db.executescript(DB_SCHEMA)
        for alter in ("ALTER TABLE backups ADD COLUMN items TEXT NOT NULL DEFAULT '[]'",
                      "ALTER TABLE sessions ADD COLUMN scene TEXT NOT NULL DEFAULT ''",
                      "ALTER TABLE sessions ADD COLUMN objective TEXT NOT NULL DEFAULT ''",
                      "ALTER TABLE messages ADD COLUMN user_id TEXT NOT NULL DEFAULT ''",
                      "ALTER TABLE sessions ADD COLUMN pending_check TEXT NOT NULL DEFAULT ''",
                      "ALTER TABLE sessions ADD COLUMN combat TEXT NOT NULL DEFAULT ''",
                      "ALTER TABLE sessions ADD COLUMN slots TEXT NOT NULL DEFAULT '{}'",
                      "ALTER TABLE sessions ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'easy'",
                      "ALTER TABLE sessions ADD COLUMN deferred TEXT NOT NULL DEFAULT '[]'"):
            try:
                self.db.execute(alter)
            except sqlite3.OperationalError:
                pass
        self.db.commit()
        # One LLM request at a time (single 3B model on the Pi).
        self._llm_lock = asyncio.Lock()
        # Prevent two adapters from interleaving turns in the same chat.
        self._chat_locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ---------- persistence ----------

    def _session(self, platform: str, chat_id: str) -> dict:
        cur = self.db.execute(
            "SELECT party, summary, scene, objective, difficulty FROM sessions "
            "WHERE platform=? AND chat_id=?",
            (platform, chat_id),
        )
        row = cur.fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO sessions (platform, chat_id, created_at, updated_at) VALUES (?,?,?,?)",
                (platform, chat_id, time.time(), time.time()),
            )
            self.db.commit()
            return {"party": {}, "summary": "", "scene": "", "objective": "",
                    "difficulty": "easy"}
        return {"party": json.loads(row[0]), "summary": row[1],
                "scene": row[2] or "", "objective": row[3] or "",
                "difficulty": row[4] or "easy"}

    def set_difficulty(self, platform: str, chat_id: str, difficulty: str) -> str:
        """Set the table's difficulty (easy/normal/hard); returns the key."""
        if difficulty not in DIFFICULTY_POLICIES:
            difficulty = "easy"
        self.db.execute(
            "UPDATE sessions SET difficulty=?, updated_at=? WHERE platform=? AND chat_id=?",
            (difficulty, time.time(), platform, chat_id))
        self.db.commit()
        return difficulty

    def get_difficulty(self, platform: str, chat_id: str) -> str:
        return self._session(platform, chat_id)["difficulty"]

    def _save_session(self, platform: str, chat_id: str, party: dict, summary: str):
        self.db.execute(
            "UPDATE sessions SET party=?, summary=?, updated_at=? WHERE platform=? AND chat_id=?",
            (json.dumps(party, ensure_ascii=False), summary, time.time(), platform, chat_id),
        )
        self.db.commit()

    def _add_msg(self, platform: str, chat_id: str, role: str, name: str,
                 content: str, user_id: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO messages (platform, chat_id, role, name, user_id, content, ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (platform, chat_id, role, name, str(user_id or ""), content, time.time()),
        )
        self.db.commit()
        return cur.lastrowid

    def _history(self, platform: str, chat_id: str, limit: int) -> list[dict]:
        cur = self.db.execute(
            "SELECT id, role, name, content FROM messages WHERE platform=? AND chat_id=? "
            "ORDER BY id DESC LIMIT ?",
            (platform, chat_id, limit),
        )
        rows = list(reversed(cur.fetchall()))
        return [{"id": i, "role": r, "name": n, "content": c}
                for i, r, n, c in rows]

    # ---------- commands ----------

    def set_party(self, platform: str, chat_id: str, party: dict):
        s = self._session(platform, chat_id)
        s["party"] = party
        self._save_session(platform, chat_id, s["party"], s["summary"])

    def get_party(self, platform: str, chat_id: str) -> dict:
        return self._session(platform, chat_id)["party"]

    def party_text(self, platform: str, chat_id: str) -> str:
        from . import charlib
        party = self.get_party(platform, chat_id)
        if not party:
            return "(no party yet)"
        lines = []
        for name, val in party.items():
            if isinstance(val, dict):
                bits = []
                if val.get("occupation"):
                    lvl = f" Lv{val['level']}" if val.get("level") else ""
                    bits.append(f"[{val['occupation']}{lvl}]")
                try:
                    ac = int(val.get("ac") or
                             charlib.default_ac(val.get("occupation", ""),
                                                val.get("stats") or {}))
                    bits.append(f"AC {ac}")
                except (TypeError, ValueError):
                    pass
                if val.get("level"):
                    bits.append(f"PB {charlib.prof_bonus(int(val['level'])):+d}")
                if isinstance(val.get("stats"), dict):
                    bits.append(" ".join(f"{k} {v}"
                                         for k, v in val["stats"].items()))
                if val.get("details"):
                    bits.append(val["details"])
                lines.append(f"{name} (owner: {val.get('owner', '?')}): "
                             + " · ".join(bits))
                if isinstance(val.get("hp_now"), int):
                    lines.append(f"  HP {val['hp_now']}/{val.get('hp_max', '?')}")
                cur, mx = self.char_slots(val)
                if mx:
                    lines.append("  法術格 " + " · ".join(
                        f"L{k} {cur.get(k, 0)}/{v}" for k, v in mx.items()))
                inv = self.inv_text(platform, chat_id, name)
                if inv:
                    lines.append(f"  {inv}")
            else:
                lines.append(f"{name}: {val}")
        return "\n".join(lines)

    # ---------- inventory (道具 / 裝備) ----------

    def inv_add(self, platform: str, chat_id: str, char: str, name: str,
                qty: int = 1, kind: str = "item") -> None:
        # guard against tag residue leaking in as an item name
        name = re.sub(r"\[|\]", "", name or "")
        name = re.sub(r"^[+\-\s*,，]+", "", name).strip()
        name = name.split("+", 1)[0].strip(" ,，")
        name = name.strip()[:60]
        qty = max(1, min(int(qty or 1), 999))
        row = self.db.execute(
            "SELECT id, qty FROM items WHERE platform=? AND chat_id=? AND "
            "char_name=? AND name=? AND kind=?",
            (platform, chat_id, char, name, kind)).fetchone()
        if row:
            self.db.execute("UPDATE items SET qty=?, ts=? WHERE id=?",
                            (min(row[1] + qty, 999), time.time(), row[0]))
        else:
            self.db.execute(
                "INSERT INTO items (platform, chat_id, char_name, kind, name, qty, ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (platform, chat_id, char, kind, name, qty, time.time()))
        self.db.commit()

    def inv_remove(self, platform: str, chat_id: str, char: str, name: str,
                   qty: int = 1) -> tuple[bool, str]:
        """Remove qty of an item; returns (ok, message)."""
        row = self.db.execute(
            "SELECT id, qty, kind FROM items WHERE platform=? AND chat_id=? AND "
            "char_name=? AND name=?",
            (platform, chat_id, char, name.strip())).fetchone()
        if row is None:
            return False, f"'{name}' not in {char}'s inventory"
        remaining = row[1] - max(1, int(qty or 1))
        if remaining > 0:
            self.db.execute("UPDATE items SET qty=? WHERE id=?",
                            (remaining, row[0]))
            self.db.commit()
            return True, f"{name} ×{row[1]} → ×{remaining}"
        self.db.execute("DELETE FROM items WHERE id=?", (row[0],))
        self.db.commit()
        return True, f"{name} removed"

    def inv_equip(self, platform: str, chat_id: str, char: str, name: str,
                  equip: bool = True) -> bool:
        cur = self.db.execute(
            "UPDATE items SET kind=? WHERE platform=? AND chat_id=? AND "
            "char_name=? AND name=?",
            ("equipment" if equip else "item", platform, chat_id,
             char, name.strip()))
        self.db.commit()
        return cur.rowcount > 0

    def inv_list(self, platform: str, chat_id: str,
                 char: str | None = None) -> dict:
        """{char: [(kind, name, qty), ...]} for one char or the whole party."""
        if char:
            rows = self.db.execute(
                "SELECT kind, name, qty FROM items WHERE platform=? AND chat_id=? "
                "AND char_name=? ORDER BY kind DESC, name",
                (platform, chat_id, char)).fetchall()
            return {char: rows} if rows else {}
        rows = self.db.execute(
            "SELECT char_name, kind, name, qty FROM items WHERE platform=? AND "
            "chat_id=? ORDER BY char_name, kind DESC, name",
            (platform, chat_id)).fetchall()
        out: dict[str, list] = {}
        for cn, kind, name, qty in rows:
            out.setdefault(cn, []).append((kind, name, qty))
        return out

    def inv_text(self, platform: str, chat_id: str, char: str) -> str:
        inv = self.inv_list(platform, chat_id, char)
        entries = inv.get(char, [])
        if not entries:
            return ""
        parts = []
        for kind, name, qty in entries:
            icon = "⚔️" if kind == "equipment" else "🎒"
            parts.append(f"{icon}{name}×{qty}" if qty > 1 else f"{icon}{name}")
        return ", ".join(parts)

    def inv_all_text(self, platform: str, chat_id: str) -> str:
        inv = self.inv_list(platform, chat_id)
        if not inv:
            return "(empty / 沒有物品)"
        lines = []
        for cn, entries in inv.items():
            parts = []
            for kind, name, qty in entries:
                icon = "⚔️" if kind == "equipment" else "🎒"
                parts.append(f"{icon}{name}×{qty}" if qty > 1 else f"{icon}{name}")
            lines.append(f"{cn}: " + ", ".join(parts))
        return "\n".join(lines)

    # ---------- DM notebook: NPCs & lore ("on paper" in the DB) ----------

    def npc_upsert(self, platform: str, chat_id: str, name: str,
                   desc: str = "", status: str = "") -> bool:
        """Insert/update an NPC note. Returns True when something changed
        (callers render only then — no-change notes stay silent)."""
        row = self.db.execute(
            "SELECT id, desc, status FROM npcs WHERE platform=? AND chat_id=? "
            "AND name=?", (platform, chat_id, name)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO npcs (platform, chat_id, name, desc, status, ts) "
                "VALUES (?,?,?,?,?,?)",
                (platform, chat_id, name, desc, status, time.time()))
            self.db.commit()
            return True
        nid, old_desc, old_status = row
        nd = desc.strip() or old_desc
        ns = status.strip() or old_status
        if nd == old_desc and ns == old_status:
            return False
        self.db.execute("UPDATE npcs SET desc=?, status=?, ts=? WHERE id=?",
                        (nd, ns, time.time(), nid))
        self.db.commit()
        return True

    def npc_list(self, platform: str, chat_id: str,
                 limit: int = 15) -> list[dict]:
        return [{"name": r[0], "desc": r[1], "status": r[2]} for r in
                self.db.execute(
                    "SELECT name, desc, status FROM npcs WHERE platform=? AND "
                    "chat_id=? ORDER BY ts DESC LIMIT ?",
                    (platform, chat_id, limit)).fetchall()]

    def lore_upsert(self, platform: str, chat_id: str, key: str,
                    fact: str) -> bool:
        row = self.db.execute(
            "SELECT id, fact FROM lore WHERE platform=? AND chat_id=? AND key=?",
            (platform, chat_id, key)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO lore (platform, chat_id, key, fact, ts) "
                "VALUES (?,?,?,?,?)",
                (platform, chat_id, key, fact, time.time()))
            self.db.commit()
            return True
        if row[1] == fact:
            return False
        self.db.execute("UPDATE lore SET fact=?, ts=? WHERE id=?",
                        (fact, time.time(), row[0]))
        self.db.commit()
        return True

    def lore_list(self, platform: str, chat_id: str,
                  limit: int = 15) -> list[dict]:
        return [{"key": r[0], "fact": r[1]} for r in self.db.execute(
            "SELECT key, fact FROM lore WHERE platform=? AND chat_id=? "
            "ORDER BY ts DESC LIMIT ?", (platform, chat_id, limit)).fetchall()]

    # ---------- deferred enemy hits (un-narrated retaliation queue) ----------

    def queue_deferred(self, platform: str, chat_id: str, char: str,
                       delta: int) -> None:
        row = self.db.execute(
            "SELECT deferred FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        try:
            q = json.loads(row[0]) if row and row[0] else []
        except ValueError:
            q = []
        q.append([char, int(delta)])
        self.db.execute(
            "UPDATE sessions SET deferred=? WHERE platform=? AND chat_id=?",
            (json.dumps(q, ensure_ascii=False), platform, chat_id))
        self.db.commit()

    def pop_deferred(self, platform: str, chat_id: str) -> list[tuple[str, int]]:
        row = self.db.execute(
            "SELECT deferred FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        try:
            q = json.loads(row[0]) if row and row[0] else []
        except ValueError:
            q = []
        if q:
            self.db.execute(
                "UPDATE sessions SET deferred='[]' WHERE platform=? AND chat_id=?",
                (platform, chat_id))
            self.db.commit()
        return [(c, int(d)) for c, d in q]

    # ---------- context graph: distilled turn events (the edges) ----------
    # One deterministic line per turn (actor + action + engine-applied state
    # changes), so the prompt carries STRUCTURED FACTS for old turns instead
    # of their full text. No extra LLM call: composed from the state log.

    def add_event(self, platform: str, chat_id: str, msg_id: int,
                  line: str) -> None:
        self.db.execute(
            "INSERT INTO events (platform, chat_id, msg_id, line, ts) "
            "VALUES (?,?,?,?,?)",
            (platform, chat_id, int(msg_id), line[:280], time.time()))
        self.db.commit()

    def events_before(self, platform: str, chat_id: str, msg_id: int,
                      limit: int = 30) -> list[str]:
        """Event lines for turns older than msg_id (the raw-text window).
        Compression v2: consecutive events are distilled into scene blocks —
        one line per stretch of story, keeping the union of engine facts.
        Single-event stretches pass through verbatim; nothing is lost from
        the ledger (the events table itself is untouched)."""
        rows = [r[0] for r in self.db.execute(
            "SELECT line FROM events WHERE platform=? AND chat_id=? "
            "AND msg_id < ? ORDER BY id DESC LIMIT ?",
            (platform, chat_id, int(msg_id), limit * 2)).fetchall()][::-1]
        if not rows:
            return []
        blocks: list[list[str]] = [[]]
        for line in rows:
            boundary = ("場景：" in line or "COMBAT" in line
                        or line.startswith("系統"))
            if boundary and blocks[-1]:
                blocks.append([])
            blocks[-1].append(line)
        out = []
        for block in blocks:
            if not block:
                continue
            if len(block) == 1:
                out.append(block[0])
                continue
            facts: list[str] = []
            for line in block:
                parts = line.split("｜", 1)
                for f in ((parts[1] if len(parts) > 1 else "").split("；")):
                    f = f.strip()
                    if f and f not in facts:
                        facts.append(f)
            head = block[0].split("｜")[0][:38]
            out.append(f"▪ {head}…（{len(block)} 回合）｜"
                       + "；".join(facts[:6]))
        return out[-limit:]

    def touch_notes(self, platform: str, chat_id: str, text: str) -> None:
        """Recency walk: npc/lore nodes mentioned in recent text float to the
        top of the prompt's note sections (injection is recency-capped)."""
        for tbl, col in (("npcs", "name"), ("lore", "key")):
            for (name,) in self.db.execute(
                    f"SELECT {col} FROM {tbl} WHERE platform=? AND chat_id=?",
                    (platform, chat_id)).fetchall():
                if name and name in text:
                    self.db.execute(
                        f"UPDATE {tbl} SET ts=? WHERE platform=? AND chat_id=? "
                        f"AND {col}=?", (time.time(), platform, chat_id, name))
        self.db.commit()

    # ---------- player-character bond registry (audit trail) ----------

    def log_bond(self, platform: str, chat_id: str, char: str,
                 player_name: str, player_id, action: str = "bind") -> None:
        """Record a bond change: bind (created), rebind (owner changed,
        e.g. admin assignment), update (same owner, sheet updated),
        unbind (character removed)."""
        self.db.execute(
            "INSERT INTO char_bonds (platform, chat_id, char_name, "
            "player_name, player_id, action, ts) VALUES (?,?,?,?,?,?,?)",
            (platform, chat_id, char, player_name or "?",
             str(player_id or ""), action, time.time()))
        self.db.commit()

    def bond_log(self, platform: str, chat_id: str,
                 limit: int = 10) -> list[dict]:
        return [{"char": r[0], "player": r[1], "pid": r[2], "action": r[3],
                 "ts": r[4]} for r in self.db.execute(
            "SELECT char_name, player_name, player_id, action, ts "
            "FROM char_bonds WHERE platform=? AND chat_id=? "
            "ORDER BY id DESC LIMIT ?", (platform, chat_id, limit)).fetchall()]

    def _inv_snapshot(self, platform: str, chat_id: str) -> str:
        return json.dumps(self.inv_list(platform, chat_id), ensure_ascii=False)

    def session_info(self, platform: str, chat_id: str) -> dict:
        cur = self.db.execute(
            "SELECT created_at, updated_at, summary FROM sessions "
            "WHERE platform=? AND chat_id=?", (platform, chat_id))
        row = cur.fetchone()
        if row is None:
            return {"created_at": None, "updated_at": None,
                    "summary": "", "messages": 0, "difficulty": "easy"}
        count = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()[0]
        beats = self.db.execute(
            "SELECT content FROM messages WHERE platform=? AND chat_id=? "
            "AND role='assistant' ORDER BY id DESC LIMIT 2",
            (platform, chat_id)).fetchall()
        srow = self.db.execute(
            "SELECT scene, objective, difficulty FROM sessions "
            "WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        return {"created_at": row[0], "updated_at": row[1],
                "summary": row[2] or "", "messages": count,
                "scene": srow[0] if srow else "",
                "objective": srow[1] if srow else "",
                "difficulty": (srow[2] if srow else None) or "easy",
                "beats": [b[0] for b in reversed(beats)]}

    def reset(self, platform: str, chat_id: str, keep_party: bool = False):
        """Clear the session. keep_party=True (used by /new) keeps characters
        and their inventory; the previous state is always backed up first."""
        cur = self.db.execute(
            "SELECT party, summary FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        if cur and json.loads(cur[0]):
            self.db.execute(
                "INSERT INTO backups (platform, chat_id, party, summary, ts, items) "
                "VALUES (?,?,?,?,?,?)",
                (platform, chat_id, cur[0], cur[1], time.time(),
                 self._inv_snapshot(platform, chat_id)))
            self.db.commit()
        self.db.execute(
            "DELETE FROM messages WHERE platform=? AND chat_id=?", (platform, chat_id)
        )
        # a new adventure is a new world: the DM's notebook starts blank
        self.db.execute("DELETE FROM npcs WHERE platform=? AND chat_id=?",
                        (platform, chat_id))
        self.db.execute("DELETE FROM lore WHERE platform=? AND chat_id=?",
                        (platform, chat_id))
        self.db.execute("DELETE FROM events WHERE platform=? AND chat_id=?",
                        (platform, chat_id))
        if not keep_party:
            self.db.execute(
                "DELETE FROM items WHERE platform=? AND chat_id=?",
                (platform, chat_id))
            self.db.execute(
                "UPDATE sessions SET scene='', objective='', slots='{}' "
                "WHERE platform=? AND chat_id=?", (platform, chat_id))
            self.db.commit()
        # stale combat order / consent checks never survive a reset or /new
        self.db.execute(
            "UPDATE sessions SET combat='', pending_check='' "
            "WHERE platform=? AND chat_id=?", (platform, chat_id))
        self.db.commit()
        # new adventure, same characters and inventory: clear story only
        self._save_session(platform, chat_id,
                           json.loads(cur[0]) if (cur and keep_party) else {}, "")

    # ---------- backups ----------

    def last_backup(self, platform: str, chat_id: str) -> dict | None:
        """Most recent backup for this chat, if any."""
        row = self.db.execute(
            "SELECT party, summary, ts FROM backups WHERE platform=? AND chat_id=? "
            "ORDER BY id DESC LIMIT 1", (platform, chat_id)).fetchone()
        if row is None:
            return None
        return {"party": json.loads(row[0]), "summary": row[1], "ts": row[2]}

    def restore_backup(self, platform: str, chat_id: str) -> bool:
        """Restore party, items and summary from the most recent backup."""
        row = self.db.execute(
            "SELECT party, summary FROM backups WHERE platform=? AND chat_id=? "
            "ORDER BY id DESC LIMIT 1", (platform, chat_id)).fetchone()
        if row is None:
            return False
        self._save_session(platform, chat_id, json.loads(row[0]), row[1])
        try:  # inventory snapshot (column exists post-ALTER)
            irow = self.db.execute(
                "SELECT items FROM backups WHERE platform=? AND chat_id=? "
                "ORDER BY id DESC LIMIT 1", (platform, chat_id)).fetchone()
            if irow and irow[0] and irow[0] != "[]":
                self.db.execute(
                    "DELETE FROM items WHERE platform=? AND chat_id=?",
                    (platform, chat_id))
                for cn, entries in json.loads(irow[0]).items():
                    for kind, name, qty in entries:
                        self.db.execute(
                            "INSERT INTO items (platform, chat_id, char_name, "
                            "kind, name, qty, ts) VALUES (?,?,?,?,?,?,?)",
                            (platform, chat_id, cn, kind, name, int(qty),
                             time.time()))
                self.db.commit()
        except (sqlite3.OperationalError, json.JSONDecodeError):
            pass
        return True

    # ---------- combat turn order (1 player 1 move) ----------

    def combat_start(self, platform: str, chat_id: str,
                     chars: list[str],
                     enemies: list[tuple[str, int]] | None = None) -> list[dict]:
        """Roll initiative (d20 + DEX mod) per character and set the order.
        enemies: optional [(name, dex_mod)] NPC slots from [[combat:...]]."""
        party = self.get_party(platform, chat_id)
        order = []
        for name in chars:
            dex = int((party.get(name, {}) or {}).get("stats", {}).get("DEX", 10))
            order.append({"name": name,
                          "init": random.randint(1, 20) + (dex - 10) // 2,
                          "npc": False})
        for ename, mod in (enemies or []):
            order.append({"name": ename, "init": random.randint(1, 20) + mod,
                          "npc": True})
        order.sort(key=lambda o: (-o["init"], o.get("npc", False)))
        state = {"order": order, "idx": 0, "round": 1,
                 "since": time.time(), "votes": []}
        self.db.execute(
            "UPDATE sessions SET combat=?, updated_at=? WHERE platform=? AND chat_id=?",
            (json.dumps(state, ensure_ascii=False), time.time(), platform, chat_id))
        self.db.commit()
        return order

    # ---- tag-driven combat: [[combat:哥布林 x2、狼]] / [[combat:end]] ----

    _ENEMY_SPLIT_RE = re.compile(r"[、,，;/]| and ", re.IGNORECASE)
    _ENEMY_COUNT_RE = re.compile(r"[x×*]?\s*(\d{1,2})\s*(?:隻|名|个|個)?\s*$")

    @classmethod
    def _parse_enemy_desc(cls, desc: str) -> list[tuple[str, int]]:
        """'哥布林 x3、狼' / '2 goblins' -> named slots (DEX 10 → +0)."""
        out: list[tuple[str, int]] = []
        for part in cls._ENEMY_SPLIT_RE.split(desc or ""):
            p = part.strip()
            if not p:
                continue
            count = 1
            m = re.match(r"^(\d{1,2})\s*(?:隻|名|个|個)?\s*(.+)$", p)  # 2 goblins
            if m:
                count, p = int(m.group(1)), m.group(2)
            else:
                m = cls._ENEMY_COUNT_RE.search(p)  # 哥布林 x3
                if m:
                    count = int(m.group(1))
                    p = p[:m.start()]
            name = re.sub(r"[x×*\s]+$", "", p).strip() or "敵人"
            count = max(1, min(12, count))
            for i in range(1, count + 1):
                label = name if count == 1 else \
                    f"{name}{chr(0x2460 + i - 1) if i <= 10 else i}"
                out.append((label[:30], 0))
        return out[:8]  # cap: chat tables don't need more slots

    def combat_auto_start(self, platform: str, chat_id: str,
                          desc: str) -> list[dict] | None:
        """[[combat:...]] tag handler: roll initiative for party + enemies."""
        party = self.get_party(platform, chat_id)
        chars = [n for n, v in party.items() if isinstance(v, dict)]
        if not chars:
            return None
        return self.combat_start(platform, chat_id, chars,
                                 self._parse_enemy_desc(desc))

    def _combat_save(self, platform: str, chat_id: str, state: dict) -> None:
        self.db.execute(
            "UPDATE sessions SET combat=?, updated_at=? WHERE platform=? AND chat_id=?",
            (json.dumps(state, ensure_ascii=False), time.time(), platform, chat_id))
        self.db.commit()

    def combat_get(self, platform: str, chat_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT combat FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def combat_current(self, platform: str, chat_id: str) -> dict | None:
        st = self.combat_get(platform, chat_id)
        if not st or not st.get("order"):
            return None
        return st["order"][st["idx"] % len(st["order"])]

    def combat_advance(self, platform: str, chat_id: str) -> dict | None:
        st = self.combat_get(platform, chat_id)
        if not st or not st.get("order"):
            return None
        st["idx"] += 1
        if st["idx"] >= len(st["order"]):
            st["idx"] = 0
            st["round"] = st.get("round", 1) + 1
        st["since"] = time.time()
        st["votes"] = []
        self._combat_save(platform, chat_id, st)
        return st["order"][st["idx"]]

    def _combat_set_idx(self, platform: str, chat_id: str, st: dict,
                        idx: int) -> None:
        """Jump the marker to idx (wrapping rounds) — used after a successful
        combat turn so a failed LLM call never burns a player's move."""
        if idx >= len(st["order"]):
            idx = 0
            st["round"] = st.get("round", 1) + 1
        st["idx"] = idx
        st["since"] = time.time()
        st["votes"] = []
        self._combat_save(platform, chat_id, st)

    @staticmethod
    def combat_act_window(st: dict) -> tuple[list[str], dict | None]:
        """From the current marker, skip consecutive NPC slots (their turns
        get compressed into the next player's reply). Returns (npc_names,
        acting_pc_slot)."""
        order = st.get("order") or []
        idx = st.get("idx", 0) % len(order) if order else 0
        npcs: list[str] = []
        while idx < len(order) and order[idx].get("npc"):
            npcs.append(order[idx]["name"])
            idx += 1
        return npcs, (order[idx] if idx < len(order) else None)

    def combat_end(self, platform: str, chat_id: str) -> bool:
        cur = self.combat_get(platform, chat_id)
        self.db.execute(
            "UPDATE sessions SET combat='', updated_at=? WHERE platform=? AND chat_id=?",
            (time.time(), platform, chat_id))
        self.db.commit()
        return cur is not None

    def combat_skip_vote(self, platform: str, chat_id: str,
                         user_id: str) -> tuple[bool, str]:
        """Vote to skip the current stalled turn; majority advances."""
        st = self.combat_get(platform, chat_id)
        if not st or not st.get("order"):
            return False, "no active combat"
        cur = st["order"][st["idx"] % len(st["order"])]["name"]
        if user_id in st.get("votes", []):
            return True, f"you already voted to skip {cur}"
        st.setdefault("votes", []).append(user_id)
        need = max(1, (len(st["order"]) + 1) // 2)
        if len(st["votes"]) >= need:
            nxt = self.combat_advance(platform, chat_id)
            return True, (f"⏭ {cur}'s turn skipped by vote "
                          f"({len(st['votes']) if st else '?'}/{need}) — "
                          f"now {nxt['name']}'s turn, round {st['round']}")
        self._combat_save(platform, chat_id, st)
        return True, f"🗳 vote {len(st['votes'])}/{need} to skip {cur}'s turn"

    def combat_find(self, platform: str, chat_id: str,
                    name: str) -> tuple[dict | None, dict | None]:
        """Find an NPC combat slot by (fuzzy) name. Returns (slot, state)."""
        st = self.combat_get(platform, chat_id)
        if not st:
            return None, None
        n = (name or "").strip()
        for o in st.get("order", []):
            if o.get("npc") and n and (n == o["name"] or n in o["name"]
                                       or o["name"] in n):
                return o, st
        return None, None

    # ---------- character math (AC / slots / rests) ----------

    def char_slots(self, entry: dict, _char: str = "") -> tuple[dict, dict]:
        """(current, max) slot dicts for an entry; lazily initialized full
        (pre-slot characters were implicitly always full)."""
        from . import charlib
        occ = (entry or {}).get("occupation", "")
        lvl = int((entry or {}).get("level", 1) or 1)
        mx = {str(k): v for k, v in charlib.slots_for(occ, lvl).items()}
        cur = entry.get("slots")
        if not isinstance(cur, dict):
            cur = dict(mx)
            entry["slots"] = cur
        return cur, mx

    def do_rest(self, platform: str, chat_id: str, kind: str) -> str:
        """Short rest (spend 1 hit die per character) or long rest (full HP,
        slots, half of hit dice back). Returns the rendered result."""
        import random as _r
        from . import charlib
        from .commands import HIT_DICE
        party = self.get_party(platform, chat_id)
        lines = ["🌿 **短休 Short Rest**（各花 1 顆生命骰）" if kind == "short"
                 else "🌙 **長休 Long Rest**（HP 全滿、法術格恢復、生命骰回復一半）"]
        for name, entry in party.items():
            if not isinstance(entry, dict):
                continue
            occ = entry.get("occupation", "")
            lvl = int(entry.get("level", 1) or 1)
            hp_max = int(entry.get("hp_max", 0) or 0)
            con = (int((entry.get("stats") or {}).get("CON", 10)) - 10) // 2
            hd_used = int(entry.get("hd_used", 0) or 0)
            if kind == "short":
                if hd_used < lvl and hp_max:
                    die = HIT_DICE.get(occ, 8)
                    heal = max(1, _r.randint(1, die) + con)
                    hp_now = min(hp_max, int(entry.get("hp_now", hp_max)) + heal)
                    entry["hp_now"] = hp_now
                    entry["hd_used"] = hd_used + 1
                    lines.append(f"· {name}：生命骰 d{die}{con:+d} → HP "
                                 f"{hp_now}/{hp_max}（生命骰剩 {lvl - hd_used - 1}/{lvl}）")
                else:
                    lines.append(f"· {name}：生命骰已用盡，短休僅恢復喘息")
            else:
                if hp_max:
                    entry["hp_now"] = hp_max
                entry["hd_used"] = max(0, hd_used - max(1, lvl // 2))
                self.char_slots(entry)
                cur, mx = entry["slots"], {str(k): v for k, v in
                                           charlib.slots_for(occ, lvl).items()}
                entry["slots"] = dict(mx)
                slot_txt = (f"，法術格 {'·'.join(f'L{k} {v}' for k, v in mx.items())}"
                            if mx else "")
                lines.append(f"· {name}：HP {hp_max}/{hp_max}{slot_txt}"
                             f"（生命骰 {lvl - entry['hd_used']}/{lvl}）")
        self.set_party(platform, chat_id, party)
        return "\n".join(lines)

    # ---------- consent-based checks ----------

    def get_pending_check(self, platform: str, chat_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT pending_check FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        if row and row[0]:
            try:
                return json.loads(row[0])
            except Exception:
                return None
        return None

    def set_pending_check(self, platform: str, chat_id: str,
                          check: dict | None) -> None:
        self.db.execute(
            "UPDATE sessions SET pending_check=?, updated_at=? "
            "WHERE platform=? AND chat_id=?",
            (json.dumps(check, ensure_ascii=False) if check else "",
             time.time(), platform, chat_id))
        self.db.commit()

    def resolve_check(self, platform: str, chat_id: str,
                      d20: int | None = None) -> dict:
        """Roll the pending check: d20 + full modifier (ability + proficiency
        when proficient) vs DC. `d20` forces the check die — used when the
        die was already rolled elsewhere (/roll-admin feeding in the roll)."""
        ck = self.get_pending_check(platform, chat_id)
        party = self.get_party(platform, chat_id)
        entry = party.get(ck.get("char", ""), {})
        kind = ck.get("kind", "check")
        if kind == "attack":
            # d20 + attack bonus vs the target's AC; crits on nat 20
            bonus = int(ck.get("bonus", 0))
            ac = int(ck.get("ac", 13))
            d20_roll = max(1, min(20, int(d20))) if d20 is not None \
                else random.randint(1, 20)
            total = d20_roll + bonus
            hit = d20_roll >= 20 or (d20_roll > 1 and total >= ac)
            verdict = {"kind": "attack", "char": ck.get("char", "?"),
                       "target": ck.get("target", "?"), "bonus": bonus,
                       "ac": ac, "dc": ac, "d20": d20_roll, "mod": bonus,
                       "total": total, "success": hit,
                       "crit": d20_roll if d20_roll in (1, 20) else None,
                       "ability": "", "skill": ""}
            self.set_pending_check(platform, chat_id, None)
            return verdict
        ability = ck.get("ability", "")
        skill = ck.get("skill", "")
        mod = checks_mod.total_mod(entry if isinstance(entry, dict) else {},
                                   ability, skill, kind)
        d20_roll = max(1, min(20, int(d20))) if d20 is not None \
            else random.randint(1, 20)
        total = d20_roll + mod
        dc = int(ck.get("dc", 10))
        if d20_roll >= 20:
            success = True
        elif d20_roll <= 1:
            success = False
        else:
            success = total >= dc
        verdict = {"char": ck.get("char", "?"), "ability": ability, "dc": dc,
                   "d20": d20_roll, "mod": mod, "total": total, "success": success,
                   "crit": d20_roll if d20_roll in (1, 20) else None,
                   "kind": kind, "skill": skill}
        self.set_pending_check(platform, chat_id, None)
        return verdict

    # SRD signature attacks per class: (move name, ability, damage dice, aoe)
    # — the moves list every character gets in /attack's dropdown
    CLASS_ATTACKS = {
        "Barbarian": [("手斧劈擊", "STR", "1d6", False),
                      ("狂暴巨斧", "STR", "1d12", False)],
        "Bard": [("細劍突刺", "DEX", "1d8", False),
                 ("惡毒嘲諷", "CHA", "1d4", False)],
        "Cleric": [("戰錘重擊", "STR", "1d8", False),
                   ("聖火術", "WIS", "1d8", False)],
        "Druid": [("詛咒木杖", "WIS", "1d8", False),
                  ("荊棘鞭打", "WIS", "1d6", False),
                  ("月光束（範圍）", "WIS", "2d10", True)],
        "Fighter": [("長劍斬擊", "STR", "1d8", False),
                    ("盾擊", "STR", "1d4", False)],
        "Monk": [("徒手連擊", "DEX", "1d4", False),
                 ("飛鏢", "DEX", "1d4", False)],
        "Paladin": [("長劍斬擊", "STR", "1d8", False),
                    ("神聖痛擊", "STR", "2d8", False)],
        "Ranger": [("短弓射擊", "DEX", "1d6", False),
                   ("雙匕首", "DEX", "1d4", False)],
        "Rogue": [("短劍背刺", "DEX", "1d6", False),
                  ("匕首投擲", "DEX", "1d4", False)],
        "Sorcerer": [("火焰箭", "CHA", "1d10", False),
                     ("灼熱之手（範圍）", "CHA", "3d6", True)],
        "Warlock": [("魔能爆", "CHA", "1d10", False),
                    ("地獄斥喝", "CHA", "2d10", False)],
        "Wizard": [("火焰箭", "INT", "1d10", False),
                   ("燃燒之手（範圍）", "INT", "3d6", True)],
    }

    # slot level required for spell-based moves (absent/0 = not a spell):
    # /attack gates them by the character's granted slot levels AND spends
    # a slot on use, matching the [[spell:]] economy
    MOVE_SPELL_LEVEL = {"月光束（範圍）": 2, "地獄斥喝": 1, "灼熱之手（範圍）": 1,
                        "燃燒之手（範圍）": 1, "神聖痛擊": 1}

    def attack_moves(self, platform: str, chat_id: str,
                     char: str) -> list[dict]:
        """Moves available to a character: class signature attacks + any
        carried equipment (improvised, STR/1d6). Deterministic, no LLM.
        Leveled-spell moves only appear when the character's class table
        actually grants slots of that level (a Lv2 Druid has no 月光束)."""
        party = self.get_party(platform, chat_id)
        entry = party.get(char) if isinstance(party.get(char), dict) else {}
        _cur, mx = self.char_slots(entry, char)
        max_slot = max((int(k) for k, v in mx.items() if v), default=0)
        inv = self.inv_list(platform, chat_id, char).get(char, [])
        return compute_attack_moves(entry.get("occupation", ""),
                                    int(entry.get("level", 1) or 1),
                                    inv, max_slot)

    def player_attack(self, platform: str, chat_id: str, char: str,
                      target: str, ability: str = "",
                      bonus: int | None = None,
                      aoe: bool = False,
                      move: str = "") -> tuple[dict | None, str]:
        """/attack: the player initiates, the engine rolls immediately —
        ONE roll vs the chosen target. `move` picks a moveset entry (sets
        ability, damage dice and AoE); aoe=True splashes damage to every
        enemy regardless."""
        from . import charlib
        party = self.get_party(platform, chat_id)
        entry = party.get(char)
        if not isinstance(entry, dict):
            return None, f"找不到角色「{char}」"
        if entry.get("hp_now") is not None and int(entry["hp_now"]) <= 0:
            return None, (f"{char} 已倒地（HP 0）——昏迷中只能進行死亡豁免，"
                          "無法行動！請同伴救治（治療藥水／法術）後再戰。")
        tname = (target or "").strip()
        ac = None
        m = re.search(r"AC\s*(\d{1,2})$", tname, re.IGNORECASE)
        if m:
            ac = int(m.group(1))
            tname = tname[:m.start()].strip()
        slot, st = self.combat_find(platform, chat_id, tname)
        tgt_char = None
        if slot is None and ac is None:
            for n in party:
                if tname and (tname == n or tname in n or n in tname):
                    tgt_char = n
                    break
            if tgt_char:
                ent2 = party[tgt_char]
                try:
                    ac = int(ent2.get("ac") or charlib.default_ac(
                        ent2.get("occupation", ""), ent2.get("stats") or {}))
                except (TypeError, ValueError):
                    ac = 13
        if ac is None and slot is not None:
            ac = slot.get("ac")
        if ac is None and slot is not None:
            # the slot exists but the DM never registered it with
            # [[enemy:...|HP|AC]] — fall back to SRD-ish defaults so the
            # attack still resolves (and so damage can't one-shot it)
            ac = 13
            slot["ac"] = 13
            if slot.get("hp") is None:
                slot["hp"] = 11
                slot["hp_max"] = 11
            self._combat_save(platform, chat_id, st)
        if ac is None:
            st = st or self.combat_get(platform, chat_id)
            foes = [o["name"] for o in (st or {}).get("order", [])
                    if o.get("npc")] if st else []
            hint = (f"；目前敵人：{'、'.join(foes)}" if foes else
                    "（也可寫『AC 15』直接指定）")
            return None, f"找不到目標「{tname or '?'}」{hint}"
        if slot is not None:
            tname = slot["name"]
        elif tgt_char:
            tname = tgt_char
        dmg = ""
        if move:
            mv = next((m for m in self.attack_moves(platform, chat_id, char)
                       if m["name"] == move), None)
            if mv is not None:
                ability = ability or mv["ability"]
                aoe = aoe or mv["aoe"]
                dmg = mv["dmg"]
                # leveled-spell moves pay a spell slot, same as [[spell:]]
                lvl_req = self.MOVE_SPELL_LEVEL.get(move, 0)
                if lvl_req > 0:
                    cur, _mx = self.char_slots(entry, char)
                    have = int(cur.get(str(lvl_req), 0))
                    if have <= 0:
                        return None, (f"{char} 的 {lvl_req} 環法術格已用盡"
                                      "——改用戲法、其他招式，或先休息！")
                    cur[str(lvl_req)] = have - 1
                    self.set_party(platform, chat_id, party)
        if bonus is None:
            bonus = checks_mod.total_mod(entry, ability or "STR", kind="attack")
        d20 = random.randint(1, 20)
        total = d20 + bonus
        hit = d20 >= 20 or (d20 > 1 and total >= ac)
        crit = d20 >= 20
        verdict = {"kind": "attack", "char": char, "target": tname,
                   "bonus": bonus, "ac": ac, "dc": ac, "d20": d20,
                   "mod": bonus, "total": total, "success": hit,
                   "crit": d20 if d20 in (1, 20) else None,
                   "ability": ability, "skill": "", "aoe": bool(aoe),
                   "move": move, "dmg": dmg}
        # engine rolls the damage too: dice shown individually, then the
        # total; crits double the pool; AoE splashes to every enemy
        if dmg and (hit or aoe):
            t1, d1 = roll_expr(dmg)
            if crit:
                t2, d2 = roll_expr(dmg)
                dmg_total, dmg_detail = t1 + t2, f"{d1}＋{d2}（暴擊骰池×2）"
            else:
                dmg_total, dmg_detail = t1, d1
            verdict["dmg_total"] = dmg_total
            verdict["dmg_detail"] = dmg_detail
            applied = []
            if aoe and st is not None:
                targets = [o for o in st.get("order", []) if o.get("npc")]
            elif slot is not None:
                targets = [slot]
            else:
                targets = []
            for o in targets:
                o["hp"] = max(0, int(o.get("hp") or 0) - dmg_total)
                if o["hp"] <= 0:
                    o["dead"] = True
                applied.append((o["name"], o["hp"], o.get("hp_max", "?"),
                                o["hp"] <= 0))
            if targets:
                self._combat_save(platform, chat_id, st)
                verdict["applied"] = applied
                npcs = [o for o in st.get("order", []) if o.get("npc")]
                if npcs and all(o.get("hp", 1) <= 0 or o.get("dead")
                                for o in npcs):
                    self.combat_end(platform, chat_id)
                    verdict["combat_end"] = True
            elif tgt_char is not None:
                p2 = self.get_party(platform, chat_id)
                ent2 = p2.get(tgt_char)
                if isinstance(ent2, dict) and "hp_now" in ent2:
                    ent2["hp_now"] = max(0, int(ent2["hp_now"]) - dmg_total)
                    self.set_party(platform, chat_id, p2)
                    verdict["applied"] = [(tgt_char, ent2["hp_now"],
                                           ent2["hp_max"],
                                           ent2["hp_now"] <= 0)]
        return verdict, ""

    def update_game_state(self, platform: str, chat_id: str,
                          scene: str | None = None, objective: str | None = None):
        if scene is not None:
            self.db.execute("UPDATE sessions SET scene=?, updated_at=? "
                            "WHERE platform=? AND chat_id=?",
                            (scene[:120], time.time(), platform, chat_id))
        if objective is not None:
            self.db.execute("UPDATE sessions SET objective=?, updated_at=? "
                            "WHERE platform=? AND chat_id=?",
                            (objective[:120], time.time(), platform, chat_id))
        self.db.commit()

    def log_message(self, platform: str, chat_id: str, name: str, content: str,
                    user_id: str = ""):
        """Store a message in the session without triggering a DM reply
        (used for table talk the DM should remember but not answer)."""
        self._add_msg(platform, chat_id, "user", name, content, user_id)

    # ---------- name placeholders (structural anti-rename) ----------

    def _slots(self, platform: str, chat_id: str) -> dict[str, int]:
        """Stable {char name: slot number}. New characters get the next free
        slot; removed characters keep their number reserved so history never
        re-maps. This is the single source of placeholder identity."""
        row = self.db.execute(
            "SELECT slots FROM sessions WHERE platform=? AND chat_id=?",
            (platform, chat_id)).fetchone()
        slots: dict[str, int] = {}
        if row and row[0]:
            try:
                slots = {k: int(v) for k, v in json.loads(row[0]).items()}
            except (ValueError, TypeError):
                slots = {}
        party = self.get_party(platform, chat_id)
        changed = False
        for n in party:
            if n not in slots:
                slots[n] = (max(slots.values()) + 1) if slots else 1
                changed = True
        if changed or not row or not row[0]:
            self.db.execute(
                "UPDATE sessions SET slots=? WHERE platform=? AND chat_id=?",
                (json.dumps(slots, ensure_ascii=False), platform, chat_id))
            self.db.commit()
        return slots

    def _name_map(self, platform: str, chat_id: str) -> dict[str, str]:
        """{real name or alias: [PCn]} longest-first, for prompt-side swaps."""
        slots = self._slots(platform, chat_id)
        party = self.get_party(platform, chat_id)
        m: dict[str, str] = {}
        for name, slot in slots.items():
            tok = f"[PC{slot}]"
            m[name] = tok
            entry = party.get(name)
            if isinstance(entry, dict):
                for a in entry.get("aliases") or []:
                    if a and a != name:
                        m[a] = tok
        return dict(sorted(m.items(), key=lambda kv: -len(kv[0])))

    def _map_text(self, text: str, mapping: dict[str, str]) -> str:
        for name, tok in mapping.items():
            text = text.replace(name, tok)
        return text

    def _restore_names(self, platform: str, chat_id: str, text: str) -> str:
        """[PCn] (tolerantly spaced/bracketed) back to the real DB name."""
        slots = self._slots(platform, chat_id)
        if not slots:
            return text
        rev = {v: k for k, v in slots.items()}

        def sub(m: re.Match) -> str:
            return rev.get(int(m.group(1)), m.group(0))

        return re.sub(r"(?:\[\s*|\b)PC\s*(\d{1,2})(?:\s*\]|\b)", sub, text)

    def turn_in_flight(self, platform: str, chat_id: str) -> bool:
        """True while a DM turn for this chat is generating (chat lock held) —
        a check card may be seconds away from landing."""
        lock = self._chat_locks.get((platform, chat_id))
        return bool(lock and lock.locked())

    # ---------- LLM ----------

    def _lang_for(self, platform: str, chat_id: str, text: str) -> str:
        """INPUT language of the player's message: "zh" or "en". Ambiguous
        inputs (option picks like "A"/"ok") inherit the last confident
        language of the chat; with no signal at all the table defaults to
        zh — output is Traditional Chinese-first regardless (see
        _build_messages)."""
        t = (text or "").strip()
        if _CJK_RE.search(t):
            return "zh"
        if len(t) >= 4:
            return "en"
        for m in reversed(self._history(platform, chat_id, 12)):
            if m["role"] != "user":
                continue
            c = (m["content"] or "").strip()
            if _CJK_RE.search(c):
                return "zh"
            if len(c) >= 4:
                return "en"
        return "zh"

    def _build_messages(self, platform: str, chat_id: str,
                        latest_user_text: str | None = None,
                        system_suffix: str | None = None) -> list[dict]:
        s = self._session(platform, chat_id)
        # rules block = always-on core compendium + this turn's SRD lookup
        lookup = "(no specific entry retrieved — follow CORE MECHANICS)"
        if self.rules_index is not None and latest_user_text:
            lookup = self.rules_index.format(
                self.rules_index.search(latest_user_text))
        rules = CORE_RULES + "\n\nSRD LOOKUP (retrieved for this turn):\n" + lookup
        # Table policy: output is ALWAYS Traditional Chinese-first (zh system
        # prompt every turn). English INPUT additionally earns one short
        # English summary line at the end of the zh narration.
        lang = self._lang_for(platform, chat_id, latest_user_text or "")
        system = SYSTEM_PROMPT_ZH.format(
            rules=rules,
            party=self.party_text(platform, chat_id) if s["party"] else "(no party yet)",
            summary=s["summary"] or "(new adventure)",
            difficulty=DIFFICULTY_POLICIES.get(s.get("difficulty") or "easy",
                                               DIFFICULTY_POLICIES["easy"]),
        )
        if lang == "en":
            system += ("\n\n本回合指示：玩家以英文輸入。回應仍以繁體中文為主"
                       "（80-180 字），並在繁體中文敘述的結尾加一行簡短英文摘要"
                       "（1-2 句：關鍵資訊或行動選項）。絕不用英文寫整段敘述。")
        if system_suffix:
            system += "\n\n" + system_suffix
        combat = self.combat_get(platform, chat_id)
        if combat and combat.get("order"):
            order = combat["order"]
            cur = order[combat["idx"] % len(order)]
            order_txt = " → ".join(
                o["name"] + ("（敵）" if o.get("npc") else "") for o in order)
            system += (f"\n\nCOMBAT 第 {combat.get('round', 1)} 回合 — 先攻順序："
                       f"{order_txt}。")
            if cur.get("npc"):
                system += (f"目前輪到敵方（{cur['name']}）：敵方行動會在下一位"
                           "玩家行動時一併敘述，現在不要自行展開。")
            else:
                system += (f"目前行動：{cur['name']} — 只處理該角色的行動，"
                           "其他玩家角色僅被動反應，結尾留下鉤子；若玩家可能"
                           "攻擊，提醒他們可用 `/attack <目標>` 指令。")
        if s.get("scene") or s.get("objective"):
            system += ("\n\nCURRENT SCENE / 目前場景: "
                       + (s.get("scene") or "-") +
                       "\nCURRENT OBJECTIVE / 目前目標: "
                       + (s.get("objective") or "-"))
        # DM notebook: named NPCs & established world facts from the DB —
        # recency-capped (mentions float nodes to the top via touch_notes)
        npcs = self.npc_list(platform, chat_id, limit=8)
        lore = self.lore_list(platform, chat_id, limit=8)
        if npcs:
            system += ("\n\nDM 筆記——NPC 記錄（跨回合事實：名字、身分與狀態"
                       "必須與此一致；狀態改變時發 [[npc:...]] 更新）：\n"
                       + "\n".join(f"- {n['name']}：{n['desc']}"
                                   + (f"（{n['status']}）" if n["status"] else "")
                                   for n in npcs))
        if lore:
            system += ("\n\nDM 筆記——世界情報（已確立的事實，敘述不得矛盾；"
                       "有新進展時發 [[lore:...]] 更新）：\n"
                       + "\n".join(f"- {l['key']}：{l['fact']}" for l in lore))
        msgs = [{"role": "system", "content": system}]
        # context graph: only the last raw_window messages verbatim; older
        # turns are injected as one-line event records (facts, not prose)
        hist = self._history(platform, chat_id, self.raw_window)
        # repetition death-spiral guard: drop near-duplicate assistant
        # replies anywhere in the window (players' messages interleave them)
        # so the model stops feeding on its own echo — and order a hard
        # break below when detected
        dup_found = False
        last_asst = None
        _cleaned = []
        for m in hist:
            if m["role"] == "assistant":
                if last_asst is not None \
                        and _near_dup(m["content"], last_asst):
                    dup_found = True
                    continue  # drop the duplicate narration entirely
                last_asst = m["content"]
            _cleaned.append(m)
        hist = _cleaned
        if hist:
            oldest_raw = hist[0].get("id") if hist[0].get("id") else 0
            events = self.events_before(platform, chat_id, oldest_raw)
            if events:
                msgs.append({"role": "system", "content":
                             "近期事件摘要（結構化事實，取代舊對話原文——劇情必須"
                             "與此一致。這是內部紀錄：僅供記憶，不要在回覆中重複、"
                             "條列或模仿這種格式，直接用小說敘述延續即可）：\n"
                             + "\n".join(f"- {e}" for e in events)})
        for m in hist:
            role = "assistant" if m["role"] == "assistant" else "user"
            content = m["content"]
            if m["role"] == "assistant":
                content = _STATUS_LINE_RE.sub("", content)  # de-spam history
                content = _ENGINE_FOOTER_RE.sub("", content)
            if m["role"] == "user" and m["name"]:
                content = f"{m['name']}: {content}"
            msgs.append({"role": role, "content": content})
        if dup_found:
            msgs.append({"role": "system", "content":
                         "REPETITION ALERT（系統偵測）：你最近幾回合的敘述幾乎完全"
                         "相同，玩家已經困惑。本回合嚴禁重複任何先前的敘述內容——"
                         "直接回應玩家的最新行動，推進新的劇情（新的發現、NPC 反應、"
                         "環境變化或新的選擇）。"})
        # canonical reminder at the END: small models attend most to recent
        # tokens, so party classes are restated right before generation
        canon = "; ".join(
            f"{n}={v['occupation']}" + (f"(Lv{v['level']})" if v.get("level") else "")
            for n, v in self.get_party(platform, chat_id).items()
            if isinstance(v, dict) and v.get("occupation"))
        if canon:
            hp = "; ".join(
                f"{n} {v['hp_now']}/{v['hp_max']}HP"
                for n, v in self.get_party(platform, chat_id).items()
                if isinstance(v, dict) and "hp_now" in v)
            rem = (f"CANONICAL 準則: {canon}"
                   + (f" | {hp}" if hp else "")
                   + ". 每個代號必須以此職業敘述（例如 Druid=德魯伊、Warlock=術士、"
                     "Wizard=法師），絕不可混淆或改變。")
            msgs.append({"role": "system", "content": rem})
        # name placeholders: real character names never reach the model —
        # it only ever sees [PCn], so it cannot rename or transliterate them
        mapping = self._name_map(platform, chat_id)
        if mapping:
            msgs = [{**m, "content": self._map_text(m["content"], mapping)}
                    for m in msgs]
        return msgs

    async def _llm_stream(self, messages: list[dict], on_delta=None,
                          max_tokens: int | None = None) -> str:
        """Call llama.cpp /v1/chat/completions with streaming; returns full text."""
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": 0.72,
            "top_p": 0.9,
            "repeat_penalty": 1.1,
            "stream": True,
        }
        text = ""
        reasoning = [0]
        async with self._llm_lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(600.0)) as client:
                async with client.stream(
                    "POST", f"{self.llm_url}/v1/chat/completions", json=body
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            delta_obj = json.loads(payload)["choices"][0]["delta"]
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                        r = delta_obj.get("reasoning") or ""
                        if r:
                            # thinking model: show progress, keep the text clean
                            reasoning[0] += len(r)
                            if on_delta and reasoning[0] % 160 < len(r):
                                try:
                                    on_delta(f"🧠 DM 思考中… ({reasoning[0]} chars)")
                                except Exception:
                                    pass
                        delta = delta_obj.get("content")
                        if delta:
                            text += delta
                            if on_delta:
                                try:
                                    on_delta(text)
                                except Exception:
                                    pass
        if not text.strip() and reasoning[0]:
            # salvage: model put everything in reasoning (truncated thinking)
            return f"(模型思考未完成，請再試一次 / thinking did not finish)"
        return text.strip()

    async def dm_reply(self, platform: str, chat_id: str, user_name: str,
                       user_text: str, on_delta=None,
                       system_suffix: str | None = None,
                       user_id: str = "",
                       combat_turn: bool = False,
                       attack_verdict: dict | list[dict] | None = None,
                       forced_d20: int | None = None) -> str:
        """Record the player's message, get the DM's narration, resolve dice, persist.

        system_suffix: directive appended to the system prompt for this call only
        (used by /new to open a scene) — not recorded in history.
        combat_turn: this is a combat move for the acting character — enemy
        slots passed since the last move are narrated in the same reply, and
        the rotation advances ONLY after the reply succeeds.
        forced_d20: use this die for the pending consent check (the roll
        already happened elsewhere, e.g. /roll-admin) instead of a fresh d20.
        """
        key = (platform, chat_id)
        lock = self._chat_locks.setdefault(key, asyncio.Lock())
        async with lock:
            # consent-based check: resolve pending check before generating
            verdict_line = ""
            consent_note = ""
            pending = self.get_pending_check(platform, chat_id)
            if attack_verdict is not None:
                # /attack: the engine already rolled — inject as FINAL
                avs = (attack_verdict if isinstance(attack_verdict, list)
                       else [attack_verdict])
                verdict_line = "\n".join(checks_mod.render_verdict(v)
                                         for v in avs)
                dmg_hint = ""
                applied_done = any(v.get("dmg_total") is not None for v in avs)
                if applied_done:
                    dmg_hint = ("傷害與 HP 已由系統擲骰並套用完畢——直接敘述"
                                "後果即可，不要再擲傷害骰、不要重算、不要再"
                                "套用 [[hp]] 標記。")
                else:
                    for v in avs:
                        if v.get("move"):
                            dmg_hint = (f"招式：{v['move']}，建議傷害骰 "
                                        f"[[{v.get('dmg') or '1d6'}]]。")
                if any(v.get("aoe") for v in avs):
                    verdict_line += ("（玩家以 /attack 發起範圍攻擊：判定對"
                                     "所選目標，但傷害是範圍性的——對場上"
                                     "每一個敵人分別以 [[骰式]] 擲傷害並用 "
                                     "[[hp:敵人:-N]] 套用。" + dmg_hint +
                                     "本回合只屬於該玩家的攻擊：絕不讓敵人"
                                     "反擊、絕不對任何玩家角色造成傷害。）")
                else:
                    verdict_line += ("（玩家以 /attack 發起，系統已判定——"
                                     "依此敘述命中/未命中與暴擊，傷害用 "
                                     "[[骰式]] 並以 [[hp:目標:-N]] 套用。" + dmg_hint +
                                     "本回合只屬於該玩家的攻擊：絕不讓敵人反"
                                     "擊、絕不對任何玩家角色造成傷害——敵方行"
                                     "動在下一位玩家的回合才敘述。）")
            elif pending and system_suffix is None:
                if checks_mod.is_consent(user_text or ""):
                    # only the pending 判定's character owner may settle it
                    # (characters without an owner_id stay open to anyone)
                    pchar = pending.get("char", "")
                    pentry = self.get_party(platform, chat_id).get(pchar)
                    powner = str(pentry.get("owner_id") or "") \
                        if isinstance(pentry, dict) else ""
                    if not powner or powner == str(user_id or ""):
                        v = self.resolve_check(platform, chat_id,
                                               d20=forced_d20)
                        verdict_line = checks_mod.render_verdict(v)
                        verdict_line += "（請 DM 依此判定敘述結果）"
                    else:
                        consent_note = (
                            f"⚠️ 待決檢定屬於 {pchar} — 僅該角色的玩家可結算"
                            "（該玩家回覆「骰」或執行 /roll）。"
                            "你的行動照常敘述。")
                else:
                    # a different action does NOT discard a posted 判定:
                    # the card stays pending until settled by its owner or
                    # superseded by a newer card (re-presented below)
                    pass
            # settle deferred enemy hits from the previous turn:
            deferred_line = ""
            deferred = self.pop_deferred(platform, chat_id)
            if deferred and system_suffix is None:
                party = self.get_party(platform, chat_id)
                parts = []
                for cname, delta in deferred:
                    ent = party.get(cname)
                    if isinstance(ent, dict) and "hp_now" in ent:
                        ent["hp_now"] = max(0, min(ent["hp_max"],
                                                   ent["hp_now"] + delta))
                        parts.append(f"{cname} {delta:+d} HP -> "
                                     f"{ent['hp_now']}/{ent['hp_max']}"
                                     + ("（倒地！死亡豁免）" if ent["hp_now"] <= 0 else ""))
                self.set_party(platform, chat_id, party)
                deferred_line = ("⚔️ **敵方反擊結算（上回合未敘述，系統補記）**："
                                 + "；".join(parts))
            # input hardening BEFORE the text is stored or shown to the model:
            # neutralize placeholder spoofing and injection phrases
            user_text, scrubbed = scrub_player_input(user_text or "")
            if system_suffix is None:
                self._add_msg(platform, chat_id, "user", user_name, user_text,
                              user_id)
            msgs = self._build_messages(platform, chat_id, user_text, system_suffix)
            # combat move: compress NPC slots into this reply; rotate after
            combat_st, combat_npc, combat_actor = None, [], None
            if combat_turn:
                combat_st = self.combat_get(platform, chat_id)
                if combat_st and combat_st.get("order"):
                    combat_npc, slot = self.combat_act_window(combat_st)
                    if slot is not None and not slot.get("npc"):
                        combat_actor = slot
                        mapping = self._name_map(platform, chat_id)
                        d = (f"COMBAT 第 {combat_st.get('round', 1)} 回合："
                             + (f"敵方行動（{('、'.join(combat_npc))}）先簡潔敘述，然後 "
                                if combat_npc else "")
                             + f"處理 {combat_actor['name']} 的行動。只解算該角色的"
                               "動作（檢定走 [[check:...]]），套用 [[hp]]/[[item]] 標記，"
                               "結尾把鏡頭交給下一位。")
                        msgs.append({"role": "system",
                                     "content": self._map_text(d, mapping)})
            # ownership directive: the speaker may only control their own
            # characters — cross-character puppeteering in the message text
            # is refused narratively by the DM (engine renders the warning)
            if system_suffix is None and user_id:
                party = self.get_party(platform, chat_id)
                mapping = self._name_map(platform, chat_id)
                pc_chars = [n for n, v in party.items() if isinstance(v, dict)]
                owned = [n for n, v in party.items()
                         if isinstance(v, dict)
                         and str(v.get("owner_id") or "") == str(user_id)]
                others = [n for n in pc_chars if n not in owned]
                if owned:
                    d = (f"OWNERSHIP: 本回合行動者 {_clip_name(user_name)} 只控制："
                         + "、".join(owned) + "。規則（強制）：\n"
                         f"· 敘述中的「你」只能指 {owned[0]}"
                         + ("（或其其他自有角色）" if len(owned) > 1 else "")
                         + "，絕不指別的角色。\n"
                         + (f"· 其他玩家角色（{('、'.join(others))}）只能有被動／"
                            "環境反應——不得替他們行動、說話、施法或做決定。\n"
                            f"· 若訊息內容涉及 {('、'.join(others))} 的未完行動或"
                            "提問，只總結現況並邀請該角色的玩家行動（「輪到你了，"
                            "你要怎麼做？」），絕不代替該角色行動。"
                            if others else ""))
                else:
                    d = (f"OWNERSHIP: 本回合行動者 {_clip_name(user_name)} 未登記任何"
                         "角色（僅為旁白或閒聊）。規則（強制）：敘述中不得使用「你」"
                         "指涉任何玩家角色；只處理環境與 NPC 的回應，並提醒玩家可用"
                         " /pc 建立角色。")
                msgs.append({"role": "system",
                             "content": self._map_text(d, mapping)})
            if verdict_line:
                mapping = self._name_map(platform, chat_id)
                msgs.append({"role": "system", "content":
                             "SYSTEM CHECK VERDICT (already rolled on the server, "
                             "FINAL — narrate the outcome to match exactly, do "
                             "not roll again; do NOT write any [[dice]] or 🎲 "
                             "line for this check — inline dice are only for "
                             "damage/healing/loot AFTER the verdict): "
                             + self._map_text(verdict_line, mapping)})
            try:
                reply = await self._llm_stream(msgs, on_delta,
                                                max_tokens=self.narr_tokens)
            except Exception as e:
                raise RuntimeError(f"LLM error: {e}") from e
            reply = _THINK_RE.sub("", reply)
            # mid-sentence cut from the cap: close it gracefully
            if reply and len(reply) > 60 and not reply.rstrip().endswith(
                    ("。", "！", "？", "」", "…", "）", ")", ".", "!", "?")):
                reply = reply.rstrip() + "……"
            # the DM must never self-roll: scrub fabricated dice claims
            reply = _FAKE_DICE_RE.sub("", reply)
            reply = _FAKE_DICE_LINE_RE.sub("", reply)
            reply = _FAKE_VERDICT_RE.sub("", reply)  # no self-written verdicts
            reply = _TABLE_BLOCK_RE.sub("", reply)  # no stat-block dumps
            reply = _ENGINE_FOOTER_RE.sub("", reply)  # no imitated footers
            reply = re.sub(r"\*{2,}\s*\*{2,}", "", reply)  # emptied ** ** wrappers
            reply = _LANG_LABEL_RE.sub("", reply, count=1)
            # placeholders -> real names from the party DB, BEFORE tag parsing
            # so [[hp:[PC1]:-4]] and [[check:...|[PC1]|WIS]] resolve normally
            reply = self._restore_names(platform, chat_id, reply)
            # language enforcement: table policy is zh-first — if the model
            # drifted into an all-English reply, translate it back to zh
            reply_zh = _is_chinese(reply)
            if system_suffix is None and not reply_zh and len(reply) > 30:
                try:
                    tr = await self._llm_stream([
                        {"role": "user", "content":
                            f"Translate the following tabletop-RPG narration into "
                            f"Traditional Chinese (繁體中文). Keep dice/state markers "
                            f"like [[d20+3]] and [[hp:Name:-4]] exactly unchanged. "
                            f"Output ONLY the translation.\n\n---\n" + reply + "\n---"},
                    ])
                    if tr and _is_chinese(tr):
                        reply = _LANG_LABEL_RE.sub("", tr, count=1)
                except Exception:
                    pass  # keep original reply
            # Qwen skews Simplified; force Traditional for Chinese replies
            if _s2t and _CJK_RE.search(reply):
                reply = _s2t(reply)
            # stray English inside zh prose (家族傳 heirlooms): one small
            # fix-up pass so the table never sees mixed-language narration
            if system_suffix is None and _is_chinese(reply)                     and _latin_leaks(reply)                     and self._lang_for(platform, chat_id,
                                       user_text or "") == "zh":
                try:
                    tr = await self._llm_stream([
                        {"role": "user", "content":
                         "將以下中文敘述裡夾雜的英文單詞改寫成對應的繁體中文。"
                         "規則：只改英文單詞，其餘內容一字不改；遊戲術語"
                         "（AC、DC、HP、STR、DEX、CON、INT、WIS、CHA、PB、Lv）"
                         "與所有 [[...]] 標記保留原樣。只輸出改寫後的全文：\n\n"
                         + reply}])
                    if (tr and _is_chinese(tr)
                            and len(reply) * 0.7 < len(tr) < len(reply) * 1.3 + 60
                            and tr.count("[[") == reply.count("[[")):
                        reply = tr
                except Exception:
                    pass  # keep the original on any failure
            reply, results = resolve_llm_rolls(reply)
            reply, state_log = apply_state_tags(reply, self, platform,
                                                chat_id, user_name,
                                                defer_party_damage=attack_verdict is not None)
            if deferred_line:
                reply = deferred_line + "\n" + reply
            if verdict_line:
                reply = verdict_line + "\n" + reply
            if scrubbed:
                reply = ("⚠️ 已過濾訊息中可疑的指令或代號文字"
                         "（骰子、檢定與角色控制由系統強制執行）。\n" + reply)
            if consent_note:
                reply = consent_note + "\n" + reply
            # a fresh 判定 card ends the turn on ITS character — the engine
            # footer has the last word over any 「輪到你了」 the model appended
            new_card = any(("檢定要求" in l) or ("豁免" in l)
                           or l.startswith(("ATTACK", "CHECK"))
                           for l in state_log)
            card_pend = self.get_pending_check(platform, chat_id)
            if new_card and card_pend:
                reply += (f"\n⏳ 等待 **{card_pend['char']}** 的玩家擲骰"
                          f"（回覆「骰」或 `/roll`）——其他角色請稍候。")
            # a pending 判定 survives non-consent turns — keep it visible so
            # it can never be lost silently (skip when this reply itself
            # shows a fresh card, or the gate note already explains it)
            cur_pend = self.get_pending_check(platform, chat_id)
            if cur_pend and not consent_note and not any(
                    k in reply for k in ("檢定要求", "攻擊要求", "豁免 SAVE")):
                ab = f" {cur_pend.get('ability', '')}" if cur_pend.get("ability") else ""
                reply += (f"\n🎯 待決檢定仍在等待：{cur_pend.get('char', '?')}{ab} vs "
                          f"DC {cur_pend.get('dc', '?')} — 該角色的玩家回覆"
                          f"「骰」或 `/roll` 結算")
            # audit pass: narration hints at state changes but emitted no tags
            if not state_log and _STATE_HINT_RE.search(reply):
                try:
                    tags = await self._llm_stream(
                        [{"role": "user", "content": _EXTRACT_PROMPT + reply}],
                    )
                    if tags and "[[" in tags:
                        extra, extra_log = apply_state_tags(
                            tags.strip(), self, platform, chat_id, user_name)
                        if extra_log:
                            reply = reply + "\n" + "\n".join(
                                l for l in extra.splitlines() if l.strip())
                            state_log = extra_log
                except Exception:
                    pass  # best-effort audit; reply stands as-is
            # /new opener records the scene from the setting itself
            if system_suffix and system_suffix.startswith("NEW ADVENTURE"):
                self.update_game_state(platform, chat_id,
                                       scene=(user_text or "")[:120], objective="")
            # rotation advances only after a successful, persisted reply
            if combat_turn and combat_st is not None and combat_actor is not None:
                order = combat_st["order"]
                actor_i = next(i for i, o in enumerate(order) if o is combat_actor)
                self._combat_set_idx(platform, chat_id, combat_st, actor_i + 1)
            msg_id = self._add_msg(platform, chat_id, "assistant", "", reply)
            # context graph: one deterministic event line for this turn
            # (facts, not prose) + float any npc/lore nodes it touched
            snippet = re.sub(r"\s+", " ", (user_text or ""))[:80]
            ev_parts = []
            if verdict_line:
                if "→ ✅" in verdict_line:
                    ev_parts.append("檢定成功")
                elif "→ ❌" in verdict_line:
                    ev_parts.append("檢定失敗")
            ev_parts += state_log[:4]
            event_line = f"{user_name}：{snippet}" + \
                (f" ｜ {'；'.join(ev_parts)}" if ev_parts else "")
            self.add_event(platform, chat_id, msg_id, event_line)
            self.touch_notes(platform, chat_id, reply + " " + user_text)
            try:
                await self._maybe_summarize(platform, chat_id)
            except Exception:
                pass  # summarizer must never break a player's turn
            return reply

    async def _maybe_summarize(self, platform: str, chat_id: str):
        """When history grows past the window, compress the dropped tail into the summary."""
        cur = self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE platform=? AND chat_id=?", (platform, chat_id)
        )
        total = cur.fetchone()[0]
        if total <= self.max_history + 10:
            return
        drop = self.db.execute(
            "SELECT id, role, name, content FROM messages WHERE platform=? AND chat_id=? "
            "ORDER BY id ASC LIMIT ?",
            (platform, chat_id, total - self.max_history),
        ).fetchall()
        drop_ids = [r[0] for r in drop]
        s = self._session(platform, chat_id)
        tail = "\n".join(
            f"{(r[2] + ': ') if r[2] else ''}{r[3]}"[:500] for r in drop
        )
        prompt = [
            {"role": "system", "content": SYSTEM_PROMPT.format(
                rules="(none needed for summarizing)",
                party=self.party_text(platform, chat_id) if s["party"] else "(no party yet)",
                summary=s["summary"] or "(new adventure)")},
            {"role": "system", "content":
                "把既有冒險日誌與下列對話摘錄，壓縮成「節拍清單」：最多 8 條，"
                "每條一行 ≤25 字，格式「場景／事件 → 結果（狀態變化）」。"
                "必須保留：角色名與等級、關鍵 NPC、地點與目標、未解鉤子、"
                "重要物品、HP 大變化。繁體中文，不要對話原文。"},
            {"role": "user", "content": tail},
        ]
        try:
            new_summary = await self._llm_stream(prompt)
            self._save_session(platform, chat_id, s["party"], new_summary)
            self.db.execute(
                f"DELETE FROM messages WHERE id IN ({','.join('?' * len(drop_ids))})",
                drop_ids,
            )
            self.db.commit()
        except Exception:
            pass  # summarization is best-effort


_WEAPON_GLYPHS = "劍斧弓矛叉棍杖匕鏢錘刀槍鞭"


def compute_attack_moves(occupation: str, level: int, inventory: list,
                         max_slot_level: int = 99) -> list[dict]:
    """Pure move math (no LLM, no DB): class signature attacks gated by the
    character's granted spell-slot levels, plus carried weapons. Shared by
    the DM engine (/attack, /act panels) and the dnd-health /moves reference
    so the two can never drift apart."""
    occ = occupation or ""
    out = [{"name": n, "ability": ab, "dmg": d, "aoe": a}
           for n, ab, d, a in DMEngine.CLASS_ATTACKS.get(occ, [])]
    out = [m for m in out
           if DMEngine.MOVE_SPELL_LEVEL.get(m["name"], 0) <= max_slot_level]
    known = [m["name"] for m in out]
    for kind, name, _qty in inventory or []:
        # only weapons become moves: equipment-kind or a weapon glyph in the
        # name — rope and potions are not attack moves
        weaponish = (kind == "equipment"
                     or any(g in name for g in _WEAPON_GLYPHS))
        if weaponish and name not in known and "[" not in name:
            # finesse weapons (匕首/飛鏢) strike with DEX
            ab = "DEX" if ("匕" in name or "鏢" in name) else "STR"
            out.append({"name": f"{name} 打擊", "ability": ab,
                        "dmg": "1d6", "aoe": False, "improvised": True})
            known.append(name)
    return out
