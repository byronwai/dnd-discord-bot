"""Command list text and creation helpers shared by all adapters (EN + 繁中)."""

import re

from .dice import roll_expr

# --- character creation: occupations & fair attribute generation -------------

CLASS_KEYS = ["Barbarian", "Bard", "Cleric", "Druid", "Fighter", "Monk",
              "Paladin", "Ranger", "Rogue", "Sorcerer", "Warlock", "Wizard"]

# bilingual labels for the /pc dropdown (value stays canonical English)
CLASS_ZH = {"Barbarian": "野蠻人", "Bard": "吟遊詩人", "Cleric": "牧師",
            "Druid": "德魯伊", "Fighter": "戰士", "Monk": "武僧",
            "Paladin": "聖武士", "Ranger": "遊俠", "Rogue": "盜賊",
            "Sorcerer": "術士", "Warlock": "魔契師", "Wizard": "法師"}


def class_choice_label(c: str) -> str:
    return f"{CLASS_ZH.get(c, c)} {c}"

# ability priority per SRD quick-build (highest roll first)
CLASS_PRIORITIES = {
    "Barbarian": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
    "Bard":      ["CHA", "DEX", "CON", "WIS", "INT", "STR"],
    "Cleric":    ["WIS", "CON", "STR", "CHA", "DEX", "INT"],
    "Druid":     ["WIS", "CON", "DEX", "INT", "CHA", "STR"],
    "Fighter":   ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
    "Monk":      ["DEX", "WIS", "CON", "STR", "CHA", "INT"],
    "Paladin":   ["STR", "CHA", "CON", "WIS", "DEX", "INT"],
    "Ranger":    ["DEX", "WIS", "CON", "STR", "INT", "CHA"],
    "Rogue":     ["DEX", "INT", "CON", "WIS", "CHA", "STR"],
    "Sorcerer":  ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
    "Warlock":   ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
    "Wizard":    ["INT", "CON", "DEX", "WIS", "CHA", "STR"],
}
STANDARD_ARRAY = [15, 14, 13, 12, 10, 8]


def resolve_occupation(text: str) -> str | None:
    """Case-insensitive class name → canonical form, or None."""
    t = (text or "").strip().lower()
    for c in CLASS_KEYS:
        if t == c.lower():
            return c
    return None

# SRD hit dice per class (level-1 HP = die + CON modifier)
HIT_DICE = {"Barbarian": 12, "Fighter": 10, "Paladin": 10, "Ranger": 10,
            "Bard": 8, "Cleric": 8, "Druid": 8, "Monk": 8, "Rogue": 8,
            "Warlock": 8, "Sorcerer": 6, "Wizard": 6}


# 5e XP thresholds per level (SRD)
XP_LEVELS = [0, 300, 900, 2700, 6500, 14000, 23000, 34000, 48000,
             64000, 85000, 100000, 120000, 140000, 165000, 195000,
             225000, 265000, 305000, 355000]


def level_for(xp: int) -> int:
    lvl = 1
    for i, need in enumerate(XP_LEVELS, start=1):
        if xp >= need:
            lvl = i
    return lvl


def starting_hp(occupation: str, stats: dict) -> int:
    die = HIT_DICE.get(occupation, 8)
    con = int(stats.get("CON", 10))
    return die + (con - 10) // 2


def roll_stats_by_class(occupation: str) -> dict:
    """Server-side 4d6kh3 ×6, assigned to the class's ability priorities."""
    rolls = sorted((roll_expr("4d6kh3")[0] for _ in range(6)), reverse=True)
    prio = CLASS_PRIORITIES.get(occupation, ["STR", "DEX", "CON", "INT", "WIS", "CHA"])
    return dict(zip(prio, rolls))


def parse_stats(text: str, admin: bool = False) -> dict:
    """Parse 'STR 15 DEX 14 …' or bare '15,14,13,12,10,8'.

    Non-admins must use exactly the standard array (no cheating); admins may
    set any values in 8–20. Raises ValueError with a player-safe message.
    """
    s = (text or "").strip()
    pairs = re.findall(r"(STR|DEX|CON|INT|WIS|CHA)\s*[:=]?\s*(\d{1,2})", s, re.IGNORECASE)
    if pairs:
        nums = {k.upper(): int(v) for k, v in pairs}
    else:
        vals = [int(x) for x in re.findall(r"\b\d{1,2}\b", s)]
        if len(vals) != 6:
            raise ValueError("need all 6 attributes, e.g. STR 15 DEX 14 CON 13 "
                             "INT 12 WIS 10 CHA 8 / 需要 6 項屬性")
        nums = dict(zip(["STR", "DEX", "CON", "INT", "WIS", "CHA"], vals))
    if len(nums) != 6:
        raise ValueError("need all 6 attributes: STR DEX CON INT WIS CHA")
    for k, v in nums.items():
        if not 8 <= v <= 20:
            raise ValueError(f"{k}={v} is out of range 8–20 / 超出範圍 8–20")
    if not admin and sorted(nums.values()) != sorted(STANDARD_ARRAY):
        raise ValueError("self-set attributes must be the standard array "
                         "15, 14, 13, 12, 10, 8 — or leave stats empty and let "
                         "the bot roll 4d6×3 / 自填數值僅限標準數值組，否則請留空由系統擲骰")
    return nums


def format_stats(d: dict) -> str:
    return " ".join(f"{k} {d[k]}" for k in ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
                    if k in d)

DISCORD_EN = """🎲 **D&D DM Bot — Commands**

`/new <setting> [difficulty]` — start an adventure; **leave empty to see suggested scenes**.
Difficulty goes right after the setting: `/new blood moon castle (hard)` (Friendly/Standard/Hard), or use the `difficulty` dropdown
`/say <text>` — act in the story (your exact words are echoed to the channel)
`/act <text>` — your combat action on your turn: cast, use an item, skill, movement — for attacks prefer `/attack` (empty text = quick-pick dropdown: canned actions, items, custom)
`/pc <name> <occupation> [details]` — create/update (occupation dropdown; **bot rolls attributes**)
`/pc <name> <occupation> stats:STR 15 DEX 14 …` — self-set (standard array only — no cheating)
`/pc <name> <occupation> player:<@member>` — *(admin)* assign to another player
`/pc <name> remove confirm` — delete (owner only, needs confirmation)
`/party` — show the party sheet (characters + stats + inventory)
`/bonds` — player↔character bonds & history / 玩家—角色綁定與變更紀錄
`/give <char> <item> [qty] [equipment]` — add an item / 給角色道具或裝備
`/take <char> <item> [qty]` — remove an item / 取走
`/equip <char> <item>` — mark as equipment / 標記為裝備
`/inv [char]` — inventory / 道具與裝備一覽
`/slots` — spell slots / 法術格一覽
`/rest short|long` — spend hit dice or full recover / 短休（生命骰）或長休
`/status` — game status: session, story so far, party, system health
`/roll <expr>` — roll dice (`/roll 2d6+3`, `/roll adv`, `/roll 4d6kh3`); a d20 settles your pending check with the rolled die
`/roll-admin <expr> <char>` — *(admin)* roll for any character (in-command list); a d20 result settles their pending check
`/say-admin <text> <char>` — *(admin)* act as any character (in-command list)
`/act-admin [text] <char>` — *(admin)* combat action as any character (empty text = canned actions/items picker)
`/attack-admin <char> <target> <move>` — *(admin)* attack with any character (in-command lists, engine resolves)
`/attack <target> <move>` — **player attack**: pick a move (its name marks AoE); engine rolls once vs the target's AC / 攻擊骰：選招式，系統直接判定（範圍招式傷害波及全體敵人）
`/rollstats` — roll your 6 attributes for character creation (4d6 keep best 3)
`/classes` — the 12 SRD classes (occupations) to choose from
`/here` — bind this channel as the adventure table
`/reset` — wipe this session
`/help` — show this list

**Getting started:** `/rollstats` → `/classes` → `/pc Byron …` → `/new` → talk!
**Consent checks:** DM posts a DC card — reply `骰`/`roll` to roll (server computes
d20+mod and decides). **Combat:** starts automatically when the DM opens one in the
story (engine rolls initiative for everyone, enemies included) or via `/combat start`;
when it's your turn just TALK (or `/act`) — enemy turns are folded into the next
player's narration; stalled? `/voteskip` or admin `/takeover`. XP/levels tracked automatically.
**Natural language works too** — @mention me with "roll a d20", "status",
"new adventure <setting>" and I'll run the command. 中文也行：「骰 d20」、「開新團：血月城堡」、
「狀態」「隊伍」「職業」「物品」「擲屬性」。中文可以直接輸入，DM 會用你使用的語言回覆。"""

DISCORD_ZH = """🎲 **D&D DM 機器人 — 指令說明**

`/new <場景> [難度]` — 開始新冒險；**留空可查看建議場景**。難度直接加在場景後：`/new 血月堡（困難）`（新手／標準／困難），或用 `difficulty` 下拉選單
`/say <文字>` — 在故事中行動（你輸入的原句會先顯示在頻道）
`/act <行動>` — 戰鬥中輪到你時的行動：施法、用道具、技能、掩護、移動——攻擊建議用 `/attack`（留空＝下拉挑選常用動作／物品／自訂）
`/pc <名字> <職業> [細節]` — 建立或更新角色（職業下拉選單；**屬性由系統擲骰**）
`/pc <名字> <職業> stats:力量 15 …` — 自填數值（僅限標準數值組，防作弊）
`/pc <名字> <職業> player:<@成員>` — （管理員）指派給其他玩家
`/pc <名字> remove confirm` — 刪除角色（僅限擁有者，需二次確認）
`/party` — 顯示隊伍一覽（角色＋屬性＋物品）
`/bonds` — 玩家—角色綁定與變更紀錄
`/give <角色> <物品> [數量] [equipment]` — 給角色道具或裝備
`/take <角色> <物品> [數量]` — 取走物品
`/equip <角色> <物品>` — 標記為裝備
`/inv [角色]` — 道具與裝備一覽
`/slots` — 法術格一覽
`/rest short|long` — 短休（花生命骰）／長休（全滿恢復）
`/status` — 遊戲狀態：場次、劇情、隊伍、系統健康
`/roll <骰式>` — 擲骰（`/roll 2d6+3`）；有待決檢定時，d20 骰值直接結算該判定（限該角色玩家）
`/say-admin <文字> <角色>` — （管理員）以任意角色行動（角色在指令內清單挑選）
`/act-admin [文字] <角色>` — （管理員）以任意角色戰鬥行動（留空＝下拉挑選常用動作／物品）
`/attack-admin <角色> <目標> <招式>` — （管理員）以任意角色攻擊（指令內清單挑選，引擎全程結算）
`/roll-admin <骰式> <角色>` — （管理員）代擲（角色在指令內清單挑選）；有待決檢定時 d20 直接結算
`/attack <目標> <招式>` — **玩家直接攻擊**：系統對目標 AC 擲骰並公告成敗；範圍招式（如 月光束（範圍））傷害波及場上所有敵人
`/rollstats` — 擲出創角用的 6 項屬性（4d6 取高 3）
`/classes` — SRD 的 12 種職業一覽
`/here` — 綁定此頻道為遊戲桌面
`/reset` — 清除本場次
`/help` — 顯示本說明

**創角流程：**`/rollstats` 擲屬性 → `/classes` 選職業 → `/pc` 登記 → `/new` 開團！
**戰鬥：**DM 在劇情中開戰時系統自動擲全體先攻（含敵人），也可 `/combat start`；
輪到你時**直接說話就行**（或 `/act`），敵人回合會併入下一位玩家的敘述；
卡住了用 `/voteskip` 或管理員 `/takeover`。XP／升級自動追蹤。
**自然語言也通：**@提到我說「骰 d20」「開新團：血月城堡」「狀態」「隊伍」「職業」
「物品」「擲屬性」都會自動執行對應指令。全繁中遊玩完全沒問題，DM 會用繁體中文回覆。"""

# suggested starting scenes: label (menu/autocomplete), setting (sent to the DM)

# difficulty levels offered at /new (value = engine.DIFFICULTY_POLICIES key)
DIFFICULTY_LABELS = {"easy": "新手 Friendly", "normal": "標準 Standard",
                     "hard": "困難 Hard"}
_DIFFICULTY_WORDS = {
    "easy": ("新手", "簡單", "easy", "friendly"),
    "normal": ("標準", "普通", "normal", "standard"),
    "hard": ("困難", "困難模式", "高難度", "hard", "hardcore"),
}


def split_difficulty(text: str) -> tuple[str, str | None]:
    """Strip a trailing/bracketed difficulty word from a setting string.
    Returns (setting, difficulty_key or None). Understands「血月堡（困難）」,
    "crypt of the moon (hard)", 「開新團：酒窖 簡單」."""
    import re as _re
    t = (text or "").strip()
    for key, words in _DIFFICULTY_WORDS.items():
        for w in words:
            for pat in (rf"[（(]\s*{w}\s*[)）]\s*$", rf"[\s,，/|]+\s*{w}\s*[!！.。]?\s*$"):
                if _re.search(pat, t, _re.IGNORECASE):
                    return _re.sub(pat, "", t, flags=_re.IGNORECASE).strip(" ，,."), key
    return t, None
SETTING_SUGGESTIONS = [
    {"label": "霧錨鎮 Mistanchor (foggy seaside town)",
     "value": "迷霧壟罩的濱海小鎮「霧錨鎮」：海底傳來詭異歌聲，漁夫接連失蹤",
     "setting": "迷霧壟罩的濱海小鎮「霧錨鎮」：鎮民深夜聽見海底傳來詭異歌聲，碼頭出現黏液足跡，漁夫接連失蹤"},
    {"label": "血月城堡 Blood Moon Castle",
     "value": "血月高懸，古堡「血月堡」的大門在午夜自行開啟，尋寶的隊伍無一歸還",
     "setting": "血月高懸的荒野，古老城堡「血月堡」的大門在午夜自行開啟，彷彿邀請冒險者入內；先前前來尋寶的隊伍無一歸還"},
    {"label": "枯萎之村 The Withered Village",
     "value": "農村「枯萎村」：莊稼一夜枯黑，井水泛著微光，孩子們夢囈同一個名字",
     "setting": "一座平靜農村「枯萎村」：莊稼一夜之間全部枯黑，井水泛著微光，村裡的孩子們在夢中呼喊著同一個陌生名字"},
    {"label": "地下競技場 The Underground Arena",
     "value": "地下競技場：你們在囚籠中醒來，鐵閘門緩緩升起，對手的黑影已經逼近",
     "setting": "沙塵飛揚的地下競技場：你們在囚籠中醒來，觀眾吶喊震耳欲聾，鐵閘門正在緩緩升起，對手的黑影已經逼近"},
    {"label": "Goblin Tavern (beginner, 哥布林旅店)",
     "value": "a ruined tavern on the trade road, infested by goblins (classic beginner)",
     "setting": "a ruined tavern on the trade road, infested by goblins who stole a merchant caravan's goods — a classic beginner adventure"},
    {"label": "The Broken Crown (intrigue, 破碎王冠)",
     "value": "a kingdom torn by civil war; your mercenary band is hired by both sides",
     "setting": "the capital of a kingdom torn by civil war: the old king is dead, two heirs claim the Broken Crown, and your mercenary band is hired by both sides"},
]


def settings_menu(platform: str) -> str:
    """Suggested scenes shown when /new has no setting."""
    lines = ["🎲 **建議開場場景 / Suggested starting scenes**", ""]
    for i, s in enumerate(SETTING_SUGGESTIONS, 1):
        lines.append(f"**{i}. {s['label']}**")
        lines.append(f"   `{s['setting']}`")
    lines.append("")
    lines.append("挑一個複製後執行 `/new <場景>`；要選難度就把難度加在場景後面——")
    lines.append("例如：`/new 血月堡（困難）`、`/new 霧錨鎮（新手）`、`/new 競技場（標準）`，")
    lines.append("或用 `/new` 的 `difficulty` 下拉選單。也可以自己寫場景！")
    return "\n".join(lines)


CLASSES_TEXT = """⚔️ **職業一覽 / The 12 SRD classes**

**野蠻人 Barbarian** — 嗜血狂暴的戰士，靠「狂暴」承受與製造傷害（主屬性：力量/體質）
**吟遊詩人 Bard** — 以音樂、故事施法的萬用支援（魅力）
**牧師 Cleric** — 神祇的代理人：治療、祝福、驅散不死（感知）
**德魯伊 Druid** — 自然的守護者，能變形為動物（感知）
**戰士 Fighter** — 武器與甲冑的大師，最直接的戰鬥職業（力量或敏捷）
**武僧 Monk** — 運「氣」的武者，徒手與敏捷見長（敏捷/感知）
**聖武士 Paladin** — 立下神聖誓約的正義騎士（力量/魅力）
**遊俠 Ranger** — 荒野獵人，追蹤與精準射擊（敏捷/感知）
**盜賊 Rogue** — 潛行、開鎖、暗算的專家（敏捷）
**術士 Sorcerer** — 血脈中天生流淌魔法的施法者（魅力）
**魔契師 Warlock** — 與異界存在締結魔契換取力量（魅力）
**法師 Wizard** — 透過法術書鑽研奧秘的學者（智力）

💡 建一個角色：`/rollstats` 擲屬性 → 挑職業 → 範例：
`/pc Byron human fighter, STR 16 DEX 12 CON 14 INT 10 WIS 12 CHA 8, greatsword, AC 16, HP 19`
Play the class you like — ask the DM in-game for rules (grounded in the SRD)."""


def rollstats_text() -> str:
    rolls = sorted((roll_expr("4d6kh3")[0] for _ in range(6)), reverse=True)
    shown = "  ".join(f"{i}. **{r}**" for i, r in enumerate(rolls, 1))
    return ("🎲 **屬性擲骰 / Attribute rolls** (4d6 取高 3):\n"
            f"{shown}\n\n"
            "把這 6 個數字分配到：力量 STR · 敏捷 DEX · 體質 CON · "
            "智力 INT · 感知 WIS · 魅力 CHA\n"
            "(不想擲骰可用標準數值 / standard array: 15, 14, 13, 12, 10, 8)\n\n"
            "範例 / example:\n"
            "`/pc Byron human fighter, STR 16 DEX 12 CON 14 INT 10 WIS 12 CHA 8, "
            "greatsword, AC 16, HP 19`")
