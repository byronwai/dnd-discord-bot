"""Discord adapter for the D&D DM bot.

Two modes:
- Full mode (MESSAGE CONTENT INTENT enabled in the developer portal):
  freeform chat — DM the bot, @mention it, or bind a channel with /here or !here.
- Slash mode (intent off): everything works through slash commands;
  /say <text> is the freeform action. DMs also always work.

Commands: /help /new /roll /pc /party /here /reset /say (+ !-prefixed twins in full mode).
"""

import asyncio
import logging
import os
import re
import time

import discord
from discord import app_commands

from engine.commands import (CLASS_KEYS, CLASSES_TEXT, DISCORD_EN, DISCORD_ZH,
                             SETTING_SUGGESTIONS, class_choice_label,
                             format_stats, parse_stats,
                             resolve_occupation, roll_stats_by_class,
                             rollstats_text, settings_menu, starting_hp)
from engine.dm import DMEngine
from engine.dice import quick_roll, roll_expr
from engine.intents import parse_intent
from engine.status import build_status

log = logging.getLogger("discord_adapter")
EDIT_INTERVAL = 3.0

_D20_EXPR_RE = re.compile(
    r"^\s*1?\s*d\s*20\s*(?:([+-])\s*(\d{1,2}))?\s*$", re.IGNORECASE)
_ADV_DIS = {"adv", "advantage", "dis", "disadvantage"}


def _check_roll_die(expr: str) -> int | None:
    """The d20 for a check-shaped expression — 'd20', '1d20', 'd20+3',
    'adv'/'dis' — or None for anything else. A flat modifier is the player
    echoing their sheet modifier; the engine re-applies the REAL modifier,
    so only the raw die is taken (total − flat)."""
    e = (expr or "").strip().lower()
    if e in _ADV_DIS:
        return roll_expr(e)[0]  # kh2/kl2 result = the effective die
    m = _D20_EXPR_RE.match(e)
    if not m:
        return None
    try:
        total, _bd = roll_expr(re.sub(r"\s+", "", e))
    except ValueError:
        return None
    flat = int(m.group(2) or 0) * (-1 if m.group(1) == "-" else 1)
    return total - flat

# canned combat actions for the /act and /act-admin quick-pick panels
_CANNED_ACTIONS = [
    ("🛡 防禦 Dodge", "我採取防禦姿態（Dodge），專注閃避敵人的攻擊。"),
    ("🤝 協助隊友 Help", "我協助隊友，干擾敵人為隊友製造機會（Help）。"),
    ("🌿 躲藏 Hide", "我尋找掩護躲藏起來（Hide，DEX 隱匿）。"),
    ("💨 衝刺 Dash", "我用全力移動（Dash），快速拉開距離。"),
    ("↩️ 撤離 Disengage", "我謹慎後撤（Disengage），避免被追擊。"),
    ("👁 觀察 Search", "我環視戰場，觀察敵人的動向與弱點（Search）。"),
    ("🛡 舉盾護住隊友", "我舉起盾牌掩護附近的隊友。"),
]


def _action_options(engine, cid: str, char: str) -> list[discord.SelectOption]:
    """Quick-pick options for a character's combat action: canned 5e actions,
    their carried items, and a custom-text escape hatch."""
    opts = [discord.SelectOption(label=lab[:100], value=val[:100])
            for lab, val in _CANNED_ACTIONS]
    for _kind, name, qty in engine.inv_list("discord", cid, char).get(char, [])[:10]:
        opts.append(discord.SelectOption(
            label=f"🎒 使用 {name}×{qty}"[:100],
            value=f"我使用 {name}。"[:100]))
    opts.append(discord.SelectOption(label="✏️ 自訂行動…", value="__custom__"))
    return opts[:25]


class DiscordBot(discord.Client):
    def __init__(self, engine: DMEngine, message_content: bool = True):
        intents = discord.Intents.default()
        intents.message_content = message_content
        super().__init__(intents=intents)
        self.engine = engine
        self.bound_channels: set[int] = set()
        self.tree = app_commands.CommandTree(self)
        self.v4 = None  # lazy V4Service for the engine-driven playground
        self._register_commands()

    async def setup_hook(self):
        # commands are guild-scoped ONLY: the guild copies update instantly,
        # and keeping a global copy too makes Discord show every command
        # TWICE in the picker (guild ∩ global duplication). Freeform DM chat
        # still works without slash commands.
        pass

    async def on_ready(self):
        log.info("Discord logged in as %s (message_content=%s, guilds=%d)",
                 self.user, self.intents.message_content, len(self.guilds))
        for g in self.guilds:  # guild-scoped copies update instantly; the
            try:  # global sync (setup_hook) covers DMs but propagates slowly
                self.tree.copy_global_to(guild=g)
                await self.tree.sync(guild=g)
                log.info("slash commands copied+synced to guild %s", g.id)
            except Exception as e:
                log.warning("guild sync failed for %s: %s (is 'applications.commands' "
                            "scope missing from the invite?)", g.id, e)

    def _register_commands(self):
        async def _announce(interaction: discord.Interaction, line: str):
            """Post the slash invocation publicly — Discord hides slash inputs,
            so every command is echoed to the channel verbatim."""
            await interaction.response.send_message(
                f"⚡ **{interaction.user.display_name}** {line}")

        @self.tree.command(name="help", description="Show the D&D DM command list / 指令說明")
        async def help_cmd(interaction: discord.Interaction):
            # DISCORD_EN is >2000 chars — Discord rejects it whole, so both
            # texts go through the chunk splitter
            chunks = _split(DISCORD_ZH) + _split(DISCORD_EN)
            await interaction.response.send_message(chunks[0])
            for chunk in chunks[1:]:
                await interaction.followup.send(chunk)

        @self.tree.command(name="act",
                           description="Take your combat action: attack/cast/item/skill / 戰鬥行動")
        @app_commands.describe(text="你的行動——留空＝下拉挑選常用動作／物品／自訂")
        async def act_cmd(interaction: discord.Interaction, text: str = ""):
            cid = str(interaction.channel_id)
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and cid == v4ch:
                await self._v4_command_turn(interaction, text)
                return
            st = self.engine.combat_get("discord", cid)
            if not st or not st.get("order"):
                await interaction.response.send_message(
                    "No active combat — `/combat start` first (or the DM can "
                    "start one in the story).")
                return
            party = self.engine.get_party("discord", cid)
            is_admin = interaction.user.guild_permissions.manage_guild
            npcs, actor = self.engine.combat_act_window(st)
            if actor is None:
                await interaction.response.send_message("No active combat.")
                return
            owner_id = (party.get(actor["name"]) or {}).get("owner_id")
            stalled_min = (time.time() - st.get("since", time.time())) / 60
            if owner_id and owner_id != interaction.user.id and not is_admin:
                # auto-skip a stalled PC turn (default 10 min) so tables flow
                if not actor.get("npc") and stalled_min >= 10:
                    await interaction.response.send_message(
                        f"⏭ {actor['name']} 的回合停滯超過 {int(stalled_min)} 分鐘，"
                        f"自動跳過，輪到你的角色行動。")
                else:
                    await interaction.response.send_message(
                        f"⏳ 現在輪到 **{actor['name']}**（{actor['name']} 的玩家"
                        f"行動）。等一下、用 `/voteskip` 投票跳過，或管理員 "
                        f"`/takeover`。（你的發言會被記住，但不會推進戰鬥）")
                    return
            hp_now = (party.get(actor["name"]) or {}).get("hp_now")
            if hp_now is not None and int(hp_now) <= 0:
                await interaction.response.send_message(
                    f"💀 **{actor['name']}** 已倒地（HP 0）——只能進行死亡豁免，"
                    "無法行動！請同伴救治。")
                return
            if not text.strip():
                # no text given: quick-pick panel (canned 5e actions, carried
                # items, custom modal) for the acting character
                bot = self
                actor_name = actor["name"]

                class _Custom(discord.ui.Modal, title="自訂行動 / Custom action"):
                    action = discord.ui.TextInput(
                        label="行動描述",
                        style=discord.TextStyle.paragraph, max_length=500)

                    async def on_submit(self, minter: discord.Interaction):
                        await bot._act_execute(minter, actor_name,
                                               self.action.value)

                class _Panel(discord.ui.View):
                    @discord.ui.select(
                        placeholder=f"{actor_name} 的行動——挑選常用動作、物品或自訂",
                        options=_action_options(self.engine, cid, actor_name))
                    async def pick(self, inner: discord.Interaction,
                                   sel: discord.ui.Select):
                        v = sel.values[0]
                        if v == "__custom__":
                            await inner.response.send_modal(_Custom())
                            return
                        await bot._act_execute(inner, actor_name, v)

                await interaction.response.send_message(
                    f"⚔️ **{actor_name}** 的行動——挑選或自訂"
                    "（僅你看得到 / only you see this）：",
                    view=_Panel(timeout=300), ephemeral=True)
                return
            await _announce(interaction,
                            f"— **{actor['name']}** 行動 / acts: `{_clip(text, 200)}`")
            await self._dm_turn_interaction(
                interaction, actor["name"], text,
                user_id=str(interaction.user.id),
                combat_turn=True)

        async def attack_target_autocomplete(interaction, current: str):
            """Live target list: enemies in combat (with HP) + party members."""
            cid = str(interaction.channel_id)
            entries = []  # (display, value)
            st = self.engine.combat_get("discord", cid)
            if st and st.get("order"):
                for o in st["order"]:
                    if o.get("npc"):
                        hp = (f" HP {o['hp']}/{o.get('hp_max', '?')}"
                              if "hp" in o else "")
                        entries.append((f"{o['name']}{hp}（敵）", o["name"]))
            for n, v in self.engine.get_party("discord", cid).items():
                if isinstance(v, dict) and v.get("owner"):
                    entries.append((f"{n}（玩家角色）", n))
            q = (current or "").strip().lower()
            res = [app_commands.Choice(name=d[:100], value=val[:100])
                   for d, val in entries if not q or q in d.lower()]
            if not res:
                res = [app_commands.Choice(name="AC 15（自訂護甲等級）",
                                           value="AC 15")]
            return res[:25]

        async def attack_move_autocomplete(interaction, current: str):
            """招式 list for the invoker's OWN character: class signature
            attacks + carried weapons (live from the party sheet). Admins
            who own no character fall back to the first party member."""
            cid = str(interaction.channel_id)
            party = self.engine.get_party("discord", cid)
            is_admin = interaction.user.guild_permissions.manage_guild
            owned = [n for n, v in party.items() if isinstance(v, dict)
                     and str(v.get("owner_id") or "") ==
                     str(interaction.user.id)]
            mine = owned or ([n for n, v in party.items()
                              if isinstance(v, dict)] if is_admin else [])
            if not mine:
                return [app_commands.Choice(name="（先用 /pc 建立角色）",
                                            value="")]
            moves = self.engine.attack_moves("discord", cid, mine[0])
            q = (current or "").strip().lower()
            out = []
            for m in moves:
                label = (f"{m['name']}（{m['ability']}，傷害 {m['dmg']}"
                         + ("，範圍" if m["aoe"] else "") + "）")
                if not q or q in label.lower() or q in m["name"].lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=m["name"][:100]))
            return out[:25]

        async def any_char_autocomplete(interaction, current: str):
            """All party characters — the in-command picker for admin tools."""
            cid = str(interaction.channel_id)
            q = (current or "").strip().lower()
            out = []
            for n, v in self.engine.get_party("discord", cid).items():
                if isinstance(v, dict) and v.get("owner"):
                    label = f"{n}（{v['occupation']}，{v['owner']}）"
                    if not q or q in label.lower():
                        out.append(app_commands.Choice(name=label[:100],
                                                       value=n[:100]))
            return out[:25]

        def _filled_character(interaction) -> str | None:
            """The character option already filled in this invocation."""
            for o in (interaction.data or {}).get("options", []):
                if o.get("name") == "character":
                    return (o.get("value") or "").strip() or None
            return None

        async def admin_move_autocomplete(interaction, current: str):
            """Moves for the character already chosen in this invocation."""
            cid = str(interaction.channel_id)
            char = _filled_character(interaction)
            if not char:
                return []
            moves = self.engine.attack_moves("discord", cid, char)
            q = (current or "").strip().lower()
            out = []
            for m in moves:
                label = (f"{m['name']}（{m['ability']}，傷害 {m['dmg']}"
                         + ("，範圍" if m["aoe"] else "") + "）")
                if not q or q in label.lower() or q in m["name"].lower():
                    out.append(app_commands.Choice(name=label[:100],
                                                   value=m["name"][:100]))
            return out[:25]

        async def item_admin_autocomplete(interaction, current: str):
            """Suggest item names: the party's carried items plus a small
            standard catalog — free text still allowed."""
            cid = str(interaction.channel_id)
            names: list[str] = []
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and cid == v4ch and self.v4 is not None:
                for stacks in self.v4.game.inventory.values():
                    names += [n for n, _q in stacks]
            else:
                for _cn, entries in self.engine.inv_list("discord", cid).items():
                    names += [n for _k, n, _q in entries]
            names += ["治療藥水", "火把", "繩索", "匕首", "長劍", "木盾",
                      "乾糧", "解毒劑"]
            seen, out = set(), []
            q = (current or "").strip().lower()
            for n in names:
                if n in seen:
                    continue
                seen.add(n)
                if not q or q in n.lower():
                    out.append(app_commands.Choice(name=n[:100],
                                                   value=n[:100]))
            return out[:25]

        @self.tree.command(name="give-admin",
                           description="(Admin) give an item to ANY character / 管理員給予物品")
        @app_commands.describe(character="要給予的角色（清單挑選）",
                               item="物品名（清單挑選或自填）",
                               qty="數量（預設 1）")
        @app_commands.autocomplete(character=any_char_autocomplete,
                                   item=item_admin_autocomplete)
        async def give_admin_cmd(interaction: discord.Interaction,
                                 character: str, item: str, qty: int = 1):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "🚫 僅伺服器管理員可使用 /give-admin。")
                return
            cid = str(interaction.channel_id)
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            await _announce(interaction,
                            f"gives **{item}×{qty}** to **{character}** "
                            f"(`{ '/give-admin' }`)")
            if v4ch and cid == v4ch:
                out = self._v4_service().admin_give(character, item, qty)
                await interaction.followup.send(out)
                return
            party = self.engine.get_party("discord", cid)
            if not isinstance(party.get(character), dict):
                await interaction.followup.send(
                    f"❓ No character named {character}.")
                return
            self.engine.inv_add("discord", cid, character, item, qty)
            await interaction.followup.send(
                f"🎒 {character} now carries: "
                f"{self.engine.inv_text('discord', cid, character)}")

        @self.tree.command(name="say-admin",
                           description="(Admin) act as ANY character / 管理員代打")
        @app_commands.describe(text="該角色的行動或台詞",
                               character="要代打的角色（清單挑選）")
        @app_commands.autocomplete(character=any_char_autocomplete)
        async def say_admin_cmd(interaction: discord.Interaction,
                                text: str, character: str):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "🚫 僅伺服器管理員可使用 /say-admin。")
                return
            cid = str(interaction.channel_id)
            entry = self.engine.get_party("discord", cid).get(character)
            if not isinstance(entry, dict):
                await interaction.response.send_message(
                    f"❓ 找不到角色「{character}」。")
                return
            if entry.get("hp_now") is not None and int(entry["hp_now"]) <= 0:
                await interaction.response.send_message(
                    f"💀 **{character}** 已倒地（HP 0），無法行動。")
                return
            # act as the character's true owner so ownership rules hold
            owner_id = str(entry.get("owner_id") or interaction.user.id)
            st = self.engine.combat_get("discord", cid)
            my_turn = False
            if st and st.get("order"):
                _npcs, act = self.engine.combat_act_window(st)
                my_turn = act is not None and act["name"] == character
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and cid == v4ch:
                await self._v4_admin_turn(interaction, character, text)
                return
            echo = (f"🎛 **{interaction.user.display_name}** (admin) takes over "
                    f"**{character}**：{_clip(text, 300)}")
            await self._dm_turn_interaction(
                interaction, character, text, echo_prefix=echo,
                user_id=owner_id, combat_turn=my_turn)

        @self.tree.command(name="act-admin",
                           description="(Admin) combat action as ANY character / 管理員戰鬥代打")
        @app_commands.describe(text="戰鬥行動（留空＝下拉挑選常用動作／物品／自訂）",
                               character="要代打的角色（清單挑選）")
        @app_commands.autocomplete(character=any_char_autocomplete)
        async def act_admin_cmd(interaction: discord.Interaction,
                                text: str = "", character: str = ""):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "🚫 僅伺服器管理員可使用 /act-admin。")
                return
            cid = str(interaction.channel_id)
            if not character.strip():
                await interaction.response.send_message(
                    "⚠️ 請在指令內選擇角色（character）。")
                return
            if text.strip():
                v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
                if v4ch and cid == v4ch:
                    await self._v4_admin_turn(interaction,
                                              character.strip(), text)
                    return
                await self._admin_combat_act(interaction, cid,
                                             character.strip(), text)
                return
            # empty text: quick-pick panel (canned 5e actions, carried items,
            # custom modal) for the chosen character
            engine = self.engine
            bot = self
            char = character.strip()

            class _Custom(discord.ui.Modal, title="自訂行動 / Custom action"):
                action = discord.ui.TextInput(
                    label="行動描述",
                    style=discord.TextStyle.paragraph, max_length=500)

                async def on_submit(self, minter: discord.Interaction):
                    await bot._admin_combat_act(minter, cid, char,
                                                self.action.value)

            class _Panel(discord.ui.View):
                @discord.ui.select(
                    placeholder=f"{char} 的戰鬥行動——挑選常用動作、物品或自訂",
                    options=_action_options(engine, cid, char))
                async def pick(self, inner: discord.Interaction,
                               sel: discord.ui.Select):
                    v = sel.values[0]
                    if v == "__custom__":
                        await inner.response.send_modal(_Custom())
                        return
                    await bot._admin_combat_act(inner, cid, char, v)

            await interaction.response.send_message(
                f"⚔️ **{char}** 的戰鬥行動——挑選或自訂"
                "（僅你看得到 / only you see this）：",
                view=_Panel(timeout=300), ephemeral=True)

        @self.tree.command(name="attack-admin",
                           description="(Admin) attack with ANY character / 管理員代打攻擊")
        @app_commands.describe(
            character="要代打的角色（清單挑選）",
            target="目標（清單挑選或『AC 15』）",
            move="招式（依所選角色列出）",
            bonus="Override the attack bonus (optional)")
        @app_commands.autocomplete(character=any_char_autocomplete,
                                   target=attack_target_autocomplete,
                                   move=admin_move_autocomplete)
        async def attack_admin_cmd(interaction: discord.Interaction,
                                   character: str, target: str,
                                   move: str = "", bonus: int = None):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "🚫 僅伺服器管理員可使用 /attack-admin。")
                return
            cid = str(interaction.channel_id)
            party = self.engine.get_party("discord", cid)
            entry = party.get(character)
            if not isinstance(entry, dict):
                await interaction.response.send_message(
                    f"❓ 找不到角色「{character}」。")
                return
            verdict, err = self.engine.player_attack(
                "discord", cid, character, target, bonus=bonus, move=move)
            if err:
                await _announce(interaction, f"(admin) {character} attacks "
                                             f"`{_clip(target, 60)}`")
                await interaction.followup.send(f"⚠️ {err}")
                return
            mv = f"（{verdict['move']}）" if verdict.get("move") else ""
            tag = "💥範圍攻擊！" if verdict.get("aoe") else ""
            await _announce(interaction,
                            f"(admin) **{character}** attacks "
                            f"**{verdict['target']}**{mv}{tag}")
            st = self.engine.combat_get("discord", cid)
            my_turn = False
            if st and st.get("order"):
                _npcs, act = self.engine.combat_act_window(st)
                my_turn = act is not None and act["name"] == character
            owner_id = str(entry.get("owner_id") or interaction.user.id)
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and cid == v4ch:
                await self._v4_admin_turn(
                    interaction, character,
                    f"攻擊 {verdict['target']}" +
                    (f" {verdict.get('move')}" if verdict.get("move") else ""))
                return
            await self._dm_turn_interaction(
                interaction, character, f"我攻擊 {verdict['target']}！",
                user_id=owner_id, combat_turn=my_turn,
                attack_verdict=verdict)

        @self.tree.command(name="roll-admin",
                           description="(Admin) roll for any character / 管理員代擲")
        @app_commands.describe(
            expr="Dice expression (default d20); a d20 settles that character's pending check",
            character="要代擲的角色（清單挑選）")
        @app_commands.autocomplete(character=any_char_autocomplete)
        async def roll_admin_cmd(interaction: discord.Interaction,
                                 expr: str = "d20", character: str = ""):
            if not interaction.user.guild_permissions.manage_guild:
                await interaction.response.send_message(
                    "🚫 僅伺服器管理員可使用 /roll-admin。")
                return
            cid = str(interaction.channel_id)
            if not character.strip():
                await interaction.response.send_message(
                    "⚠️ 請在指令內選擇角色（character）。")
                return
            character = character.strip()
            entry = self.engine.get_party("discord", cid).get(character)
            if not isinstance(entry, dict):
                await interaction.response.send_message(
                    f"❓ 找不到角色「{character}」。")
                return
            expr = (expr or "d20").strip() or "d20"
            try:
                _total, _bd = roll_expr(expr)
            except ValueError as e:
                await interaction.response.send_message(f"⚠️ {e}")
                return
            owner = entry.get("owner") or interaction.user.display_name
            owner_id = str(entry.get("owner_id") or interaction.user.id)
            # a check-shaped roll settles THAT character's pending check with
            # the actual d20; otherwise reply & discard
            pend = self.engine.get_pending_check("discord", cid)
            die = _check_roll_die(expr)
            if pend is not None and pend.get("char") == character \
                    and die is not None:
                await _announce(interaction,
                                f"(admin) rolls for **{character}**（{owner}）："
                                f"`{expr}` → d20 **{die}**")
                await self._settle_pending_with_die(
                    interaction, character, owner_id, die)
            else:
                result = quick_roll(expr)
                await _announce(interaction,
                                f"(admin) rolls for **{character}**（{owner}）："
                                f"{result}")
                await interaction.followup.send(
                    result + "\n_(該角色目前沒有待決檢定——這顆骰不影響劇情)_")

        @self.tree.command(name="continue",
                           description="Nudge the DM if the game stalls / 遊戲卡住時推進劇情")
        async def continue_cmd(interaction: discord.Interaction):
            cid = str(interaction.channel_id)
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and cid == v4ch:
                # v4: check for stuck state, then re-narrate the scene
                # (announce skipped — the v4 output IS the response)
                svc = self._v4_service()
                g = svc.game
                lines = []
                if svc.pending is not None:
                    p = svc.pending
                    lines.append(f"❓ 待確認：**{p.actor or '?'} {p.action}"
                                 f" {p.target or p.destination or p.item or ''}**"
                                 "——回覆「確認」執行")
                if g.combat.active:
                    cur = g.combat.current()
                    if cur and not cur.get("npc"):
                        lines.append(f"⚔️ 戰鬥 R{g.combat.round}——輪到 "
                                     f"**{cur['name']}**")
                    elif cur:
                        lines.append(f"⚔️ 戰鬥 R{g.combat.round}——敵方回合，"
                                     "用 /act 行動或 pass")
                if not lines:
                    lines.append("🔄 場景：" + g.world.here.name)
                    # re-narrate the scene with a fresh hook
                    facts = [e.text for e in g.ledger.entries[-5:]]
                    brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                                     for n, e in g.party.items())
                    narr = await svc.narrator.narrate(
                        facts, g.world.here.name, brief,
                        hints=[f"眾人目前在{g.world.here.name}。"
                               f"{g.world.here.description}",
                               "給玩家一個新的視角或鉤子，推進故事。"])
                    if narr:
                        lines.append("📖 " + narr)
                await interaction.response.send_message(
                    "\n".join(lines)[:1900])
                return
            if self.engine.turn_in_flight("discord", cid):
                await interaction.response.send_message(
                    "⏳ DM 仍在生成上一個回應——請再等一下；真的卡死時再按一次 /continue。")
                return
            await _announce(interaction, "runs `/continue`（遊戲停滯，推進劇情）")
            await self._dm_turn_interaction(
                interaction, "party", "（推進劇情）",
                system_suffix=(
                    "CONTINUE（玩家回報遊戲停滯）：檢查最近的玩家行動——"
                    "若尚未回應，現在回應它；若已回應，推進新劇情（新的發現、"
                    "NPC 反應、環境變化或新的選擇）。嚴禁重複任何先前的敘述內容。"
                    "80-150 字，繁體中文。"),
                user_id=str(interaction.user.id))

        @self.tree.command(name="say", description="Act in the story (freeform)")
        @app_commands.describe(text="What your character does or says")
        async def say_cmd(interaction: discord.Interaction, text: str):
            v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
            if v4ch and str(interaction.channel_id) == v4ch:
                await self._v4_command_turn(interaction, text)
                return
            echo = f"💬 **{interaction.user.display_name}:** {_clip(text, 1800)}"
            await self._dm_turn_interaction(interaction, interaction.user.display_name,
                                           text, echo_prefix=echo)

    # ---------- turn handling ----------

    async def _dm_turn_interaction(self, interaction: discord.Interaction,
                                   name: str, text: str, system_suffix: str | None = None,
                                   echo_prefix: str | None = None,
                                   user_id: str = "",
                                   combat_turn: bool = False,
                                   attack_verdict: dict | None = None,
                                   forced_d20: int | None = None):
        # echo_prefix: post the player's exact slash input verbatim (slash
        # messages are invisible to the channel otherwise); never LLM-edited.
        # user_id: whose action this is (an /act may act for the character's
        # owner under admin takeover); defaults to the invoking user.
        v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
        if v4ch and str(interaction.channel_id) == v4ch:
            await interaction.response.send_message(
                "🧪 此頻道已由 **v4 引擎**接管——直接打字就行"
                "（v3 回合指令在此頻道停用）。", ephemeral=True)
            return
        if echo_prefix is not None:
            await interaction.response.send_message(echo_prefix)
        elif not interaction.response.is_done():
            # commands that _announce() first have already used the response
            await interaction.response.defer()
        follow = await interaction.followup.send("🎲 DM 正在思考… The DM ponders…")
        last_edit = [0.0]

        async def _safe_edit(content: str):
            try:
                await follow.edit(content=_clip(content) + " ▍")
            except discord.HTTPException:
                pass

        def on_delta(acc: str):
            now = asyncio.get_event_loop().time()
            if now - last_edit[0] >= EDIT_INTERVAL and len(acc) > 20:
                last_edit[0] = now
                asyncio.create_task(_safe_edit(acc))

        try:
            reply = await self.engine.dm_reply(
                "discord", str(interaction.channel_id), name, text, on_delta,
                system_suffix=system_suffix,
                user_id=user_id or
                (str(interaction.user.id) if interaction.user else ""),
                combat_turn=combat_turn,
                attack_verdict=attack_verdict,
                forced_d20=forced_d20)
        except Exception as e:
            log.exception("dm_reply failed")
            await follow.edit(content=f"⚠️ {e}")
            return
        if not reply:
            await follow.edit(content="🎲 …(silence)")
            return
        chunks = _split(reply)
        await follow.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    async def _dm_turn_channel(self, channel, name: str, text: str,
                               system_suffix: str | None = None,
                               user_id: str = "",
                               combat_turn: bool = False):
        v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
        if v4ch and str(channel.id) == v4ch:
            await channel.send("🧪 此頻道已由 **v4 引擎**接管——直接打字就行"
                               "（v3 回合指令在此頻道停用）。")
            return
        status = await channel.send("🎲 DM 正在思考… The DM ponders…")
        last_edit = [0.0]

        async def _safe_edit(content: str):
            try:
                await status.edit(content=content)
            except discord.HTTPException:
                pass

        def on_delta(acc: str):
            now = asyncio.get_event_loop().time()
            if now - last_edit[0] >= EDIT_INTERVAL and len(acc) > 20:
                last_edit[0] = now
                asyncio.create_task(_safe_edit(_clip(acc) + " ▍"))

        try:
            reply = await self.engine.dm_reply("discord", str(channel.id), name, text,
                                               on_delta, system_suffix=system_suffix,
                                               user_id=user_id,
                                               combat_turn=combat_turn)
        except Exception as e:
            log.exception("dm_reply failed")
            await status.edit(content=f"⚠️ {e}")
            return
        if not reply:
            await status.edit(content="🎲 …(silence)")
            return
        chunks = _split(reply)
        await status.edit(content=chunks[0])
        for chunk in chunks[1:]:
            await channel.send(chunk)

    async def _settle_pending_with_die(self, interaction: discord.Interaction,
                                       char: str, user_id: str, die: int):
        """Settle the chat's pending 判定 with an already-rolled die: the DM
        narrates per the verdict, and the combat turn is consumed when it was
        that character's move (a failed LLM call still never burns the turn)."""
        cid = str(interaction.channel_id)
        st = self.engine.combat_get("discord", cid)
        my_turn = False
        if st and st.get("order"):
            _npcs, act = self.engine.combat_act_window(st)
            my_turn = act is not None and act["name"] == char
        await self._dm_turn_interaction(
            interaction, char, "骰", user_id=user_id,
            combat_turn=my_turn, forced_d20=die)

    async def _act_execute(self, interaction: discord.Interaction,
                           char: str, text: str):
        """Execute a combat move for char from a component interaction,
        re-validating turn ownership and HP at execution time."""
        cid = str(interaction.channel_id)
        st = self.engine.combat_get("discord", cid)
        if not st or not st.get("order"):
            await interaction.response.send_message(
                "🏁 戰鬥已結束——行動未執行。")
            return
        _npcs, actor = self.engine.combat_act_window(st)
        if actor is None or actor["name"] != char:
            cur = actor["name"] if actor else "?"
            await interaction.response.send_message(
                f"⚠️ 現在輪到 **{cur}**——**{char}** 的行動未執行。")
            return
        hp = (self.engine.get_party("discord", cid).get(char) or {}).get("hp_now")
        if hp is not None and int(hp) <= 0:
            await interaction.response.send_message(
                f"💀 **{char}** 已倒地（HP 0），無法行動。")
            return
        echo = f"⚔️ **{char}** 行動 / acts: `{_clip(text, 200)}`"
        await self._dm_turn_interaction(
            interaction, char, text, echo_prefix=echo,
            user_id=str(interaction.user.id), combat_turn=True)

    async def _admin_combat_act(self, interaction: discord.Interaction,
                                cid: str, char: str, text: str):
        """Admin drives any character's combat action; acts as the char's
        true owner, consuming the turn only if it's theirs."""
        st = self.engine.combat_get("discord", cid)
        if not st or not st.get("order"):
            await interaction.response.send_message(
                "🏁 目前沒有戰鬥——非戰鬥行動請用 /say-admin。")
            return
        entry = self.engine.get_party("discord", cid).get(char)
        if not isinstance(entry, dict):
            await interaction.response.send_message(
                f"❓ 找不到角色「{char}」。")
            return
        if entry.get("hp_now") is not None and int(entry["hp_now"]) <= 0:
            await interaction.response.send_message(
                f"💀 **{char}** 已倒地（HP 0），無法行動。")
            return
        owner_id = str(entry.get("owner_id") or interaction.user.id)
        _npcs, actor = self.engine.combat_act_window(st)
        my_turn = actor is not None and actor["name"] == char
        echo = (f"🎛 **{interaction.user.display_name}** (admin) — **{char}** "
                f"戰鬥行動：`{_clip(text, 200)}`")
        await self._dm_turn_interaction(
            interaction, char, text, echo_prefix=echo,
            user_id=owner_id, combat_turn=my_turn)
        if not my_turn:
            cur = actor["name"] if actor else "?"
            try:
                await interaction.followup.send(
                    f"_（注意：現在輪到 **{cur}**——此行動不會推進輪替）_",
                    ephemeral=True)
            except discord.HTTPException:
                pass

    # ---------- message handling (full mode / DMs) ----------

    def _wants_reply(self, message: discord.Message) -> bool:
        if self.user and message.guild is None:
            return True  # DMs always work
        if self.user and self.user in message.mentions:
            return True
        if message.channel and message.channel.id in self.bound_channels:
            return True
        return False

    def _v4_service(self):
        """Lazy-init the engine-driven service (shared by the channel
        router and admin commands)."""
        if self.v4 is None:
            from v4.service import V4Service
            self.v4 = V4Service(
                os.environ.get("DATA_DIR", "data"),
                os.environ.get("LLM_URL", "http://127.0.0.1:11434"),
                os.environ.get("V4_DIGEST_MODEL", "gemma3:12b-it-qat"),
                os.environ.get("V4_NARR_MODEL", "gemma3:27b-it-qat"))
        return self.v4

    async def _v4_admin_turn(self, interaction: discord.Interaction,
                             character: str, text: str):
        """Admin drives a character through the v4 pipeline: construct the
        intent directly (no digestor needed), resolve, narrate."""
        await interaction.response.defer(thinking=True)
        status = await interaction.followup.send(
            f"🎛 **{interaction.user.display_name}** (admin) → "
            f"**{character}**：`{_clip(text, 200)}`")
        svc = self._v4_service()
        g = svc.game
        if character not in g.party:
            await status.edit(content=f"❓ 沒有角色「{character}」")
            return
        from v4.intent import Intent
        # try the deterministic parser first; fall back to digestor
        it = None
        from v4.intent import parse_command
        it = parse_command(text, party_names=list(g.party))
        if it is None or it.actor != character:
            # force the actor and try digestor for freeform
            it = await svc.digestor.digest(
                text, list(g.party), g.world.here.name,
                list(g.world.here.exits.values()),
                known_targets=list(g.enemies) +
                [n["name"] for n in g.world.here.npcs] + list(g.party))
        it.actor = character  # admin override: this character acts
        from v4.rules_core import resolve as v4_resolve
        r = v4_resolve(g, it)
        lines = r.lines
        narration = ""
        if r.accepted and r.lines:
            from v4.templates import render_hint
            idx0 = len(g.ledger.entries) - \
                sum(1 for e in g.ledger.entries if e.turn == g.ledger.turn)
            hints = [h for h in (render_hint(e, g)
                                 for e in g.ledger.entries[idx0:]) if h]
            facts = [e.text for e in g.ledger.entries[idx0:]]
            brief = "；".join(f"{n} {e['hp_now']}/{e['hp_max']}HP"
                             for n, e in g.party.items())
            narration = await svc.narrator.narrate(
                facts, g.world.here.name, brief, hints=hints)
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        body = "\n".join(lines)
        if body:
            await interaction.followup.send(_clip(body))
        if narration:
            await interaction.followup.send("📖 " + _clip(narration))

    async def _v4_message(self, message: discord.Message):
        """Plain text in the v4 channel = table talk: ledger it, stay quiet.
        The DM is triggered explicitly — /say or /act."""
        try:
            self._v4_service().table_talk(message.content,
                                          message.author.display_name)
        except Exception:
            log.exception("v4 table_talk failed")
        try:
            await message.add_reaction("💬")
        except discord.HTTPException:
            pass

    async def _v4_command_turn(self, interaction: discord.Interaction,
                               text: str):
        """A slash command (/say, /act) explicitly addresses the DM:
        digest → engine (instant) → narrate."""
        await interaction.response.defer(thinking=True)
        status = await interaction.followup.send(
            f"⚙️ <@{interaction.user.id}> 引擎處理中…")
        try:
            lines, narration = await self._v4_service().handle(
                text, interaction.user.display_name)
        except Exception as e:
            log.exception("v4 turn failed")
            await status.edit(content=f"⚠️ {e}")
            return
        try:
            await status.delete()
        except discord.HTTPException:
            pass
        body = "\n".join(lines)
        if body:
            await interaction.followup.send(_clip(body))
        elif not narration:
            await interaction.followup.send(
                f"💬 <@{interaction.user.id}> 已收到（桌邊對話——用 `/say` "
                "對 DM 說話）")
        if narration:
            await interaction.followup.send("📖 " + _clip(narration))

    async def on_message(self, message: discord.Message):
        if message.author.bot or not self.user:
            return
        content = message.content.strip()
        if not content:
            return  # empty in guilds when message content intent is off
        # v4 playground channel (engine-driven flow; v3 everywhere else)
        v4ch = (os.environ.get("V4_CHANNEL_ID") or "").strip()
        if v4ch and str(message.channel.id) == v4ch:
            await self._v4_message(message)
            return
        cid = str(message.channel.id)
        mentioned = self.user in message.mentions
        for m in message.mentions:
            if m == self.user:
                content = content.replace(f"<@{m.id}>", "").strip()

        # natural-language meta-commands ("roll a d20", "開新團：血月城堡"):
        # when tagging the bot (or in DMs), detect and rewrite to the command
        if mentioned or message.guild is None:
            intent = parse_intent(content)
            if intent:
                content = f"!{intent[0]} {intent[1]}".strip()

        low = content.lower()
        if low.startswith(("!help", "!commands")):
            for chunk in _split(DISCORD_ZH) + _split(DISCORD_EN):
                await message.channel.send(chunk)
            return
        if low.startswith("!status"):
            await message.channel.send(_clip(await build_status(self.engine, "discord", cid)))
            return
        if low.startswith("!here"):
            self.bound_channels.add(message.channel.id)
            await message.channel.send("🎲 本頻道已成為遊戲桌面，這裡的對話 DM 都會記住。"
                                       "需要回應時請 @mention 我。\n"
                                       "This channel is now the adventure table — "
                                       "@mention me when you want a reply.")
            return
        if low.startswith("!roll"):
            expr = content[5:].strip()
            pend = self.engine.get_pending_check("discord", cid)
            if pend and (not expr or expr.lower() in ("d20", "roll", "骰")):
                await self._dm_turn_channel(
                    message.channel, message.author.display_name, "骰",
                    user_id=str(message.author.id))
                return
            try:
                result = quick_roll(expr) if expr else quick_roll("d20")
            except ValueError as e:
                await message.channel.send(f"⚠️ {e}")
                return
            self.engine.log_message("discord", cid, message.author.display_name,
                                    f"🎲 {message.author.display_name} {result}",
                                    str(message.author.id))
            await message.channel.send(result)
            return
        if low.startswith("!pc"):
            arg = content[3:].strip()
            if not arg:
                await message.channel.send(f"```\n{self.engine.party_text('discord', cid)}\n```")
                return
            parts = arg.split(maxsplit=1)
            name = parts[0]
            rest = parts[1].strip() if len(parts) > 1 else ""
            # optional "@player" at the end: admin-only assignment
            mention = re.search(r"<@!?(\d+)>", rest)
            assigned = None
            if mention:
                target = message.guild.get_member(int(mention.group(1))) if message.guild else None
                if target is None:
                    await message.channel.send("❓ I can't see that member in this server.")
                    return
                if not message.author.guild_permissions.manage_guild:
                    await message.channel.send(
                        "🚫 Only server admins can assign a character to another player.")
                    return
                assigned = (target.display_name, target.id)
                rest = rest[:mention.start()].strip()
            party = self.engine.get_party("discord", cid)
            if rest.lower().startswith("remove"):
                entry = party.get(name)
                if entry is None:
                    await message.channel.send(f"❓ No character named {name}.")
                    return
                char_owner = entry.get("owner", "?") if isinstance(entry, dict) else "?"
                char_owner_id = entry.get("owner_id") if isinstance(entry, dict) else None
                is_owner = char_owner_id == message.author.id
                is_admin = message.author.guild_permissions.manage_guild
                if not (is_owner or is_admin):
                    await message.channel.send(
                        f"🚫 Only {char_owner} (or a server admin) can remove **{name}**.")
                    return
                if rest.lower() != "remove confirm":
                    await message.channel.send(
                        f"⚠️ This permanently removes **{name}**. To confirm: "
                        f"`!pc {name} remove confirm`")
                    return
                party.pop(name, None)
                self.engine.set_party("discord", cid, party)
                self.engine.log_bond("discord", cid, name,
                                     char_owner, char_owner_id, "unbind")
                await message.channel.send(
                    f"🧙 Removed {name}. / 已移除 {name}。\n"
                    f"🔗 綁定已解除：**{char_owner} ↔ {name}**")
                return
            if assigned is not None:
                owner, owner_id = assigned
            else:
                owner, owner_id = message.author.display_name, message.author.id
            # structured creation: first token after name must be a class
            tokens = rest.split(maxsplit=1)
            occ = resolve_occupation(tokens[0]) if tokens else None
            if occ is None:
                await message.channel.send(
                    f"⚠️ '{tokens[0] if tokens else '?'}' is not a class — see `/classes`. "
                    f"Usage: `!pc {name} <occupation> [details]`")
                return
            details = tokens[1].strip() if len(tokens) > 1 else ""
            sdict = roll_stats_by_class(occ)  # always rolled on the text path
            hp = starting_hp(occ, sdict)
            from engine.charlib import default_ac, slots_for
            ac = default_ac(occ, sdict)
            slots = {str(k): v for k, v in slots_for(occ, 1).items()}
            party[name] = {"occupation": occ, "stats": sdict,
                           "hp_now": hp, "hp_max": hp, "details": details,
                           "owner": owner, "owner_id": owner_id,
                           "ac": ac, "slots": slots, "hd_used": 0}
            self.engine.set_party("discord", cid, party)
            self.engine.log_bond("discord", cid, name, owner, owner_id, "bind")
            slot_txt = (f"\n✨ 法術格: " + " · ".join(f"L{k} ×{v}"
                                                  for k, v in slots.items())
                        if slots else "")
            await message.channel.send(
                f"🧙 **{name}** — {occ} (controlled by {owner}) "
                f"🛡 AC {ac} ❤️{hp}/{hp}{slot_txt}\n"
                f"🔗 玩家綁定：**{owner} ↔ {name}**（已記錄，`/bonds` 可查）\n"
                f"{format_stats(sdict)}\n"
                f"_{details or 'no details'}_\n"
                f"🎲 attributes rolled 4d6×3 by the bot")
            return
        if low.startswith("!party"):
            from engine.status import party_header
            await message.channel.send(
                f"{party_header()}\n"
                f"```\n{self.engine.party_text('discord', cid)}\n```")
            return
        if low.startswith("!rollstats"):
            await message.channel.send(rollstats_text())
            return
        if low.startswith("!inv"):
            arg = content[4:].strip()
            if arg:
                text = self.engine.inv_text("discord", cid, arg) or \
                    f"(nothing / {arg} 沒有物品)"
                await message.channel.send(f"🎒 **{arg}**: {text}")
            else:
                await message.channel.send(
                    f"```\n{self.engine.inv_all_text('discord', cid)}\n```")
            return
        if low.startswith("!classes"):
            await message.channel.send(CLASSES_TEXT)
            return
        if low.startswith("!new"):
            setting = content[4:].strip()
            if not setting:
                await message.channel.send(settings_menu("discord"))
                return
            from engine.commands import DIFFICULTY_LABELS, split_difficulty
            setting, diff_kw = split_difficulty(setting)
            diff = diff_kw or "easy"
            self.engine.reset("discord", cid, keep_party=True)
            self.engine.set_difficulty("discord", cid, diff)
            await message.channel.send(
                f"🎭 新冒險開始〔{DIFFICULTY_LABELS.get(diff, diff)}〕 "
                f"New adventure: {_clip(setting, 300)}")
            await self._dm_turn_channel(
                message.channel, "party", setting,
                system_suffix=f"NEW ADVENTURE: set the opening scene for: {setting}. "
                              f"Describe the place and situation in 80-150 words, "
                              f"introduce a hook, then ask the players what they do. "
                              f"Record every named NPC you introduce with a [[npc:名字|身分|態度]] tag and every key world fact with a [[lore:主題|事實]] tag — these are your persistent notes.")
            return
        if low.startswith("!reset"):
            self.engine.reset("discord", cid)
            await message.channel.send("🗑️ 本場次已清除。Session cleared.")
            return

        # identity guard: display name == a registered character but wrong user
        party = self.engine.get_party("discord", cid)
        entry = party.get(message.author.display_name)
        if isinstance(entry, dict) and entry.get("owner_id") and \
                entry["owner_id"] != message.author.id:
            await message.channel.send(
                f"🚫 {message.author.mention} The name **{message.author.display_name}** "
                f"is registered to another player. Please use your own name/character.")
            return
        if not self._wants_reply(message):
            # table talk: remember it so the DM has context, but stay quiet
            if message.channel.id in self.bound_channels:
                self.engine.log_message("discord", cid,
                                        message.author.display_name, content,
                                        str(message.author.id))
            return
        # combat rotation for freeform chat: the acting character's owner
        # talking IS their move; other players get logged as table talk + a
        # gentle whose-turn notice (keeps 1-player-1-action without /act)
        st = self.engine.combat_get("discord", cid)
        if st and st.get("order"):
            npcs, actor = self.engine.combat_act_window(st)
            if actor is not None and not actor.get("npc"):
                party = self.engine.get_party("discord", cid)
                owner_id = (party.get(actor["name"]) or {}).get("owner_id")
                _hp = (party.get(actor["name"]) or {}).get("hp_now")
                if _hp is not None and int(_hp) <= 0:
                    await message.channel.send(
                        f"💀 **{actor['name']}** 已倒地（HP 0）——只能進行死亡"
                        "豁免，無法行動！請同伴救治。")
                    return
                if owner_id and owner_id != message.author.id:
                    self.engine.log_message(
                        "discord", cid, message.author.display_name, content,
                        str(message.author.id))
                    await message.channel.send(
                        f"⚔️ 目前輪到 **{actor['name']}** — 你的話已記下，"
                        "但不會推進戰鬥（等輪到你、`/voteskip`，或管理員"
                        " `/takeover`）。")
                    return
                await self._dm_turn_channel(
                    message.channel, actor["name"], content or "[the player stays silent]",
                    user_id=str(message.author.id), combat_turn=True)
                return
        await self._dm_turn_channel(message.channel, message.author.display_name,
                                    content or "[the player stays silent]",
                                    user_id=str(message.author.id))


def _clip(s: str, limit: int = 1900) -> str:
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _split(text: str, limit: int = 1900):
    """Split text into chunks that fit Discord's message limit."""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


async def run_discord(engine: DMEngine, token: str):
    bot = DiscordBot(engine, message_content=True)
    try:
        await bot.start(token)
    except discord.errors.PrivilegedIntentsRequired:
        log.warning(
            "MESSAGE CONTENT INTENT is not enabled in the developer portal — "
            "falling back to slash-command mode. Use /say, /new, /roll, etc.; "
            "freeform channel chat returns once the intent is enabled "
            "(developer portal -> Bot -> Privileged Gateway Intents).")
        await DiscordBot(engine, message_content=False).start(token)
