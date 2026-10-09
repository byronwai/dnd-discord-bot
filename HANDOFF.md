# D&D DM Bot — Handoff Document

**Date**: 2026-10-08 (post Discord-only + 27b + rules-v2 revamp) · **Status**: code deployed & verified, **dm-bot STOPPED+DISABLED by request** (bring back: `sudo systemctl enable --now dm-bot`; DISCORD_TOKEN intact in .env; gemma4 removed from Ollama)

## Architecture

```
Discord (Dungeon-and-Dragon#1869) ── dm-bot (python, systemd) @ GB10
                                           │
                                           ├─ Ollama @ GB10 :11434
                                           │    DM brain: gemma3:27b-it-qat (16k ctx)
                                           │    Embeddings: mxbai-embed-large (1024-d)
                                           └─ SQLite: ~/dnd-dm-bot/data/
                                                campaign.db (game state)
                                                rules.db   (2918 SRD chunks, schema v2)
RPi 192.168.79.78 — COLD STANDBY (dm-bot stopped).
Same code at ~/dnd-dm-bot; its campaign.db holds the pre-migration snapshot.
```

**Discord-only since 2026-10-08**: Telegram adapter deleted; the cross-platform
shared-table feature removed (`/table_new` `/join` `/table` `/leave` gone, engine
tables/bindings machinery gone). The live shared table BTWQS6 was folded into
its Discord chat (`discord/1557388849959674006`) by `tools/migrate_discord_only.py`
(backup: `data/campaign.db.pre-discord-only.bak` on GB10).

## Servers & Access

| Host | User | Auth | Role |
|---|---|---|---|
| 10.5.28.210 (gx10-bbdf, NVIDIA GB10, 119GB RAM) | comfyui | **SSH key: `keys/dnd_ed25519`** (password `comfyui` also works; sudo same password) | PRIMARY: everything |
| 192.168.79.78 (RPi5, byronwai/P@ssw0rd) | byronwai | same SSH key + password | standby (pre-revamp code) |

`ssh -i keys/dnd_ed25519 comfyui@10.5.28.210`

## Services (GB10)

- `dm-bot` — the game bot (Discord). Logs: `sudo journalctl -u dm-bot -f`
- `ollama` — model server; context pinned to 16384 via `/etc/systemd/system/ollama.service.d/ctx.conf` (qwen3.5:35b CRASHED at 16k on GB10 — CUDA illegal access; do not raise ctx with that model)
- Also on box (untouched): ComfyUI service, tg-ollama vision bot

## Key config (~/dnd-dm-bot/.env on GB10)

`LLM_URL=http://127.0.0.1:11434` · `LLM_MODEL=gemma3:27b-it-qat` · `EMBED_MODEL=mxbai-embed-large` · `EMBED_PREFIX=` (empty — mxbai needs no bge prefix) · `MAX_TOKENS=700` · `MAX_HISTORY=40` · `DISCORD_TOKEN` inside (Telegram token retired as a comment).

**Model choice (2026-10-08, for the 128GB GB10)**: every big model already on the
box is a THINKING model unusable for chat pacing — re-verified: Qwen3.8-abliterated
and qwen3.5-abliterated:27b return empty `content` with everything in `reasoning`;
deepseek-r1:70b and CyberStrike-35B same class; qwen3-coder-next is a coder.
**gemma3:27b-it-qat** won: same proven non-thinking family as the old 12b,
streams content directly, strong 繁中, correct [[check:...]] discipline out of
the box; ~11.5 tok/s warm → 16-24s per 200-token turn (12b was 7-11s; the
quality jump is worth it). Rollback if ever needed: `LLM_MODEL=gemma3:12b-it-qat`.

## Rules grounding (the "handbook ↔ model" contract)

1. **`engine/compendium.py`** — ~2.4k-char always-on block distilled from the
   SRD core (d20 math, PB table, DC ladder, actions in combat, grapple/shove,
   cover, all conditions one-liners, death saves, concentration, rests,
   falling). Verified against `~/srd` sources. Injected into every prompt
   before the retrieval block — core mechanics no longer depend on retrieval luck.
2. **`tools/ingest_srd.py` v2** — excludes junk (Spell_Lists name indexes,
   Spells_A-Z duplicates of Spells_Each, README/Legal), classifies chunks by
   chapter (spell/monster/item/feat/class/race/rule), and embeds **title+body**
   (spell/monster names live in headings — body-only vectors missed "Fireball").
   2918 chunks (was 3479 with dupes).
3. **`engine/rules.py` v2** — zh→en glossary appended to Chinese queries before
   embedding (deterministic, ~90 terms); gating skips embedding calls on
   consent/option-pick messages; hybrid score = cosine + title overlap (with
   prefix credit: grapple~Grappling) + body overlap (top-64 only) + kind boost;
   k=3 deduped by (kind, title); LRU query cache. Validated: 「火球術」→ Fireball,
   「死亡豁免」→ Death Saving Throws, 「治療藥水」→ Potion of Healing, 「哥布林」→ Goblin.

## Game systems (all deterministic on-server)

- **Consent checks**: DM emits `[[check:DC|角色|屬性]]` → check card → player replies 骰/roll → server rolls d20+real modifier, nat-20/1 crits, verdict injected as FINAL; DM narrates per verdict. Inline `[[expr]]` only for damage/heal/loot.
- **State tags**: `[[hp:名:-4]]` `[[item:名:+藥水 x2]]` `[[scene:…]]` `[[objective:…]]` `[[xp:名:100]]` (5e levels, auto HP bump) — parsed tolerantly, applied to SQLite, audited.
- **Anti-cheat /pc**: occupation dropdown, stats = server-rolled 4d6kh3 or exact standard array (admins exempt), HP from class hit die + CON, owner = registering user id.
- **Combat v2 (2026-10-08, tag-driven)**: combat starts diegetically — the DM emits `[[combat:哥布林 x2、狼]]` and the engine rolls initiative for PCs + named NPC slots（哥布林①②…, cap 8）and renders the order card; `[[combat:end]]` finishes. `/combat start [enemies]` remains as manual override. When it's their turn players just TALK in the channel (freeform from the acting character's owner is their move) or `/move`; NPC slots are compressed into the next player's reply; rotation advances only AFTER a successful LLM reply; a PC turn stalled >10 min is auto-skipped by the next owner's move. `/voteskip`/`/takeover` remain. Stat-block tables the model pastes are scrubbed.
- **Context graph (2026-10-08)**: prompts no longer carry full history — only the last `DM_RAW_WINDOW=10` messages verbatim (voice continuity), while older turns enter as one-line deterministic event records (`events` table: actor + action snippet + engine-applied state changes/verdicts, no extra LLM call), injected as「近期事件摘要」before the raw window (placeholder-mapped; cutoff by message id). NPC/lore note injection is recency-capped at 8+8, and `touch_notes()` floats nodes mentioned in recent text (a recency graph walk). Events clear on reset. Measured: 30% smaller prompts at 20 turns of history; savings grow toward ~60% as the 40-message window fills. Turn latency also drops (fewer prompt tokens for the 27b).
- **DM notebook (2026-10-08, "on paper" in the DB)**: two campaign tables — `npcs` (name/desc/status) and `lore` (key/fact) — written by the DM via tags and injected into EVERY prompt as「DM 筆記」sections, so named NPCs and established world facts persist beyond the lossy summary. `[[npc:瑪莎|酒館老闆娘|friendly]]` upserts (two-field form with a lone status word updates status only; unchanged re-notes render nothing); `[[lore:海妖女王|族人被困在海底祭壇]]` upserts by key. `/status` shows both. `/new`/`/reset` clear the notebook (new adventure = new world). The /new opener suffix explicitly instructs recording NPCs/lore — directive-level instruction is what makes the 27b actually take notes; the constitution line alone was not enough.
- **5e mechanics gap closed (2026-10-08, engine/charlib.py + tags)**: AC on every character (SRD starting-armor defaults by class + DEX; lazy-computed for existing characters); proficiency bonus applied everywhere (attacks always proficient, saves via class save proficiencies, skill checks via class core-skill lists — zh skill names parse: 洞悉/察覺/潛行…); `[[save:DC|char|AB]]` distinct from `[[check:...]]`; `[[attack:attacker|target|+5]]` → consent attack card → server rolls vs target AC (bonus literal or ability→auto mod+PB; enemy slot AC, party AC, or literal "AC 15"); `[[enemy:哥布林①|HP 7|AC 15]]` registers combat enemies → `[[hp:哥布林①:-4]]` counts them down, all-dead auto-ends combat (combat tags process FIRST in apply_state_tags — enemies may arrive in the same reply); spell slots per SRD full/half/pact-magic tables — `[[spell:char:1]]` consumes (0=cantrip free, empty slots fail visibly), `/slots` views, long rest refills; `[[rest:short]]/[[rest:long]]` + `/rest` command (hit dice d8–d12 + CON, long = full HP + slots + half hit dice back); level-ups now grant new slot tiers filled + show PB. Party sheet shows AC · PB · slots.
- **Name placeholders (structural anti-rename)**: real character names NEVER reach the model. Each chat keeps stable `slots` (sessions column): name → `[PCn]`; aliases map to the same token; `_build_messages` swaps every message/party sheet/canonical tail to placeholders, and `_restore_names` swaps `[PCn]` back to the DB name right after generation (before tag parsing, so `[[hp:[PC1]:-4]]` works). Slot numbers persist; removed characters keep theirs reserved; full `/reset` clears slots. Fixes the 依思→Isis/Elyse、大力蕉→Dali Jiao drift seen on the 27b.
- **Language (zh-first policy, 2026-10-08)**: output is ALWAYS Traditional Chinese-first — every turn uses the full Chinese system prompt, ambiguous messages default zh, and an all-English model reply is translated back to zh. When a player writes English, the DM narrates in zh and appends one short English summary line (1-2 sentences). OpenCC s2t; canonical tail at prompt end.
- **Difficulty selection (2026-10-08)**: `/new <setting> difficulty:` 新手 Friendly（default）/ 標準 Standard / 困難 Hard — also parsed from text like「血月堡（困難）」. Persisted per session (`sessions.difficulty`), injected into the DM constitution as the 難度方針 block (DC scale, failure cost, combat lethality, XP generosity); survives `/reset`; shown in `/status`.
- **Backups**: every `/reset`//`new` snapshots party+items; `engine.restore_backup()` recovers.
- **Ownership & anti-injection (2026-10-08, hard layer)**: slash paths hard-block non-owners (`/pc` remove, `/give` `/take` `/equip`, `/move`) with 🚫 warnings; display-name impersonation guard in freeform. Every freeform turn also injects an OWNERSHIP directive (speaker may only control chars where `owner_id` matches their user id; strangers get NPC/environment-only handling), and `scrub_player_input()` neutralizes `[PCn]` placeholder spoofing, fake "SYSTEM CHECK VERDICT"/"FINAL VERDICT" preambles and injection phrases (EN+zh) before the text is stored or shown to the model — the table sees a ⚠️ 已過濾 warning when anything was scrubbed. Player-typed `[[hp]]`/`[[check]]` tags never execute (tags parse only from the DM's reply). Offline audit: `tools/ownership_audit.py`.

## Ops cheatsheet (GB10)

```bash
sudo systemctl restart dm-bot                 # after engine/code edits
sudo journalctl -u dm-bot -f                  # live logs
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/status.py          # server+game status
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/ownership_audit.py # control audit
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/playtest.py        # e2e smoke (~3 min)
# rebuild rules index (~60s on GB10):
EMBED_PREFIX= ~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/ingest_srd.py \
  ~/srd ~/dnd-dm-bot/data/rules.db http://127.0.0.1:11434 mxbai-embed-large
```

Failback to Pi: `ssh byronwai@192.168.79.78` → deploy current code + campaign.db → start dm-bot there (stop GB10's first — one poller per bot token!). Note: the Pi copy still has the pre-revamp code (Telegram + shared tables); it will not clash with the migrated DB schema (bindings/tables simply unused), but redeploy before real failback.

## Known issues / roadmap

- 27b turns are 16-24s (12b was 7-11s) — acceptable; if pacing complaints: rollback env to 12b, or try a future non-thinking mid-size release
- DM occasionally re-explains a check card or latches onto the previous pending check during combat rotation (cosmetic; engine state stays correct)
- qwen3.5:35b / Qwen3.8 unusable (thinking + CUDA crash at 16k) — revisit after ollama/driver updates
- Ideas: scene-card images via the box's ComfyUI; long-rest/short-rest command (compendium already carries the rules); initiative persistence across scenes

## Dev copies (Windows)

`D:\zcode\dnd-dm-bot\` — source of truth (md5-verified vs GB10 2026-10-08) · `data\*.db` — post-migration snapshots · `keys\dnd_ed25519(.pub)` — SSH key · `dnd-dm-bot-export-20261008.tgz` — full pre-revamp bundle (Telegram era).
