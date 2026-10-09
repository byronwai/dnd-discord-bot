# D&D DM Bot (Discord · GB10 DGX Spark)

Self-hosted Dungeon Master bot for **Discord**, powered by a local Ollama LLM
(gemma3:27b-it-qat) on an NVIDIA GB10 (DGX Spark, 128GB unified memory) —
no cloud APIs.

```
dnd-dm-bot/
├── bot.py               # entry point: starts the Discord adapter
├── engine/
│   ├── dm.py            # session persistence (SQLite), prompt, LLM streaming
│   ├── compendium.py    # always-on core-rules block (SRD distillate)
│   ├── rules.py         # SRD retrieval v2: zh glossary, gating, hybrid scoring
│   ├── dice.py          # dice roller: 2d6+3, 4d6kh3, adv/dis, [[dice]] in narration
│   ├── checks.py        # consent-based checks: [[check:DC|char|ABILITY]] cards
│   ├── state.py         # state tags: [[hp]] [[item]] [[scene]] [[objective]] [[xp]]
│   └── commands.py      # shared command text + fair character creation
├── adapters/
│   └── discord_bot.py   # /help /new /roll /pc /party /combat /move …
├── tools/
│   ├── ingest_srd.py    # build rules.db from SRD markdown (schema v2)
│   ├── migrate_discord_only.py  # one-off: fold shared tables into Discord chats
│   └── playtest.py      # end-to-end smoke test against the live LLM
└── deploy/gb10/dm-bot.service
```

## How it plays
- Talk in plain language; the DM narrates and streams the reply.
- **Engine decides, model narrates**: dice, DCs, HP, items and XP are all
  computed by deterministic code; the LLM only writes the story.
- **Consent checks**: the DM emits `[[check:DC|角色|屬性]]` → the player sees
  a check card (需骰 ≥ N) and replies 骰/roll → the server rolls d20+real
  modifier and the verdict is final.
- **Rules grounding, two layers**:
  1. `engine/compendium.py` — the SRD's core mechanics (checks, attack math,
     actions in combat, conditions, dying, concentration, resting), verified
     against the handbook and injected into EVERY prompt.
  2. `engine/rules.py` — hybrid retrieval (mxbai embeddings + title/body
     lexical signals + zh→en glossary) over ~2,900 SRD chunks for the long
     tail: exact spells, monsters, items, class features. Junk sources
     (spell name indexes, A-Z duplicates) are excluded at ingest, and chunk
     vectors embed title+body so "火球術" finds Fireball.
- `/new <setting>` starts a fresh adventure; `/pc` builds the party sheet
  (attributes rolled server-side or the exact standard array — no cheating);
  long sessions auto-summarize into an adventure log.
- `/combat start` → `/move` per player (1 player 1 move), `/voteskip`,
  admin `/takeover`; initiative d20+DEX; XP/levels via `[[xp:...]]` tags.

## Operate (GB10)
```bash
ssh -i keys/dnd_ed25519 comfyui@10.5.28.210
sudo systemctl restart dm-bot            # after engine/code edits
sudo journalctl -u dm-bot -f             # live logs
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/status.py
~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/playtest.py   # e2e smoke test
# rebuild rules index (~60s):
EMBED_PREFIX= ~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/ingest_srd.py \
  ~/srd ~/dnd-dm-bot/data/rules.db http://127.0.0.1:11434 mxbai-embed-large
```

Expected pace: a DM turn (~200 tokens) takes roughly 16-24s on the 27b —
slower than the old 12b but with much stronger rule discipline and Chinese.

## Fill in token
Edit `.env` (see `.env.example`):
- `DISCORD_TOKEN=` — from https://discord.com/developers/applications
  (create an app → Bot → Token; **enable MESSAGE CONTENT INTENT**)

SRD source: github.com/OldManUmby/DND.SRD.Wiki (reForged layout), CC-BY 4.0.
