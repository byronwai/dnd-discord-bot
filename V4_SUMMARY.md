# V4 Summary — Engine-Driven Architecture (Current)

> Version: v4 (active) · Updated: 2026-10-10
> Architecture: engine does 70% (rules, state, combat, dice), LLM does 30%
> (reading intent, writing prose). Game fully playable without any LLM.
> Full v1→v4 evolution: see `EVOLUTION.md`

---

## 1. Architecture

```
Player text ──→ [Digestor 12b] ──→ Intent JSON
         (or /combat dropdowns ──────→ Intent directly, no LLM)
                                            │
                                            ▼
                                   [GAME ENGINE]     ←── no LLM needed
                                   validate + resolve
                                   dice · HP · items · combat · scenes
                                            │
                                   [Ledger] append-only facts
                                            │
                         ┌─────────────────┼─────────────────┐
                         ▼                 ▼                 ▼
                   [Templates]       [Narrator 27b]    [Debug Portal]
                   skeleton prose     polish + flavour   #dnd-health
                   (no LLM)          (guards applied)   log stream
```

## 2. Commands (14)

| Player (10) | Admin (4) |
|---|---|
| `/pc name occupation` — create character | `/explore-admin text char` |
| `/explore text` — freeform action (digestor→engine) | `/combat-admin` |
| `/confirm` — confirm pending action | `/roll-admin expr char` |
| `/combat action target move` — RPG menu (no LLM) | `/give-admin char item qty` |
| `/inventory [char]` — items + slots + moves | |
| `/roll [expr]` — dice (settles pending checks) | |
| `/give item [to]` — transfer to party member | |
| `/status` — party + scene | |
| `/continue` — unstuck + scene context | |
| `/help` | |

**Channel keywords** (instant, no LLM): `status` `inv` `moves` `scene`
**Plain text** = table talk (silently ignored)

## 3. Input Paths

| Path | Flow | LLM? |
|---|---|---|
| `/combat` (dropdowns) | Structured Intent directly → engine | None for intent |
| `/explore` (freeform) | Scrub → digestor (12b) → Intent → engine | 12b for intent |
| Fallback (digestor down) | Cantonese-aware keyword parser → Intent | None |
| Channel keywords | Direct engine query | None |

**Digestor** handles: Cantonese (劈/揼/篤/執/攞), English code-mixing
("i picked a sword"), paraphrase to written Chinese first, action
meanings (not just words). See `v4/digestor.py` prompt for the full
action definition table.

## 4. Dice Mechanics

| Path | Flow | Agency |
|---|---|---|
| `/combat` | Player picks action → engine rolls immediately | Consent by selection |
| `/explore` (check) | Engine shows check card → player `/roll d20` → engine settles | Player rolls |
| `/explore` (take) | Engine moves ground item to inventory (no roll needed) | — |
| `/explore` (talk NPC) | Friendly: no check. Hostile: CHA check card | Player rolls |
| `/explore` (claim) | "我升到99級" → narrator responds in-character, engine denies | — |

## 5. Core Modules (v4/)

| Module | Responsibility |
|---|---|
| `intent.py` | Intent dataclass + deterministic parser (Cantonese-aware fallback) |
| `world.py` | Scene graph, NPC (with `knows`), encounters |
| `turn.py` | Game state: party, combat, inventory, spell slots |
| `rules_core.py` | validate + resolve — the ONLY write path |
| `ledger.py` | Append-only event log (replayable) |
| `digestor.py` | 12b: freeform → Intent JSON (paraphrase, action meanings) |
| `narrator.py` | 27b: skeletons + facts → prose (streaming, guards applied) |
| `templates.py` | Prose skeletons + scene context + suggested actions |
| `guards.py` | v3 defenses: placeholders, scrubbing, repetition, s2t |
| `service.py` | Orchestration, per-channel games, persistence |

## 6. Actions (15)

| Action | Trigger | Engine behaviour |
|---|---|---|
| `attack` | /combat, /explore | d20+bonus vs AC → damage → HP → death |
| `take` | /explore (執/撿/pick) | Move ground/hidden item to inventory |
| `move` | /explore (去/go) | Scene change + encounter check |
| `use` | /combat, /explore | Apply item (potions heal, etc.) |
| `cast` | /combat, /explore | Consume spell slot, narrate effect |
| `talk` | /explore | NPC check (CHA if hostile), narrator voices NPC |
| `search` | /explore (搜索) | WIS check → reveal hidden items |
| `creative` | /explore (自創) | Ability check for improvised method |
| `claim` | /explore (我升到99級) | Narrator responds, engine denies |
| `meta` | /explore (目標/感受) | Narrator responds, scene context shown |
| `chat` | plain text | Silently ignored (table talk) |
| `rest` | /explore (休息) | Short: hit dice. Long: full restore |
| `give` | /give | Transfer item between party members |
| `check` | /explore (檢定) | Ability check with player roll |
| `pass` | /explore (等待) | Skip turn (combat) |

## 7. Guards (v3 Lessons Applied)

| Guard | Prevents |
|---|---|
| Placeholder names `[PC1]` | Narrator transliterates characters |
| Fake-dice scrubbing | Narrator writes dice results |
| Repetition guard | Narrator spirals into identical outputs |
| Language check + OpenCC s2t | Non-Chinese / Simplified output |
| Input sanitization | `[PCn]` / `SYSTEM VERDICT` injection |
| Ownership (user_id) | Player controls another's character |
| NPC `knows` lists | Narrator invents quest content |
| Scrub before digestor | Injection reaches the LLM |

## 8. Per-Channel Games

Each Discord channel gets its own independent game:
- State: `v4_state_<channel_id>.json`
- Env: `V4_CHANNEL_IDS=<id1>,<id2>,...`
- Everything isolated: characters, world, inventory, ledger

## 9. Turn Output (v3-style streaming UX)

```
t=0s   🎭 **PlayerName** action text          ← echo (immediate)
t=0s   📖 DM 正在寫作…                         ← placeholder
t=0s   🎲 engine result + 📍 scene + 👉 你可以  ← replaces placeholder
t=2s   📖 海風鹹濕地吹拂著… ▍                    ← streaming (2s edits)
t=10s  📖 full narration                       ← complete
```

## 10. What's Parked

| Feature | Status |
|---|---|
| Director (new objects) | Design ready — typed templates + engine gate |
| SRD retrieval | rules.db exists, not wired to v4 |
| Difficulty scaling | Not in v4 world model |
| Death save counters | Not implemented |
| Conditions | Not implemented |
