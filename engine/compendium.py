"""Core-rules compendium: the slice of the SRD that applies to nearly every
turn, distilled once and ALWAYS in the system prompt.

Retrieval (engine.rules.RulesIndex) is great for the long tail — the exact
text of Fireball, a monster stat block, an obscure feat — but the core
mechanics (checks, attack math, actions, conditions, dying) are needed so
often that leaving them to retrieval is a coin flip. This block is the
handbook's spine, verified against the SRD source (06_Gameplay,
07_Spells/Spellcasting, 08_Gamemastering/Conditions).

Keep it under ~2,500 chars: it is injected into every single prompt.
"""

CORE_RULES = """CORE MECHANICS (D&D 5e SRD — always in force; ground every ruling in these):
- d20 tests: ability check / attack roll / saving throw = d20 + ability
  modifier (+ proficiency bonus if proficient). Advantage/disadvantage =
  roll 2d20 keep high/low; multiple sources don't stack, adv+dis cancels.
- Ability modifier by score: 8-9 -1, 10-11 +0, 12-13 +1, 14-15 +2, 16-17 +3,
  18-19 +4, 20 +5. Proficiency bonus: levels 1-4 +2, 5-8 +3, 9-12 +4,
  13-16 +5, 17-20 +6.
- Typical DCs: 5 very easy, 10 easy, 15 medium, 20 hard, 25 very hard,
  30 nearly impossible.
- Attacks: d20 + ability mod + prof vs target AC; natural 20 = critical hit
  (double the weapon's damage dice). Spell attack = d20 + spellcasting
  ability mod + prof. Spell save DC = 8 + prof + spellcasting ability mod;
  many damage spells allow a save for half.
- Contests (grapple, chase, deception vs insight): both sides roll, higher
  total wins; ties go to the defender/unchanged situation.
- Actions in combat (one per turn): Attack, Cast a Spell, Dash (move x2),
  Disengage (no opportunity attacks), Dodge (attacks against you have
  disadvantage, your DEX saves advantage), Help (ally's next check/attack
  has advantage), Hide (DEX Stealth vs passive Perception), Ready, Search,
  Use an Object. Bonus action only if a feature grants it; one reaction per
  round (opportunity attack: melee foe leaves your reach without Disengage).
- Movement splits freely before/after the action. Grapple/shove: part of the
  Attack action, Athletics vs target's Athletics or Acrobatics; shove knocks
  prone or pushes 5 ft.
- Cover: half +2 AC & DEX saves; three-quarters +5. Unseen attacker: its
  attack has advantage, attacks against it have disadvantage.
- Conditions (essence): Blinded (fails sight checks; attacks vs it have
  advantage, its attacks disadvantage) · Charmed (can't attack the charmer) ·
  Frightened (disadvantage while the source is in sight; can't willingly
  move closer) · Grappled (speed 0) · Incapacitated (no actions/reactions) ·
  Invisible (attacks vs it disadvantage, its attacks advantage) · Paralyzed
  (incapacitated; hits within 5 ft auto-crit) · Poisoned (disadvantage on
  attacks & checks) · Prone (its melee attacks disadvantage; melee attacks
  vs it advantage, ranged normal; costs half speed to stand) · Restrained ·
  Stunned (incapacitated, fails STR/DEX saves) · Unconscious (prone,
  incapacitated, drops what it holds, attacks vs it advantage and auto-crit
  within 5 ft, fails STR/DEX saves).
- Dying: at 0 HP a creature falls unconscious. At the start of each of its
  turns: death saving throw, plain d20 — 10+ success, 9- fail; 3 successes
  = stable, 3 failures = dead; nat 20 = conscious at 1 HP; nat 1 = two
  failures. Damage at 0 HP = one failure (crit = two); damage >= hp_max =
  instant death. Any healing ends the countdown.
- Concentration: one concentration spell at a time; taking damage forces a
  CON save vs DC 10 or half the damage taken (whichever is higher) or the
  spell ends; incapacitated/another concentration spell also ends it.
- Resting: short rest >= 1 hour, spend Hit Dice (roll + CON mod each) to
  heal; long rest >= 8 hours, restores all HP and half of spent Hit Dice,
  once per 24 hours. Passive Perception = 10 + WIS mod (+ prof).
- Falling: 1d6 bludgeoning per 10 ft fallen (max 20d6); lands prone."""
