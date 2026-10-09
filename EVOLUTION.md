# D&D DM Bot — Complete Evolution: v1 → v4

> A detailed workflow document explaining how the logic, infrastructure,
> setup, and architecture changed across four major versions, including
> every mechanism that was added, removed, or transformed.
>
> Timeline: 2026-10-07 → 2026-10-09 (two days of marathon development)

---

## Version Timeline

```
v1          v2              v3                  v4
Pi + 3B     GB10 + 27b      Hardened v3         Engine-driven
2-platform  1-platform      27 commands         13 commands
LLM-driven  Tag protocol    Anti-cheat layer    Engine core
            v1              v2 + hardening      No LLM required
```

---

## 1. Infrastructure Evolution

| | v1 | v2 | v3 | v4 |
|---|---|---|---|---|
| **Hardware** | Raspberry Pi 5 (8GB) | GB10 DGX Spark (119GB) | GB10 | GB10 |
| **Model** | Qwen2.5-3B (Q4) | gemma3:12b-it-qat | gemma3:27b-it-qat | 27b (narrator) + 12b (digestor) |
| **Speed** | ~4 tok/s | 7–11s/turn | 16–24s/turn | Engine: instant; Narrator: 8–15s |
| **Context** | 4k tokens | 16k tokens | 16k → 24k | 24k (expandable) |
| **Platforms** | Discord + Telegram | Discord only | Discord only | Discord only |
| **Processes** | 1 bot | 1 bot + 1 health | 1 bot + 1 health | 1 bot + 1 health |
| **Storage** | SQLite | SQLite | SQLite + events + npcs + lore | v4_state.json (JSON) |

### Why the hardware changed
The Pi couldn't run models larger than 3B-Q4 at usable speed (4 tok/s
LPDDR4X bandwidth limit). Every core design decision was forced by this
constraint. Moving to GB10 (128GB unified memory) allowed 12b then 27b,
but the lessons learned on 3B shaped everything.

### Why Telegram was dropped (v2)
Two adapters + cross-platform shared tables (bindings, `/table_new`,
six-digit join codes) doubled the state machine complexity for a feature
that converged to zero real-world use. The live table BTWQS6 was folded
into its Discord channel by `tools/migrate_discord_only.py`.

---

## 2. Architecture Evolution

### v1: LLM-Driven DM (Pi + 3B)

```
Discord ─┐
         ├─→ Adapter → LLM (generates story + decides everything)
Telegram ┘         │
                   ├─ SQLite (characters, items, plot)
                   └─ Dice tags [[d20+3]] parsed from LLM output
```

**The problem**: The 3B model was the DM. It decided when checks happened,
what the DC was, whether you succeeded. It invented dice results,
renamed characters, drifted between languages, and hallucinated rules.
Every "fix" was a prompt patch that the model would eventually route around.

**Key mechanisms invented (out of desperation)**:
- `[[dice]]` tags: model "proposes" a roll, server executes it
- Canonical tail: party sheet at the END of the prompt (small models
  attend most to recent tokens)
- Sticky language: ambiguous messages inherit the last clear language
- Alias table: "Elyse" → 依思 (character name transliteration defense)
- Actor-fallback: unknown tag name → attribute to the current speaker

### v2: Tag Protocol + Rules Grounding (GB10 + 12b)

```
Discord ─→ Adapter → LLM (narrates + emits [[tags]])
                        │
                        ▼
                   Tag Parser → SQLite
                   ([[check]] [[hp]] [[item]] [[scene]] [[xp]])
                        │
                        ▼
                   SRD Retrieval (RAG, 2918 chunks)
```

**The shift**: The LLM still narrated the story, but now it had to
**propose** state changes via a tag protocol. The engine parsed and
executed them. The LLM could still cheat (by not emitting tags, or by
writing fake dice), but the engine now had a contract to enforce.

**New mechanisms**:
- Consent checks: `[[check:DC|char|ABILITY]]` → card → player "骰" → engine rolls
- State tags: `[[hp:名:-4]]` `[[item:名:+藥水]]` `[[scene:…] `[[objective:…]]`
- Two-layer rules grounding: always-on compendium + SRD RAG
- Name placeholders: `[PC1]` `[PC2]` — model never sees real names
- OpenCC s2t: Simplified → Traditional Chinese conversion
- Difficulty tiers: mechanically adjust enemy HP/AC at registration

### v3: Hardened Anti-Cheat (GB10 + 27b)

```
Discord ─→ Adapter → LLM (narrates + emits tags)
                        │
                        ▼ [post-processing pipeline]
                   ┌─ Think-scrub
                   ├─ Fake-dice removal (6+ regex families)
                   ├─ Fake-card conversion (hand-written → real pending)
                   ├─ Placeholder restore ([PC1] → 依思)
                   ├─ Language enforcement (zh check + translate + s2t)
                   ├─ Dice resolution ([[expr]] → real rolls)
                   ├─ State tag application → SQLite
                   ├─ Audit pass (missed tags → LLM extraction)
                   └─ Repetition guard (near-dup collapse)
                        │
                        ▼
                   Context Hygiene Layer
                   (strip engine echoes from history so model
                    can't learn and imitate them)
```

**The philosophy matured**: "Engine decides, model narrates" was no longer
just about dice — it became about **curating everything the model sees**.
If the model sees engine-rendered status lines, it imitates them. If it
sees its own repeated narrations, it spirals. The context hygiene layer
strips these before they enter the prompt.

**New mechanisms** (the "fourth wave" — one night of live-hardening):
- Pending cards never silently dropped (v3 lesson: card disappears mid-flight)
- `/roll` waits for in-flight DM turns (streaming race condition)
- Fake-check-card converter (bold variants: `vs **DC 20** → **15** (失敗)`)
- Self-attack guard (converter doesn't mint phantom cards from status text)
- NPC knowledge constraints (narrator can only reveal `knows` list facts)
- Repetition death-spiral guard (SequenceMatcher, 80% threshold)
- Latin-leak scrubber (stray English words in zh prose → targeted fix-up)
- Scene-blocked event compression (one line per scene stretch)
- Beat-list summarizer (8 bullets, ≤25 chars each, replaces 250-word prose)
- Combat rotation: engine-enforced, never parks on dead PCs, auto-resolves
  NPC turns when player acts (no "wait or pass")
- Spell-slot economy: leveled moves consume slots, gated by class table

### v4: Engine-Driven (current)

```
Player text ──→ [Digestor 12b] ──→ Intent JSON
                                            │
                                            ▼
                                   [GAME ENGINE]     ←── no LLM
                                   validate + resolve
                                   dice · HP · items · combat · scenes
                                            │
                                   [Ledger] append-only facts
                                            │
                         ┌─────────────────┼─────────────────┐
                         ▼                 ▼                 ▼
                   [Templates]       [Narrator 27b]    [Debug Portal]
                   skeleton prose     polish + flavor   log stream
                   (no LLM)          (guards applied)   #dnd-health
```

**The inversion**: The engine IS the game. The LLM is a peripheral that
reads player intent and writes prose. Remove both LLMs and the game is
still fully playable (combat, items, scenes, checks — all deterministic).

**What fundamentally changed**:

| Aspect | v3 | v4 |
|---|---|---|
| Turn engine | LLM generates → engine post-processes | Engine resolves → LLM narrates |
| Model's write path | Tag protocol (proposes state changes) | **None** (pure output, no state access) |
| Cheat surface | Tags that could be malformed/faked | **Zero** (no write path to attack) |
| No-LLM mode | Impossible | Full gameplay via CLI |
| Combat rotation | In prompt directives | In engine state machine |
| Dice | Parsed from LLM output tags | Engine-rolled, rendered by templates |
| World state | LLM's "understanding" + tag fragments | Structured world model (scenes, NPCs) |
| Narration | LLM generates from scratch | Engine provides skeleton, LLM polishes |

---

## 3. Mechanism Evolution (Detailed)

### 3.1 Dice Rolling

| Version | Mechanism | Trust Level |
|---|---|---|
| v1 | LLM writes `[[d20+3]]`, server substitutes result | Model proposes, server executes |
| v2 | Same tag + consent checks (`[[check:DC|char|AB]]`) | Player must consent before roll |
| v3 | Same + fake-dice scrubbing + forced_d20 (player's actual die) | Post-processing catches cheats |
| v4 | Engine rolls everything, templates render results | **Model never touches dice** |

### 3.2 State Management

| Version | Where state lives | How it changes |
|---|---|---|
| v1 | LLM's context + SQLite (best effort) | LLM narrates → server tries to parse |
| v2 | SQLite (party, items, scenes) | LLM emits `[[hp]]` `[[item]]` tags → parser executes |
| v3 | Same + events table + NPC/lore notebook + pending_check | Tags + audit pass + deferred retaliation |
| v4 | `v4_state.json` (world model + party + inventory + combat + ledger) | **Only** `rules_core.resolve()` writes |

### 3.3 Combat System

| Version | Initiative | Turn order | Attack resolution |
|---|---|---|---|
| v1 | LLM narrates it | LLM decides | LLM narrates outcome |
| v2 | Engine rolls d20+DEX | Prompt directive | `[[attack:char|target|+N]]` → consent → engine |
| v3 | Same + auto-skip dead PCs | Engine-enforced + interaction-aware | `/attack` = full engine chain (d20→crit→damage→HP→death) |
| v4 | Engine state machine | Engine auto-resolves NPC turns when player acts | Same chain + spell-slot economy + AoE splash |

### 3.4 Character Identity

| Version | Name handling | Ownership |
|---|---|---|
| v1 | Real names in prompt; model transliterates | None |
| v2 | Alias table (Elyse→依思) + actor-fallback | user_id check on commands |
| v3 | Placeholder `[PC1]`/`[PC2]` mapping + restore + scrub player PCn guesses | user_id + display-name collision + char_bonds audit |
| v4 | Same placeholder system (guards.py) + ownership default (caller's char) | user_id check in service.handle() |

### 3.5 Rules Grounding

| Version | Approach | Coverage |
|---|---|---|
| v1 | None (LLM's training data) | Whatever the model remembers |
| v2 | SRD RAG (2918 chunks, zh→en glossary, hybrid scoring) | Long-tail lookup per turn |
| v3 | RAG + always-on compendium (~2.4k chars, every prompt) | Core mechanics always in context |
| v4 | Not yet implemented (parked for Director layer) | — |

### 3.6 Language Policy

| Version | Layers | Fail Rate |
|---|---|---|
| v1 | zh system prompt + few-shot (failed) | ~30% drift |
| v2 | + OpenCC s2t + translate-rescue | ~10% |
| v3 | + latin-leak scrubber + per-turn directive | ~2% |
| v4 | + language check (reject non-Chinese narration) + skeleton fallback | ~0% (rejected → skeleton) |

### 3.7 Anti-Cheat Defense

| Version | Approach | What it catches |
|---|---|---|
| v1 | Prompt bans ("never write dice results") | Almost nothing |
| v2 | Regex families (fake-dice, fake-cards) + tag protocol | Specific patterns |
| v3 | + Structural defenses (placeholders, context hygiene, no-write-path) + variant-tolerant regex + conversion (fake → real pending) | All known variants |
| v4 | **No write path exists** — model output is decoration only | Everything (nothing to catch) |

### 3.8 Context Management

| Version | Strategy | Prompt Size |
|---|---|---|
| v1 | Full history (40 messages) | ~8k tokens at max |
| v2 | Summary (250 words) + raw window (10) | ~4k tokens |
| v3 | Summary (beat-list, 8 bullets) + raw window (12) + scene-blocked events (40 rows) | ~6k tokens (24k ctx) |
| v4 | Ledger (append-only, complete) + templates (no history needed for narrator) | Narrator: ~2k tokens |

---

## 4. Setup Evolution

### v1 (Raspberry Pi)
```bash
# One process, one model
python bot.py  # Discord + Telegram adapters
# LLM: llama.cpp on port 8080 (Qwen2.5-3B-Q4)
# Embeddings: llama.cpp on port 8081 (bge-small)
```

### v2/v3 (GB10)
```bash
# Two processes
sudo systemctl start dm-bot       # main bot (Ollama :11434)
sudo systemctl start dnd-health   # health board (read-only DB)
# LLM: Ollama (gemma3:27b-it-qat, 24k ctx)
# Embeddings: Ollama (mxbai-embed-large)
```

### v4 (GB10, current)
```bash
# Same two processes
sudo systemctl start dm-bot dnd-health
# V4_CHANNEL_ID env var routes a channel to the v4 engine
# Everything else (v3) is in legacy/
```

---

## 5. Key Lessons (Cross-Version)

### Lessons that carried forward from v1
1. **Engine decides, model narrates** — the single most important rule
2. **Rules at the END of the prompt** — small models attend to recency
3. **Structural > enumerative defense** — rename a tag, don't add a regex
4. **Player complaints are architecture feedback** — best fixes came from
   real gameplay friction

### Lessons learned in v2
5. **Thinking models can't do chat pacing** — content empty, reasoning
   explodes; test `content` streams before committing to a model
6. **Title+body embedding** — spell/monster names live in headings; pure
   body vectors miss them (the single most important retrieval fix)
7. **Data before scoring** — fix the index (remove junk, classify chunks)
   before tuning similarity scores

### Lessons learned in v3
8. **Rendering is teaching** — anything the model sees, it imitates; strip
   engine echoes from history (context hygiene)
9. **Un-narrated state changes are UX crimes** — deferred retaliation:
   damage doesn't exist until it has a story
10. **Falsy-zero discipline** — `hp or 1` breaks everything; use explicit
    `is not None` + inequality checks
11. **InteractionResponded** — _announce consumes the response; check
    `is_done()` before defer/respond (bit us three times)
12. **Silent deploy failures** — `&&` chains break, scp to nonexistent
    dirs silently fails; always md5-verify after deploy

### Lessons learned in v4
13. **The engine should own everything** — every mechanism that lived in
    the LLM (combat, checks, world state) became simpler, faster, and
    bug-free when moved to deterministic code
14. **Narration is decoration** — templates.py can produce readable prose
    from structured facts; the LLM adds flavor, not substance
15. **NPC knowledge is data** — `knows` lists prevent the narrator from
    inventing quest content; the world model controls what's revealable
16. **Confirmation is friction** — only ask when genuinely uncertain;
    structured inputs (/combat dropdowns) should skip the digestor entirely
17. **The v3 lessons still apply** — placeholders, fake-dice scrubbing,
    repetition guards: all ported because the narrator is still an LLM
    that will drift, invent, and repeat if unguarded

---

## 6. What v4 Still Doesn't Have (Parked)

| Feature | Status | Design |
|---|---|---|
| Director LLM | Not implemented | Proposes new scenes/NPCs/encounters; engine gates entry |
| SRD retrieval | Not wired to v4 | rules.db exists; needs integration into check DCs |
| Difficulty scaling | Not in v4 world model | Enemy HP/AC multipliers based on party level |
| Context compression | Not needed yet | Ledger grows; scene-blocked injection when it matters |
| Death save counters | Not implemented | 3 successes/failures tracking at HP 0 |
| Conditions ([[cond:]]) | Not implemented | Blinded/Frightened/Prone mechanical effects |
| Grid/distance | Not implemented | Tactical positioning |
| CR budgeting | Not implemented | Encounter difficulty scaling |

---

## 7. File Location Cross-Reference

| Version | Code Location | Status |
|---|---|---|
| v1 | Not preserved separately (evolved into v2) | Lost |
| v2 | `legacy/` (dm.py, state.py, commands.py) | Archived |
| v3 | `legacy/` (same files + design docs) | Archived |
| v4 | `v4/` + `engine/` + `adapters/` + `health/` | **Active** |
| Design docs | `DESIGN.md` (v3), `V4_DESIGN.md`, `V3_SUMMARY.md` | Reference |
| This document | `EVOLUTION.md` | Living |

*Generated 2026-10-09 · 41 git commits · 4,094 lines active code*
