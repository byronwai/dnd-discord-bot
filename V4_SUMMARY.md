# V4 Summary — Engine-Driven Architecture (Current)

> Version: v4 (active) · Updated: 2026-10-09
> Architecture: engine does 70% (rules, state, combat, dice), LLM does 30%
> (reading intent, writing prose). Game fully playable without any LLM.
> Full v1→v4 evolution: see `EVOLUTION.md`

---

## 1. Architecture

```
Player text ──→ [Digestor 12b] ──→ Intent JSON
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
| `/explore text` — freeform action | `/combat-admin` |
| `/confirm` — confirm pending action | `/roll-admin expr char` |
| `/combat action target move` — RPG menu | `/give-admin char item qty` |
| `/inventory [char]` — items + slots + moves | |
| `/roll [expr]` — dice (settles pending checks) | |
| `/give item [to]` — transfer to party member | |
| `/status` — party + scene | |
| `/continue` — unstuck + scene context | |
| `/help` | |

**Channel keywords** (instant, no LLM): `status` `inv` `moves` `scene`
**Plain text** = table talk (silently ignored, no reaction, no reply)

## 3. Dice Mechanics

| Path | Flow | Agency |
|---|---|---|
| `/combat` (structured) | Player picks action → engine rolls immediately | Consent given by selection |
| `/explore` (freeform) | Engine shows check card → player `/roll d20` → engine settles with that die | Player rolls their own dice |
| `/roll d20` (no pending) | Utility roll, no game effect | — |
| NPC talk (hostile) | CHA check card → player rolls | Player rolls |
| NPC talk (friendly) | No check needed, NPC engages | — |
| Attack | Engine rolls d20+bonus vs AC, damage, HP, death | Structured = auto |
| Creative action | Engine shows ability check card → player rolls | Player rolls |

## 4. Core Modules (v4/)

| Module | Lines | Responsibility |
|---|---|---|
| `intent.py` | 80 | Intent dataclass + deterministic keyword parser |
| `world.py` | 80 | Scene graph, NPC (with `knows`), encounters |
| `turn.py` | 149 | Game state: party, combat, inventory, spell slots |
| `rules_core.py` | ~470 | validate + resolve — the ONLY write path |
| `ledger.py` | 55 | Append-only event log (replayable, serializable) |
| `digestor.py` | ~90 | 12b: freeform text → Intent JSON |
| `narrator.py` | ~80 | 27b: skeletons + facts → prose (120-200 字) |
| `templates.py` | ~180 | Deterministic prose + scene context + suggestions |
| `guards.py` | 132 | v3 defenses: placeholders, scrubbing, repetition |
| `service.py` | ~250 | Orchestration, persistence, per-channel games |
| `cli.py` | 230 | No-LLM REPL + seeded selftest |

## 5. Shared Math (engine/)

| Module | Lines | Responsibility |
|---|---|---|
| `charlib.py` | 158 | AC, PB, spell slots, save/skill proficiencies |
| `dice.py` | 124 | Dice expressions (4d6kh3, adv/dis, ±N) |
| `checks.py` | 148 | Total modifier calculator |
| `moves.py` | ~100 | Class move tables + `compute_attack_moves` |

## 6. Guards (v3 Lessons Applied)

| Guard | What it prevents |
|---|---|
| Placeholder names | Narrator transliterates/renames characters |
| Fake-dice scrubbing | Narrator writes dice results |
| Repetition guard | Narrator spirals into identical outputs |
| Language check | Non-Chinese narration reaches players |
| Input sanitization | Player injects [PCn] or SYSTEM VERDICT |
| Ownership check | Player controls another player's character |
| NPC knows lists | Narrator invents quest content via NPC dialogue |

## 7. Per-Channel Games

Each Discord channel gets its own independent game:
- State file: `v4_state_<channel_id>.json`
- Env: `V4_CHANNEL_IDS=<id1>,<id2>,...` (comma-separated)
- Characters, world, inventory, ledger — all isolated per table

## 8. Turn Output Format

Every turn shows:
1. **Public echo**: `🎭 **PlayerName** action text`
2. **Engine output** (instant): check math, damage, HP, combat status
3. **Turn context**: 📍 scene + ❤️ party HP + 👉 suggested actions
4. **Narration** (8-15s later): 📖 prose with hook at the end

## 9. What's Not Yet Implemented

| Feature | Status |
|---|---|
| Director LLM | Not implemented — proposes new scenes/NPCs |
| SRD retrieval | rules.db exists, not wired to v4 check DCs |
| Difficulty scaling | Not in v4 world model |
| Death save counters | Not implemented |
| Conditions ([[cond:]]) | Not implemented |
| Grid/distance | Not implemented |
| Context compression | Ledger grows unbounded (fine for now) |

## 10. Selftest

```bash
python v4/cli.py selftest          # deterministic (seed=7)
V4_SEED=42 python v4/cli.py selftest  # different seed
```

Covers: route rejection, encounter spring, check flow (pending → roll →
resolve), slot economy, potion healing, target denial, combat rotation,
long rest, ledger persistence.
