#!/usr/bin/env python3
"""Party Health Board — standalone Discord bot that live-renders party HP
status from the dm-bot campaign SQLite DB into one channel message.

Update strategy:
  * every EVENT_POLL seconds a cheap MAX(id) heartbeat on the messages table
    detects new DM replies and pushes a board edit immediately (bypasses the
    edit throttle — one extra edit per reply is well within rate limits)
  * a FULL_REFRESH rebuild every 30s acts as a fallback in case a poll was
    missed; the fallback path still honours MIN_EDIT_INTERVAL

Configuration arrives via environment (systemd EnvironmentFile):
    DISCORD_TOKEN   bot token
    CHANNEL_ID      channel that hosts the board message
    DB_PATH         read-only path to campaign.db
    HEALTH_KEY      reserved for future use
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import discord
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dnd-health")

# the main bot's engine provides the shared (pure) move tables — /moves is
# fully deterministic: no LLM, one read-only DB query
DM_BOT_HOME = os.environ.get("DM_BOT_HOME", "/home/<USER>/dnd-dm-bot")

MARKER = "🩸 **隊伍健康看板 / Party Health Board**"

# One-time play guide (posted into this channel on first boot; marker-scanned
# + state.json flag so restarts never re-post it). Chunks stay under
# Discord's 2000-char message limit.
GUIDE_MARKER = "📘 **玩家指南 / Game Guide**"
GUIDE_CHUNKS = [
    GUIDE_MARKER + """（本訊息由看板機器人發布一次，不會重複）

**這是什麼遊戲？**
AI 地下城主（DM）主持的《龍與地下城》5e 冒險：你們扮演角色說話、行動、擲骰；DM 扮演全世界。
核心原則：**引擎判定、模型敘事** — 骰值、HP、物品、XP 全部由系統計算，DM 只把結果寫成故事。

**🚀 快速開始**
1️⃣ `/rollstats` 擲屬性
2️⃣ `/classes` 挑職業（12 種）
3️⃣ `/pc <名字> <職業>` 建立角色（屬性由系統公正擲骰）
4️⃣ `/new <場景>` 開新冒險（可選難度：新手／標準／困難）
5️⃣ 之後直接打字就行——說話＝行動！

**💬 怎麼行動**
直接在遊戲頻道用文字描述（「我推開門」「向老闆娘打聽消息」「我用火把照牆壁」）。
DM 每回合以繁體中文敘述 80–180 字，結尾給你鉤子或建議選項。XP、升級、HP、物品全部自動追蹤。""",

    """**🎯 檢定怎麼骰**
不確定的行動由引擎自動判定：d20＋角色真實修正值 vs DC。
引擎即時顯示完整結果（🎲 檢定行），敘事隨後補上。

**⚔️ 戰鬥流程**
1. 場景觸發遭遇 → 系統自動擲全體先攻（含敵人）
2. 輪到你時：用 `/combat` 選單行動（攻擊／物品／技能／防禦／撤退）
3. 引擎即時結算命中、傷害、HP，敘事隨後
4. 敵人回合由引擎自動結算
5. 敵人全滅 → 自動收戰

**🧰 玩家指令**
探索：`/explore`（對話／搜索／移動）
戰鬥：`/combat`（RPG 選單）
擲骰：`/roll` · 給物品：`/give`
狀態：`/status` · 推進：`/continue` · 說明：`/help`

**🎛 管理員指令**
`/explore-admin <text> <char>` — 以任意角色探索
`/combat-admin` — 以任意角色戰鬥
`/roll-admin <骰式> <角色>` — 代擲
`/give-admin <角色> <物品>` — 給物品

**規則**
· 只能控制自己擁有的角色（引擎強制）
· NPC 只透露已知事實（引擎管理）
· 直接打字＝桌邊聊天（引擎記錄但不回應）""",

    """**🩸 關於這個頻道的看板**
本頻道的健康看板每 2 秒自動更新：隊伍 HP 條與狀態、場景與難度、待決檢定、戰鬥敵人血條、最近事件。它只讀取遊戲資料庫，不會介入對話。

**🔄 一回合的流程**
你說話（行動）→ DM 想故事 → 不確定就發檢定卡 → 你骰（回覆「骰」或 `/roll`）→ 系統判定 → DM 敘述結果 → HP／物品／XP 自動更新 → 看板同步顯示。

祝冒險愉快！有問題隨時 `/help` 🎲""",
]
DIFFICULTY_ZH = {"easy": "新手", "normal": "標準", "hard": "困難"}
CHAT_WHERE = "platform = 'discord' AND chat_id = '<CHANNEL_ID>'"
SESSION_SQL = ("SELECT party, scene, difficulty, combat, pending_check "
               "FROM sessions WHERE " + CHAT_WHERE)
HEARTBEAT_SQL = "SELECT MAX(id), MAX(ts) FROM messages WHERE " + CHAT_WHERE
LAST_REPLY_SQL = ("SELECT ts FROM messages WHERE " + CHAT_WHERE +
                  " AND role = 'assistant' ORDER BY id DESC LIMIT 1")
EVENTS_SQL = ("SELECT line FROM events WHERE " + CHAT_WHERE +
              " ORDER BY id DESC LIMIT 3")
STATE_FILE = Path(__file__).resolve().parent / "state.json"
EVENT_POLL = 2            # seconds; cheap new-reply heartbeat
FULL_REFRESH = 30.0       # seconds; fallback rebuild if a poll was missed
MIN_EDIT_INTERVAL = 10.0  # fallback-path edit throttle (reply pushes bypass)


# ------------------------------------------------------------------ rendering

def hp_bar(now: int, mx: int, segments: int = 10) -> str:
    filled = 0 if mx <= 0 else round(now / mx * segments)
    filled = max(0, min(segments, filled))
    return "▓" * filled + "░" * (segments - filled)


def status_word(now: int, mx: int) -> str:
    if now <= 0:
        return "昏迷"
    if now <= 5 or (mx > 0 and now / mx <= 0.25):
        return "危急"
    if mx > 0 and now / mx <= 0.50:
        return "受傷"
    return "健康"


def character_line(name: str, data: dict) -> str:
    hp = int(data.get("hp_now") or 0)
    mx = int(data.get("hp_max") or 0)
    cls = data.get("occupation") or "?"
    lvl = data.get("level") or 1  # missing/null level (pre-tracking chars) → Lv1
    return (f"• **{name}** {cls} Lv{lvl} `{hp_bar(hp, mx)}` "
            f"**{hp}/{mx}** — {status_word(hp, mx)}")


def fmt_uptime(secs: float) -> str:
    secs = int(secs)
    d, secs = divmod(secs, 86400)
    h, secs = divmod(secs, 3600)
    m, _ = divmod(secs, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m" if m else f"{secs}s"


def rel_zh(delta: float) -> str:
    s = max(0, int(delta))
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分鐘"
    if s < 86400:
        return f"{s // 3600} 小時"
    return f"{s // 86400} 天"


def pending_line(p) -> str | None:
    if not p:
        return None
    return (f"🎯 {p.get('char') or '?'} {p.get('ability') or '?'} "
            f"vs DC {p.get('dc') or '?'} — 等待玩家回覆「骰」")


def combat_lines(combat: dict) -> list:
    order = combat.get("order") or []
    idx = combat.get("idx", -1)
    turn = "—"
    if 0 <= idx < len(order):
        turn = order[idx].get("name", "—")
        if order[idx].get("npc"):
            turn += "（敵）"
    lines = ["", f"⚔️ **戰鬥進行中**｜第 {combat.get('round', '?')} 回合｜"
             f"目前行動：**{turn}**"]
    for e in order:
        if not e.get("npc") or e.get("hp") is None:
            continue  # only enemies that carry hp data
        hp = int(e.get("hp") or 0)
        mx = int(e.get("hp_max") or 0)
        lines.append(f"　👹 {e.get('name', '?')} `{hp_bar(hp, mx)}` {hp}/{mx}")
    return lines


def render(party, scene, difficulty, combat, pending, last_ts, events,
           uptime_secs: float) -> str:
    stamp = datetime.now().strftime("%H:%M:%S")
    diff = DIFFICULTY_ZH.get(difficulty, difficulty or "—")
    lines = [
        MARKER,
        f"🟢 自動更新中｜運行 {fmt_uptime(uptime_secs)}｜更新：{stamp}",
        f"🗺️ 場景：{scene or '—'}｜難度：{diff}",
    ]
    if last_ts:
        lines.append(f"⏱️ 最後回合：{rel_zh(time.time() - last_ts)}前")
    lines.append("")
    if party:
        lines += [character_line(n, d) for n, d in party.items()]
    else:
        lines.append("_尚無隊伍資料 / no party data_")
    pl = pending_line(pending)
    if pl:
        lines += ["", pl]
    if combat:
        lines += combat_lines(combat)
    if events:
        lines += ["", "📜 最近事件"]
        lines += [f"　▪️ {e}" for e in events]  # newest first
    lines += ["", "_自動更新，資料來源 campaign.db (updated automatically)_"]
    return "\n".join(lines)


# ---------------------------------------------------------------- data source

def _json(value):
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def read_last_reply_id(db_path: str):
    """Cheap heartbeat: MAX(id) of tracked messages, or None when unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(HEARTBEAT_SQL).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None  # briefly locked/missing — skip this cycle
    return row[0] if row and row[0] is not None else None


def read_board(db_path: str):
    """Full snapshot for one render, or None when the DB is unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute(SESSION_SQL).fetchone()
            if row is None:
                return None
            reply = conn.execute(LAST_REPLY_SQL).fetchone()
            try:
                events = [r[0] for r in conn.execute(EVENTS_SQL)]
            except sqlite3.OperationalError:
                events = []  # events table absent on older DBs
            ev_head = conn.execute(
                "SELECT COALESCE(MAX(id),0) FROM events WHERE " + CHAT_WHERE
            ).fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return (_json(row[0]), row[1], row[2], _json(row[3]), _json(row[4]),
            reply[0] if reply else None, events, ev_head)


def read_new_events(db_path: str, last_id: int) -> tuple[int, list]:
    """(new_cursor, new event lines) — v3 engine facts since last push."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = conn.execute(
                "SELECT id, line FROM events WHERE " + CHAT_WHERE +
                " AND id > ? ORDER BY id LIMIT 15", (last_id,)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return last_id, []
    return (rows[-1][0] if rows else last_id), [r[1] for r in rows]


def read_new_v4_entries(path: str, cursor: int) -> tuple[int, list]:
    """(new_cursor, new lines) — v4 ledger entries since last push."""
    try:
        with open(path, encoding="utf-8") as f:
            entries = json.load(f).get("entries", [])
    except (OSError, ValueError):
        return cursor, []
    if len(entries) < cursor:
        cursor = 0  # the playground state was reset
    new = entries[cursor:cursor + 15]
    return (cursor + len(new)), [
        f"[v4 t{e.get('turn', '?')}][{e.get('kind', '?')}] "
        f"{e.get('actor', '?')}: {e.get('text', '')}" for e in new
        if e.get("kind") not in ("table",)]  # table talk = not game log


def v4_summary(path: str) -> str:
    """One compact debug block for the board message."""
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
    except (OSError, ValueError):
        return ""
    world = blob.get("world", {})
    cur = world.get("scenes", {}).get(world.get("current", ""), {})
    combat = blob.get("combat") or {}
    lines = [f"🧪 v4 playground：{cur.get('name', '?')} · turn "
             f"{blob.get('turn', 0)} · "
             + (f"戰鬥 R{combat.get('round', '?')}" if combat.get("order")
                else "探索")]
    for n, e in blob.get("party", {}).items():
        lines.append(f"　{n} {e.get('hp_now')}/{e.get('hp_max')}HP "
                     f"slots {e.get('slots') or {}}")
    for n, f in (blob.get("enemies") or {}).items():
        if not f.get("dead"):
            lines.append(f"　👹 {n} {f.get('hp')}/{f.get('hp_max')} "
                         f"AC{f.get('ac')}")
    return "\n".join(lines)


# ---------------------------------------------------------------- state file

def load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_state(**fields) -> None:
    data = load_state()
    data.update(fields)
    try:
        STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        log.warning("could not persist state.json")


# ------------------------------------------------------------------ moves

def moves_text(db_path: str, char_filter: str = "") -> str:
    """Every character's attack moves, computed live from campaign.db with
    the engine's own pure tables (no LLM). Locked spell moves are shown
    with the slot level that unlocks them."""
    if DM_BOT_HOME not in sys.path:
        sys.path.insert(0, DM_BOT_HOME)
    from engine.moves import compute_attack_moves, move_spell_level
    from engine.charlib import slots_for
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT party FROM sessions WHERE " + CHAT_WHERE).fetchone()
        inv_rows = conn.execute("SELECT char_name, kind, name, qty FROM items "
                                "WHERE " + CHAT_WHERE).fetchall()
    finally:
        conn.close()
    if not row or not row[0]:
        return "（尚無隊伍資料 / no party data）"
    party = json.loads(row[0])
    inv: dict[str, list] = {}
    for cn, kind, name, qty in inv_rows:
        inv.setdefault(cn, []).append((kind, name, qty))
    q = (char_filter or "").strip()
    lines = ["⚔️ **角色招式一覽 / Character Moves**（即時計算，無 LLM）", ""]
    for name, v in party.items():
        if not isinstance(v, dict) or (q and q not in name):
            continue
        occ = v.get("occupation", "?")
        lvl = int(v.get("level", 1) or 1)
        mx = {int(k): n for k, n in slots_for(occ, lvl).items()}
        max_slot = max(mx, default=0)
        cur = v.get("slots") or {}
        slot_txt = ""
        if mx:
            slot_txt = "｜法術格 " + " · ".join(
                f"L{k} {cur.get(str(k), n)}/{n}" for k, n in sorted(mx.items()))
        lines.append(f"**{name}**（{occ} Lv{lvl}）{slot_txt}")
        for m in compute_attack_moves(occ, lvl, inv.get(name, []), 99):
            req = move_spell_level.get(m["name"], 0)
            aoe = "（範圍）" if m["aoe"] else ""
            if req > max_slot:
                lines.append(f"　🔒 {m['name']} {m['ability']} {m['dmg']}{aoe}"
                             f" — 需 {req} 環法術格（尚未解鎖）")
                continue
            cost = f"（耗 {req} 環法術格）" if req else ""
            src = "（裝備）" if m.get("improvised") else ""
            lines.append(f"　· {m['name']} {m['ability']} {m['dmg']}{aoe}{cost}{src}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------- bot

class HealthBoardBot(discord.Client):
    def __init__(self, channel_id: int, db_path: str):
        super().__init__(intents=discord.Intents.default())  # default intents only
        self.channel_id = channel_id
        self.db_path = db_path
        self.channel = None
        self.board = None
        self.last_content = None
        self.last_edit = 0.0
        self.started = time.monotonic()
        self._last_max_id = None
        self._next_full = 0.0
        self._board_located = False
        self._guide_done = False
        self._perm_warned = False
        self._task = None
        self.tree = app_commands.CommandTree(self)
        self._register_commands()
        self.v4_path = os.path.join(os.path.dirname(os.path.abspath(db_path)),
                                    "v4_state.json")
        self.log_state = load_state()

    def _register_commands(self):
        @self.tree.command(name="moves",
                           description="角色攻擊招式一覽（即時計算，無 LLM）/ attack moves reference")
        @app_commands.describe(character="Optional: only one character / 可只看一名角色")
        async def moves_cmd(interaction: discord.Interaction,
                            character: str = ""):
            try:
                text = moves_text(self.db_path, character)
            except Exception as e:
                log.warning("moves failed: %s", e)
                await interaction.response.send_message(f"⚠️ {e}")
                return
            chunks, t = [], text
            while len(t) > 1900:
                cut = t.rfind("\n", 0, 1900)
                cut = cut if cut > 950 else 1900
                chunks.append(t[:cut])
                t = t[cut:].lstrip("\n")
            chunks.append(t)
            await interaction.response.send_message(chunks[0])
            for c in chunks[1:]:
                await interaction.followup.send(c)

    async def setup_hook(self):
        self._task = asyncio.create_task(self._poll_loop())

    async def on_ready(self):
        log.info("connected as %s (id %s)", self.user, self.user.id)
        if not self._board_located:
            try:
                await self._locate_board()
            except discord.HTTPException as exc:
                log.warning("board lookup failed: %s", exc)
        await self._ensure_guide()
        for g in self.guilds:  # guild-scoped sync (needs 'applications.commands')
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                log.info("slash commands synced to guild %s", g.id)
            except Exception as exc:
                log.warning("command sync failed for %s: %s (is "
                            "'applications.commands' in the invite?)", g.id, exc)

    async def _ensure_guide(self):
        """Post the play guide once per channel (state flag + marker scan)."""
        if self._guide_done:
            return
        self._guide_done = True
        if load_state().get("guide_posted"):
            return
        try:
            if self.channel is None:
                self.channel = self.get_channel(self.channel_id) \
                    or await self.fetch_channel(self.channel_id)
            async for msg in self.channel.history(limit=100):
                if msg.author.id == self.user.id and \
                        msg.content.startswith(GUIDE_MARKER):
                    save_state(guide_posted=True)
                    log.info("guide already present (message %s)", msg.id)
                    return
            first = None
            for chunk in GUIDE_CHUNKS:
                sent = await self.channel.send(chunk)
                first = first or sent
            try:
                await first.pin()  # nice-to-have; needs manage-messages
            except discord.HTTPException:
                pass
            save_state(guide_posted=True)
            log.info("game guide posted (%d chunks, first pinned)", len(GUIDE_CHUNKS))
        except discord.HTTPException as exc:
            log.warning("guide post failed: %s", exc)

    async def on_resumed(self):
        self.last_content = None  # force full re-render after reconnect

    async def _locate_board(self):
        """Find a reusable board message via state.json, else channel history."""
        self.channel = self.get_channel(self.channel_id) \
            or await self.fetch_channel(self.channel_id)
        self._board_located = True
        msg_id = load_state().get("message_id")
        if msg_id:
            try:
                self.board = await self.channel.fetch_message(msg_id)
                log.info("reusing board message %s (state.json)", msg_id)
                return
            except discord.NotFound:
                pass
        async for msg in self.channel.history(limit=50):  # newest first
            if msg.author.id == self.user.id and msg.content.startswith(MARKER):
                self.board = msg
                save_state(message_id=msg.id)
                log.info("reusing board message %s (channel history)", msg.id)
                return
        log.info("no existing board message; will post a new one")

    async def _poll_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._tick()
            except Exception:
                log.exception("poll tick failed")  # never crash the loop
            await asyncio.sleep(EVENT_POLL)

    async def _tick(self):
        if not self._board_located:
            return  # never post a fresh board before on_ready locates/reuses
        max_id = read_last_reply_id(self.db_path)
        reply_push = False
        if max_id is not None:
            if self._last_max_id is not None and max_id > self._last_max_id:
                reply_push = True  # a new DM reply landed
            self._last_max_id = max_id
        if not reply_push and time.monotonic() < self._next_full:
            await self._push_logs()  # logs flow even between board refreshes
            return
        data = read_board(self.db_path)
        if data is None:
            return
        self._next_full = time.monotonic() + FULL_REFRESH
        content = render(*data[:7], time.monotonic() - self.started)
        v4s = v4_summary(self.v4_path)
        if v4s:
            content += "\n" + v4s
        await self._publish(content, bypass_throttle=reply_push)
        await self._push_logs()

    async def _push_logs(self):
        """Debug portal: stream new engine facts (v3 events + v4 ledger)
        into the channel as batched log messages."""
        if self.channel is None:
            return
        # first run: park the cursors at the current head (no backlog flood)
        if "last_event_id" not in self.log_state:
            try:
                conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True,
                                       timeout=1.0)
                try:
                    head = conn.execute("SELECT COALESCE(MAX(id),0) FROM events "
                                        "WHERE " + CHAT_WHERE).fetchone()[0]
                finally:
                    conn.close()
            except sqlite3.Error:
                head = 0
            self.log_state["last_event_id"] = head
        if "last_v4_len" not in self.log_state:
            try:
                with open(self.v4_path, encoding="utf-8") as f:
                    self.log_state["last_v4_len"] = len(
                        json.load(f).get("entries", []))
            except (OSError, ValueError):
                self.log_state["last_v4_len"] = 0
        cur_ev, ev_lines = read_new_events(
            self.db_path, self.log_state["last_event_id"])
        cur_v4, v4_lines = read_new_v4_entries(
            self.v4_path, self.log_state["last_v4_len"])
        lines = [f"[v3] {l}" for l in ev_lines] + v4_lines
        if not lines:
            return
        try:
            await self.channel.send(
                "🧪 **debug log**\n" + "\n".join(lines)[:1900])
        except discord.HTTPException as exc:
            log.warning("log push failed: %s", exc)
            return
        self.log_state["last_event_id"] = cur_ev
        self.log_state["last_v4_len"] = cur_v4
        save_state(**self.log_state)

    async def _publish(self, content: str, bypass_throttle: bool) -> None:
        if content == self.last_content:
            return
        if not bypass_throttle and time.monotonic() - self.last_edit < MIN_EDIT_INTERVAL:
            return
        if self.board is None:
            if self.channel is None:
                try:
                    self.channel = await self.fetch_channel(self.channel_id)
                except discord.HTTPException as exc:
                    self._warn_perm(exc)
                    return
            self.board = await self.channel.send(content)
            save_state(message_id=self.board.id)
            self._perm_warned = False
            log.info("posted board message %s", self.board.id)
        else:
            try:
                await self.board.edit(content=content)
            except discord.Forbidden as exc:
                self._warn_perm(exc)
                return
            except discord.HTTPException as exc:
                log.warning("board edit failed: %s", exc)
                return
            self._perm_warned = False
            log.info("board updated%s", " (reply push)" if bypass_throttle else "")
        self.last_content = content
        self.last_edit = time.monotonic()

    def _warn_perm(self, exc):
        if not self._perm_warned:
            self._perm_warned = True
            log.error("Discord permission error (%s) — is the bot invited and "
                      "allowed in the target channel?", exc)


def main() -> int:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN not set; check the EnvironmentFile")
        return 2
    try:
        channel_id = int(os.environ.get("CHANNEL_ID", "0"))
    except ValueError:
        log.error("CHANNEL_ID must be an integer")
        return 2
    db_path = os.environ.get("DB_PATH", "/home/<USER>/dnd-dm-bot/data/campaign.db")
    bot = HealthBoardBot(channel_id, db_path)
    bot.run(token, log_handler=None)  # logging already goes to stdout/journald
    return 0


if __name__ == "__main__":
    sys.exit(main())
