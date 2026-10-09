"""Discord adapter — v4 engine-driven (11 commands).

/explore  — freeform non-combat action (digest → engine → narrate)
/combat   — RPG action menu via in-command autocomplete
"""

import asyncio
import logging
import os
import re

import discord
from discord import app_commands

from engine.dice import quick_roll, roll_expr

log = logging.getLogger("discord_adapter")


def _clip(s: str, limit: int = 1900) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _split(text: str, limit: int = 1900):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


HELP_TEXT = """🎲 **v4 引擎指令 / Commands**（11 個）

**玩家 / Player**
`/explore <text>` — 探索／對話／移動（非戰鬥行動）
　· 引擎即時判定（搜索/NPC對話/場景移動），敘事隨後補上
　· 範例：`/explore 依思詢問船長關於巴鐸的線索`

`/combat` — 戰鬥行動（RPG 選單，自動完成挑選目標和招式）
　· action：⚔️攻擊 🎒物品 ✨技能 🛡防禦 🏃撤退 👁觀察
　· 範例：`/combat action:攻擊 target:哥布林① move:詛咒木杖`

`/roll [expr]` — 擲骰（`/roll d20`、`/roll 2d6+3`、`/roll adv`）

`/give <item> [to]` — 把物品給隊友

`/status` — 隊伍 HP／法術格／場景／戰鬥狀態

`/continue` — 遊戲卡住時推進（顯示待確認／戰鬥輪到誰／場景重述）

`/help` — 本說明

**管理員 / Admin**
`/explore-admin <text> <char>` — 以任意角色探索
`/combat-admin` — 以任意角色戰鬥（同 /combat 選單）
`/roll-admin <expr> <char>` — 代擲
`/give-admin <char> <item> [qty]` — 給物品

**頻道內關鍵字**（直接打字，即時回應、無 LLM）：
`status` — 隊伍狀態
`inv` — 物品清單
`moves` — 招式一覽
`scene` — 場景描述

**桌邊聊天**：直接打字＝玩家間對話（引擎記錄但不回應）
**角色限制**：只能控制自己擁有的角色（引擎強制）
**NPC 對話**：NPC 只會透露其已知的事實（引擎管理）"""


class DiscordBot(discord.Client):
    def __init__(self, engine, message_content: bool = True):
        intents = discord.Intents.default()
        intents.message_content = message_content
        super().__init__(intents=intents)
        self.engine = engine
        self.tree = app_commands.CommandTree(self)
        self._v4_games: dict[str, object] = {}  # channel_id → V4Service
        self._register_commands()

    async def setup_hook(self):
        pass

    async def on_ready(self):
        log.info("logged in as %s (guilds=%d)", self.user, len(self.guilds))
        for g in self.guilds:
            try:
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
            except Exception as e:
                log.warning("sync failed for %s: %s", g.id, e)

    def _v4_channels(self) -> set:
        """All channel IDs routed to the v4 engine (comma-separated env)."""
        raw = (os.environ.get("V4_CHANNEL_IDS")
               or os.environ.get("V4_CHANNEL_ID")  # legacy single-channel
               or "").strip()
        return {c.strip() for c in raw.split(",") if c.strip()}

    def _v4_service(self, channel_id: str = ""):
        """Per-channel V4Service — each table gets its own game state."""
        cid = str(channel_id) if channel_id else "default"
        if cid not in self._v4_games:
            from v4.service import V4Service
            data_dir = os.environ.get("DATA_DIR", "data")
            self._v4_games[cid] = V4Service(
                data_dir,
                os.environ.get("LLM_URL", "http://127.0.0.1:11434"),
                os.environ.get("V4_DIGEST_MODEL", "gemma3:12b-it-qat"),
                os.environ.get("V4_NARR_MODEL", "gemma3:27b-it-qat"),
                channel_id=cid)
        return self._v4_games[cid]

    def _in_v4(self, interaction_or_channel) -> bool:
        cid = str(getattr(interaction_or_channel, "channel_id", None)
                  or getattr(interaction_or_channel, "id", ""))
        return cid in self._v4_channels()

    # ------------------------------------------------------------------
    #  command registration
    # ------------------------------------------------------------------

    def _register_commands(self):
        svc = self  # closure alias

        # ---- /pc (character creation) ----

        CLASS_KEYS = ["Barbarian", "Bard", "Cleric", "Druid", "Fighter",
                      "Monk", "Paladin", "Ranger", "Rogue", "Sorcerer",
                      "Warlock", "Wizard"]
        CLASS_ZH = {"Barbarian": "野蠻人", "Bard": "吟遊詩人",
                    "Cleric": "牧師", "Druid": "德魯伊", "Fighter": "戰士",
                    "Monk": "武僧", "Paladin": "聖武士", "Ranger": "遊俠",
                    "Rogue": "盜賊", "Sorcerer": "術士",
                    "Warlock": "魔契師", "Wizard": "法師"}

        @self.tree.command(name="pc",
                           description="建立角色 / create character")
        @app_commands.describe(
            name="角色名",
            occupation="職業（下拉選擇）",
            remove="填 remove confirm 以刪除角色（僅限擁有者）")
        @app_commands.choices(occupation=[
            app_commands.Choice(name=f"{CLASS_ZH[c]} {c}", value=c)
            for c in CLASS_KEYS])
        async def pc_cmd(interaction: discord.Interaction, name: str,
                         occupation: app_commands.Choice[str],
                         remove: str = ""):
            g = svc._v4_service(str(interaction.channel_id)).game
            uid = str(interaction.user.id)
            uname = interaction.user.display_name

            # removal
            if remove.strip().lower().startswith("remove"):
                entry = g.party.get(name)
                if not isinstance(entry, dict):
                    await interaction.response.send_message(
                        f"❓ 沒有角色「{name}」")
                    return
                if str(entry.get("owner_id", "")) != uid:
                    await interaction.response.send_message(
                        f"🚫 只有 {entry.get('owner', '?')} 可以刪除 {name}。")
                    return
                if remove.strip().lower() != "remove confirm":
                    await interaction.response.send_message(
                        f"⚠️ 確認刪除 {name}？填 `remove confirm`")
                    return
                g.party.pop(name, None)
                g.inventory.pop(name, None)
                g.ledger.add(name, "meta", f"{uname} 刪除了角色 {name}")
                svc._v4_service(str(interaction.channel_id))._save()
                await interaction.response.send_message(
                    f"🗑️ 已刪除 {name}。")
                return

            # creation or update
            if name in g.party:
                await interaction.response.send_message(
                    f"❓ 「{name}」已存在（擁有者：{g.party[name].get('owner', '?')}）")
                return

            occ = occupation.value if occupation else "Fighter"
            # fair stats: server-side 4d6kh3 ×6, assigned by class priority
            from engine.charlib import default_ac, slots_for
            from engine.dice import roll_expr
            PRIORITIES = {
                "Barbarian": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
                "Bard": ["CHA", "DEX", "CON", "WIS", "INT", "STR"],
                "Cleric": ["WIS", "CON", "STR", "CHA", "DEX", "INT"],
                "Druid": ["WIS", "CON", "DEX", "INT", "CHA", "STR"],
                "Fighter": ["STR", "CON", "DEX", "WIS", "CHA", "INT"],
                "Monk": ["DEX", "WIS", "CON", "STR", "CHA", "INT"],
                "Paladin": ["STR", "CHA", "CON", "WIS", "DEX", "INT"],
                "Ranger": ["DEX", "WIS", "CON", "STR", "INT", "CHA"],
                "Rogue": ["DEX", "INT", "CON", "WIS", "CHA", "STR"],
                "Sorcerer": ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
                "Warlock": ["CHA", "CON", "DEX", "WIS", "INT", "STR"],
                "Wizard": ["INT", "CON", "DEX", "WIS", "CHA", "STR"],
            }
            prio = PRIORITIES.get(occ, ["STR", "DEX", "CON", "INT", "WIS", "CHA"])
            rolls = sorted((roll_expr("4d6kh3")[0] for _ in range(6)),
                           reverse=True)
            stats = dict(zip(prio, rolls))
            # HP = hit die + CON mod
            HIT_DICE = {"Barbarian": 12, "Fighter": 10, "Paladin": 10,
                        "Ranger": 10, "Bard": 8, "Cleric": 8, "Druid": 8,
                        "Monk": 8, "Rogue": 8, "Warlock": 8,
                        "Sorcerer": 6, "Wizard": 6}
            die = HIT_DICE.get(occ, 8)
            con_mod = (stats["CON"] - 10) // 2
            hp = die + con_mod
            ac = default_ac(occ, stats)
            slots = {str(k): v for k, v in slots_for(occ, 1).items()}

            g.party[name] = {
                "occupation": occ, "stats": stats,
                "hp_now": hp, "hp_max": hp, "level": 1, "xp": 0,
                "owner": uname, "owner_id": uid,
                "ac": ac, "slots": slots, "hd_used": 0,
            }
            g.inventory.setdefault(name, [])
            g.ledger.add(name, "meta",
                         f"{uname} 建立了角色 {name}（{occ}）")
            svc._v4_service(str(interaction.channel_id))._save()

            stat_txt = " ".join(f"{k} {stats[k]}"
                                for k in ["STR", "DEX", "CON", "INT", "WIS", "CHA"])
            slot_txt = (" · 法術格 " + " ".join(f"L{k}×{v}"
                        for k, v in slots.items())) if slots else ""
            await interaction.response.send_message(
                f"🧙 **{name}** — {CLASS_ZH.get(occ, occ)} Lv1 "
                f"(玩家：{uname})\n"
                f"🛡 AC {ac} · ❤️ {hp}/{hp}{slot_txt}\n"
                f"📊 {stat_txt}\n"
                f"🎲 屬性由系統擲骰（4d6 取高 3）")

        # ---- autocomplete helpers ----

        async def combat_target_ac(interaction, current: str):
            cid = str(interaction.channel_id)
            g = svc._v4_service(str(interaction.channel_id)).game
            entries = []
            for name, foe in g.enemies.items():
                if foe.dead:
                    continue
                hp = f" HP {foe.hp}/{foe.hp_max}" if foe.hp_max else ""
                entries.append((f"{name}{hp}（敵）", name))
            for n in g.party:
                entries.append((f"{n}（玩家）", n))
            if not entries:
                entries.append(("AC 15（自訂）", "AC 15"))
            q = (current or "").strip().lower()
            return [app_commands.Choice(name=d[:100], value=v[:100])
                    for d, v in entries if not q or q in d.lower()][:25]

        async def combat_move_ac(interaction, current: str):
            cid = str(interaction.channel_id)
            g = svc._v4_service(str(interaction.channel_id)).game
            char = _filled_param(interaction, "character")
            if not char:
                char = next((n for n, v in g.party.items()
                             if isinstance(v, dict)
                             and str(v.get("owner_id", ""))
                             == str(interaction.user.id)), "")
            if not char:
                return [app_commands.Choice(name="（先用 /explore 建立角色）",
                                            value="")]
            from engine.moves import compute_attack_moves
            inv = [("item", n, q) for n, q in g.inventory.get(char, [])]
            mx = g.max_slot(char)
            moves = compute_attack_moves(
                g.party[char].get("occupation", ""),
                int(g.party[char].get("level", 1)), inv, mx)
            q = (current or "").strip().lower()
            out = []
            for m in moves:
                label = (f"{m['name']}（{m['ability']} 傷害 {m['dmg']}"
                         + ("，範圍" if m["aoe"] else "") + "）")
                if not q or q in label.lower() or q in m["name"].lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=m["name"][:100]))
            return out[:25]

        async def any_char_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            q = (current or "").strip().lower()
            out = []
            for n, v in g.party.items():
                label = f"{n}（{v.get('occupation', '?')}）"
                if not q or q in label.lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=n[:100]))
            return out[:25]

        async def item_ac(interaction, current: str):
            g = svc._v4_service(str(interaction.channel_id)).game
            q = (current or "").strip().lower()
            names = []
            for stacks in g.inventory.values():
                names += [n for n, _ in stacks]
            names += ["治療藥水", "火把", "繩索", "匕首", "長劍", "木盾"]
            seen, out = set(), []
            for n in names:
                if n in seen:
                    continue
                seen.add(n)
                if not q or q in n.lower():
                    out.append(app_commands.Choice(name=n[:100],
                                                   value=n[:100]))
            return out[:25]

        def _filled_param(interaction, name) -> str:
            for o in (interaction.data or {}).get("options", []):
                if o.get("name") == name:
                    return (o.get("value") or "").strip()
            return ""

        # ---- /explore ----

        @self.tree.command(name="explore",
                           description="探索／對話／移動（非戰鬥行動）/ freeform action")
        @app_commands.describe(text="你要做什麼？")
        async def explore_cmd(interaction: discord.Interaction, text: str):
            await _v4_turn(interaction, text)

        @self.tree.command(name="confirm",
                           description="確認待決行動 / confirm pending action")
        async def confirm_cmd(interaction: discord.Interaction):
            v4svc = svc._v4_service(str(interaction.channel_id))
            if v4svc.pending is None:
                await interaction.response.send_message(
                    "（沒有待確認的行動）")
                return
            p = v4svc.pending
            v4svc.pending = None
            p.args["confirmed"] = True
            await interaction.response.defer(thinking=True)
            from v4.rules_core import resolve as v4_resolve
            r = v4_resolve(v4svc.game, p)
            body = "\n".join(r.lines)
            if body:
                await interaction.followup.send(_clip(body))
            # narrate
            if r.accepted and r.lines:
                from v4.templates import render_hint
                from v4.guards import (make_placeholder_map, map_out,
                                       map_in, make_restore_map,
                                       scrub_narration, is_chinese)
                g = v4svc.game
                recent = [e for e in g.ledger.entries
                          if e.turn == g.ledger.turn][-6:]
                hints = [h for h in (render_hint(e, g) for e in recent) if h]
                facts = [e.text for e in recent]
                brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                                 for n, e in g.party.items())
                pmap = make_placeholder_map(list(g.party))
                rmap = make_restore_map(list(g.party))
                narr = await v4svc.narrator.narrate(
                    [map_out(f, pmap) for f in facts],
                    g.world.here.name, map_out(brief, pmap),
                    hints=[map_out(h, pmap) for h in hints])
                try:
                    from opencc import OpenCC
                    narr = OpenCC("s2t").convert(narr)
                except ImportError:
                    pass
                narr, _ = scrub_narration(narr)
                narr = map_in(narr, rmap)
                if narr and is_chinese(narr):
                    await interaction.followup.send("📖 " + _clip(narr))
                elif hints:
                    await interaction.followup.send(
                        "📖 " + _clip(map_in("\n".join(hints), rmap)))
            v4svc._save()

        # ---- /combat ----

        @self.tree.command(name="combat",
                           description="戰鬥行動（RPG 選單）/ combat action menu")
        @app_commands.describe(
            action="選擇行動類型",
            target="目標（清單挑選或自填 AC N）",
            move="招式（依角色列出）")
        @app_commands.choices(action=[
            app_commands.Choice(name="⚔️ 攻擊 Attack", value="attack"),
            app_commands.Choice(name="🎒 物品 Item", value="item"),
            app_commands.Choice(name="✨ 技能 Skill", value="skill"),
            app_commands.Choice(name="🛡 防禦 Defend", value="defend"),
            app_commands.Choice(name="🏃 撤退 Escape", value="escape"),
            app_commands.Choice(name="👁 觀察 Observe", value="observe"),
        ])
        @app_commands.autocomplete(target=combat_target_ac,
                                   move=combat_move_ac)
        async def combat_cmd(interaction: discord.Interaction,
                             action: app_commands.Choice[str],
                             target: str = "", move: str = ""):
            g = svc._v4_service(str(interaction.channel_id)).game
            if not g.combat.active and action.value not in ("observe",):
                await interaction.response.send_message(
                    "⚔️ 目前沒有戰鬥——用 `/explore` 行動。")
                return
            # find the caller's character
            char = next((n for n, v in g.party.items()
                         if str(v.get("owner_id", ""))
                         == str(interaction.user.id)), "")
            if not char:
                await interaction.response.send_message(
                    "❓ 找不到你的角色。")
                return
            act = action.value
            text = ""
            if act == "attack":
                text = f"{char} 攻擊 {target}" + (f" {move}" if move else "")
            elif act == "item":
                text = f"{char} 使用 {target}"  # target dropdown lists items
            elif act == "skill":
                text = f"{char} 施展 {target}" + (f" 於 {move}" if move else "")
            elif act == "defend":
                text = f"{char} 防禦"
            elif act == "escape":
                text = f"{char} 撤退"
            elif act == "observe":
                text = f"{char} 觀察"
            await _v4_turn(interaction, text, structured=True)

        # ---- /inventory ----

        @self.tree.command(name="inventory",
                           description="物品欄與招式一覽 / inventory & moves")
        @app_commands.describe(character="角色（留空＝自己）")
        @app_commands.autocomplete(character=any_char_ac)
        async def inventory_cmd(interaction: discord.Interaction,
                                character: str = ""):
            g = svc._v4_service(str(interaction.channel_id)).game
            # default to the caller's character
            char = character.strip()
            if not char:
                char = next((n for n, v in g.party.items()
                             if str(v.get("owner_id", ""))
                             == str(interaction.user.id)), "")
            if not char or char not in g.party:
                await interaction.response.send_message(
                    "❓ 請指定角色（用 character 參數挑選）。")
                return
            e = g.party[char]
            lines = [f"🎒 **{char}** — {e.get('occupation', '?')} "
                     f"Lv{e.get('level', 1)} "
                     f"HP {e['hp_now']}/{e['hp_max']}", ""]
            # inventory
            inv = g.inventory.get(char, [])
            if inv:
                lines.append("**物品 / Items**")
                for name, qty in inv:
                    lines.append(f"  🎒 {name}" + (f" ×{qty}" if qty > 1 else ""))
            else:
                lines.append("**物品 / Items**：（空）")
            # spell slots
            slots = e.get("slots") or {}
            if slots:
                lines.append("")
                lines.append("**法術格 / Spell Slots**")
                lines.append("  " + " · ".join(
                    f"L{k} {v}" for k, v in slots.items()))
            # moves
            from engine.moves import compute_attack_moves, \
                DMEngine
            inv_tuples = [("item", n, q) for n, q in inv]
            mx = g.max_slot(char)
            moves = compute_attack_moves(
                e.get("occupation", ""), int(e.get("level", 1)),
                inv_tuples, mx)
            all_moves = compute_attack_moves(
                e.get("occupation", ""), int(e.get("level", 1)),
                inv_tuples, 99)
            lines.append("")
            lines.append("**招式 / Moves**")
            for m in all_moves:
                req = move_spell_level.get(m["name"], 0)
                aoe = "（範圍）" if m["aoe"] else ""
                if m in moves:
                    cost = f"（耗 {req} 環法術格）" if req else ""
                    src = "（裝備）" if m.get("improvised") else ""
                    lines.append(f"  ⚔️ {m['name']} {m['ability']} "
                                 f"{m['dmg']}{aoe}{cost}{src}")
                else:
                    lines.append(f"  🔒 {m['name']} {m['ability']} "
                                 f"{m['dmg']}{aoe} — 需 {req} 環（未解鎖）")
            await interaction.response.send_message(
                "\n".join(lines)[:1900])

        # ---- /roll ----

        @self.tree.command(name="roll", description="擲骰 / dice roll")
        @app_commands.describe(expr="骰式：d20、2d6+3、adv")
        async def roll_cmd(interaction: discord.Interaction,
                           expr: str = "d20"):
            from engine.dice import quick_roll
            try:
                result = quick_roll(expr.strip() or "d20")
            except ValueError as e:
                await interaction.response.send_message(f"⚠️ {e}")
                return
            await interaction.response.send_message(
                f"🎲 **{interaction.user.display_name}** {result}")

        # ---- /give ----

        @self.tree.command(name="give",
                           description="把物品給隊友 / give item to party member")
        @app_commands.describe(item="物品名", to="給誰（角色名）")
        @app_commands.autocomplete(item=item_ac)
        async def give_cmd(interaction: discord.Interaction,
                           item: str, to: str = ""):
            g = svc._v4_service(str(interaction.channel_id)).game
            giver = next((n for n, v in g.party.items()
                          if str(v.get("owner_id", ""))
                          == str(interaction.user.id)), "")
            if not giver:
                await interaction.response.send_message("❓ 找不到你的角色。")
                return
            if to and to not in g.party:
                await interaction.response.send_message(
                    f"❓ 沒有角色「{to}」")
                return
            if not g.take_item(giver, item, 1):
                await interaction.response.send_message(
                    f"🚫 {giver} 沒有「{item}」")
                return
            if to:
                g.give_item(to, item, 1)
                g.ledger.add(giver, "item", f"{giver} 把 {item} 給了 {to}")
                await interaction.response.send_message(
                    f"🎒 {giver} → {to}：{item}×1")
            else:
                # no target specified: drop it
                g.ledger.add(giver, "item", f"{giver} 丟棄了 {item}")
                await interaction.response.send_message(
                    f"🎒 {giver} 丟棄了 {item}")
            svc._v4_service(str(interaction.channel_id))._save()

        # ---- /status ----

        @self.tree.command(name="status", description="隊伍／場景狀態 / game status")
        async def status_cmd(interaction: discord.Interaction):
            g = svc._v4_service(str(interaction.channel_id)).game
            lines = [f"📍 場景：{g.world.here.name}"]
            if g.combat.active:
                cur = g.combat.current()
                lines.append(f"⚔️ 戰鬥 R{g.combat.round}"
                             + (f"—輪到 {cur['name']}" if cur else ""))
            for n, e in g.party.items():
                slots = e.get("slots") or {}
                slot_txt = (" slots " + "·".join(
                    f"L{k}{v}" for k, v in slots.items())) if slots else ""
                lines.append(f"  {n} {e.get('occupation', '?')}"
                             f" Lv{e.get('level', 1)}"
                             f" HP {e['hp_now']}/{e['hp_max']}{slot_txt}")
            for n, f in g.enemies.items():
                if not f.dead:
                    lines.append(f"  👹 {n} {f.hp}/{f.hp_max} AC{f.ac}")
            await interaction.response.send_message("\n".join(lines)[:1900])

        # ---- /continue ----

        @self.tree.command(name="continue",
                           description="遊戲卡住時推進 / nudge if stalled")
        async def continue_cmd(interaction: discord.Interaction):
            await interaction.response.defer(thinking=True)
            v4svc = svc._v4_service(str(interaction.channel_id))
            g = v4svc.game
            lines = []
            if v4svc.pending is not None:
                p = v4svc.pending
                lines.append(f"❓ 待確認：**{p.actor or '?'} {p.action}"
                             f" {p.target or p.destination or p.item or ''}**"
                             "——回覆「確認」")
            if g.combat.active:
                cur = g.combat.current()
                if cur and not cur.get("npc"):
                    lines.append(f"⚔️ 輪到 **{cur['name']}**——用 `/combat`")
            if not lines:
                lines.append(f"🔄 場景：{g.world.here.name}")
                if not g.party:
                    lines.append("❓ 還沒有角色——先用 `/pc` 建立角色。")
                else:
                    facts = [e.text for e in g.ledger.entries[-5:]]
                    brief = "；".join(
                        f"{n} {e['hp_now']}/{e['hp_max']}HP"
                        for n, e in g.party.items()
                        if isinstance(e, dict))
                    narr = await v4svc.narrator.narrate(
                        facts, g.world.here.name, brief,
                        hints=[f"眾人在{g.world.here.name}。"
                               f"{g.world.here.description}",
                               "給玩家新的視角或鉤子。"])
                    if narr:
                        lines.append("📖 " + narr)
            await interaction.followup.send(
                "\n".join(lines)[:1900])

        # ---- /help ----

        @self.tree.command(name="help", description="指令說明 / commands")
        async def help_cmd(interaction: discord.Interaction):
            for chunk in _split(HELP_TEXT):
                if not interaction.response.is_done():
                    await interaction.response.send_message(chunk)
                else:
                    await interaction.followup.send(chunk)

        # =============================================================
        #  admin commands
        # =============================================================

        @self.tree.command(name="explore-admin",
                           description="(Admin) 以任意角色行動 / act as any character")
        @app_commands.describe(text="行動內容", character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def say_admin_cmd(interaction: discord.Interaction,
                                text: str, character: str):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            await _v4_admin(interaction, character, text)

        @self.tree.command(name="combat-admin",
                           description="(Admin) 以任意角色戰鬥 / combat as any character")
        @app_commands.describe(character="角色", action="行動",
                               target="目標", move="招式")
        @app_commands.choices(action=[
            app_commands.Choice(name="⚔️ 攻擊", value="attack"),
            app_commands.Choice(name="🎒 物品", value="item"),
            app_commands.Choice(name="✨ 技能", value="skill"),
            app_commands.Choice(name="🛡 防禦", value="defend"),
            app_commands.Choice(name="🏃 撤退", value="escape"),
        ])
        @app_commands.autocomplete(character=any_char_ac,
                                   target=combat_target_ac,
                                   move=combat_move_ac)
        async def combat_admin_cmd(interaction: discord.Interaction,
                                   character: str,
                                   action: app_commands.Choice[str],
                                   target: str = "", move: str = ""):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            act = action.value
            if act == "attack":
                text = f"{character} 攻擊 {target}" + \
                    (f" {move}" if move else "")
            elif act == "item":
                text = f"{character} 使用 {target}"
            elif act == "skill":
                text = f"{character} 施展 {target}"
            elif act == "defend":
                text = f"{character} 防禦"
            elif act == "escape":
                text = f"{character} 撤退"
            else:
                text = f"{character} 觀察"
            await _v4_admin(interaction, character, text)

        @self.tree.command(name="roll-admin",
                           description="(Admin) 代擲 / roll for any character")
        @app_commands.describe(expr="骰式", character="角色")
        @app_commands.autocomplete(character=any_char_ac)
        async def roll_admin_cmd(interaction: discord.Interaction,
                                 expr: str = "d20", character: str = ""):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            try:
                result = quick_roll(expr.strip() or "d20")
            except ValueError as e:
                await interaction.response.send_message(f"⚠️ {e}")
                return
            await interaction.response.send_message(
                f"🎲 (admin) **{character or '?'}** {result}")

        @self.tree.command(name="give-admin",
                           description="(Admin) 給物品 / give item")
        @app_commands.describe(character="角色", item="物品", qty="數量")
        @app_commands.autocomplete(character=any_char_ac, item=item_ac)
        async def give_admin_cmd(interaction: discord.Interaction,
                                 character: str, item: str, qty: int = 1):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message("🚫 僅管理員。")
                return
            out = svc._v4_service(str(interaction.channel_id)).admin_give(character, item, qty)
            await interaction.response.send_message(out)

        # ------------------------------------------------------------------
        #  shared v4 turn runners (defined last, used above)
        # ------------------------------------------------------------------

        async def _v4_turn(interaction, text, echo=None,
                           structured=False):
            """Player v4 turn: public echo → engine output → narrate.
            structured=True skips the digestor (from /combat dropdowns)."""
            # public echo: other players need to see what was issued
            # (slash command inputs are invisible in the channel)
            await interaction.channel.send(
                f"🎭 **{interaction.user.display_name}** "
                f"{_clip(text, 200)}")
            await interaction.response.defer(thinking=True)
            try:
                lines, narration = await svc._v4_service(str(interaction.channel_id)).handle(
                    text, interaction.user.display_name,
                    user_id=str(interaction.user.id),
                    structured=structured)
            except Exception as e:
                log.exception("v4 turn failed")
                await interaction.followup.send(f"⚠️ {e}")
                return
            body = "\n".join(lines)
            if body:
                await interaction.followup.send(_clip(body))
            if narration:
                await interaction.followup.send("📖 " + _clip(narration))

        async def _v4_admin(interaction, character, text):
            """Admin v4 turn for any character — with public echo."""
            await interaction.channel.send(
                f"🎛 **{interaction.user.display_name}** (admin) — "
                f"**{character}** {_clip(text, 200)}")
            await interaction.response.defer(thinking=True)
            try:
                out = await svc._v4_admin_run(interaction, character, text)
            except Exception as e:
                log.exception("v4 admin turn failed")
                await interaction.followup.send(f"⚠️ {e}")
                return
            if out:
                for msg in out:
                    await interaction.followup.send(_clip(msg))

    # ------------------------------------------------------------------
    #  message handling: table talk only (no DM reply, no emoji)
    # ------------------------------------------------------------------

    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.user:
            return
        content = message.content.strip()
        if not content:
            return
        v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
        if v4ch and str(message.channel.id) == v4ch:
            # plain text = table talk: ledger silently, no reaction, no reply
            try:
                self._v4_service(str(message.channel.id)).table_talk(
                    content, message.author.display_name)
            except Exception:
                pass
            return
        # non-v4 channels: ignore (v3 is retired)

    async def _v4_admin_run(self, interaction, character, text):
        """Execute admin-driven v4 turn; returns list of message strings."""
        svc = self._v4_service(str(interaction.channel_id))
        g = svc.game
        if character not in g.party:
            return [f"❓ 沒有角色「{character}」"]
        from v4.intent import Intent, parse_command
        it = parse_command(text, party_names=list(g.party))
        if it is None or it.actor != character:
            it = await svc.digestor.digest(
                text, list(g.party), g.world.here.name,
                list(g.world.here.exits.values()),
                known_targets=list(g.enemies) +
                [n["name"] for n in g.world.here.npcs] + list(g.party))
        it.actor = character
        from v4.rules_core import resolve as v4_resolve
        r = v4_resolve(g, it)
        out = r.lines
        if r.accepted and r.lines:
            from v4.templates import render_hint
            recent = [e for e in g.ledger.entries
                      if e.turn == g.ledger.turn][-6:]
            hints = [h for h in (render_hint(e, g) for e in recent) if h]
            facts = [e.text for e in recent]
            brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                             for n, e in g.party.items())
            narr = await svc.narrator.narrate(
                facts, g.world.here.name, brief, hints=hints)
            if narr:
                out = out + ["📖 " + narr]
        svc._save()
        return out


async def run_discord(engine, token: str):
    bot = DiscordBot(engine, message_content=True)
    await bot.start(token)
