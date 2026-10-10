"""v4 CLI: play the game with ZERO LLM — the P1 proof of engine-driven play.

    python v4/cli.py            # interactive REPL
    python v4/cli.py selftest   # scripted regression scenario (assertions)
"""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from v4.intent import parse_command, Intent
from v4.ledger import Ledger
from v4.rules_core import resolve
from v4.turn import Game
from v4.world import World, Scene, Enemy


def build_demo_game(seed: int = None) -> Game:
    """seed pins the game's own RNG (initiative, dice, enemy targeting) —
    selftests pass a seed for byte-identical replays."""
    g = _build_demo_world_party()
    if seed is not None:
        g._random.seed(seed)
    return g


def _build_demo_world_party() -> Game:
    world = World()
    world.add_scene(Scene(
        "dock", "霧錨鎮碼頭",
        "漁船靜靜泊在霧中，空氣裡有海藻與焦油的味道。北邊的小路通往酒館。",
        exits={}, npcs=[{"name": "老船長", "desc": "獨眼，正在補網",
                         "disposition": "friendly"}]))
    world.add_scene(Scene(
        "tavern", "破舊酒館",
        "哥布林佔據的酒館，桌椅翻倒，角落有可疑的酒窖門。",
        exits={}, search_dc=11,
        hidden_items=[("生鏽的鐵牌", 1)]))
    world.scenes["dock"].exits["tavern"] = "破舊酒館"
    world.scenes["tavern"].exits["dock"] = "碼頭"
    world.current = "dock"

    party = {
        "依思": {"occupation": "Warlock",
                 "stats": {"STR": 11, "DEX": 14, "CON": 16, "INT": 11,
                           "WIS": 14, "CHA": 17},
                 "hp_now": 11, "hp_max": 11, "level": 2},
        "大力蕉": {"occupation": "Druid",
                   "stats": {"STR": 10, "DEX": 13, "CON": 16, "INT": 12,
                             "WIS": 17, "CHA": 9},
                   "hp_now": 11, "hp_max": 11, "level": 2},
    }
    g = Game(party, world,
             encounters={"tavern": [Enemy.make("哥布林①", 7, 15, 3, "1d6"),
                                    Enemy.make("哥布林②", 5, 13, 2, "1d4")]})
    g.give_item("依思", "治療藥水", 2)
    g.give_item("大力蕉", "治療藥水", 2)
    return g


META = {
    "status": lambda g: [f"{n} {e['hp_now']}/{e['hp_max']}HP "
                         f"{e.get('occupation')} Lv{e.get('level',1)}"
                         for n, e in g.party.items()],
    "scene": lambda g: g.world.describe().splitlines(),
    "inv": lambda g: [f"{n}: " + (", ".join(f"{i}×{q}" for i, q in inv)
                                  or "（空）")
                      for n, inv in g.inventory.items()],
    "moves": lambda g: [f"{n}: " + ", ".join(
        m["name"] for m in
        __import__("engine.moves", fromlist=["x"]).compute_attack_moves(
            e.get("occupation", ""), int(e.get("level", 1)),
            [("item", i, q) for i, q in g.inventory[n]],
            g.max_slot(n)))
        for n, e in g.party.items()],
}


def handle(g: Game, text: str) -> list[str]:
    t = text.strip()
    if not t:
        return []
    if t in META:
        return META[t](g)
    it = parse_command(t, party_names=list(g.party))
    if it is None:
        return [f"🤖 看不懂（degraded 模式只认指令语法）。试试："
                f"status / scene / inv / moves / "
                f"「{list(g.party)[0]} 攻擊 X」「去 破舊酒館」「使用 治療藥水」"]
    r = resolve(g, it)
    return r.lines


def repl() -> None:
    g = build_demo_game(seed=int(os.environ.get("V4_SEED", "7")))
    random.seed()  # fair dice in interactive mode
    print("=== v4 engine · no-LLM mode ===  (status/scene/inv/moves, "
          "或「角色 動詞 賓語」；Ctrl-D 離開)")
    for line in g.world.describe().splitlines():
        print(line)
    while True:
        try:
            text = input("v4> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        for line in handle(g, text):
            print(line)


def selftest() -> None:

    g = build_demo_game()
    L = []

    def run(text):
        L.extend(handle(g, text))

    # invalid route is rejected by the ENGINE (not the DM's judgement)
    run("依思 go 月球")
    assert any("沒有通往" in x for x in L), L
    L.clear()

    # ---- the 18-skill system -------------------------------------------
    from v4.rules_core import resolve_pending_check as _rpc

    # unknown skill is denied deterministically, with the valid list
    run("依思 技能 飛天")
    assert any("未知的技能" in x for x in L), L
    L.clear()

    # stealth: pending check -> PLAYER's own die -> effect applied
    # (nat 20 always succeeds -> the unseen-attacker flag is set)
    run("依思 技能 潛行")
    ok, line = _rpc(g, 20)
    assert ok and getattr(g, "_stealth", {}).get("依思"), L
    assert "潛行" in line, line
    L.clear()

    # insight: reveals the NPC's true disposition as an engine fact
    run("大力蕉 技能 洞察 老船長")
    ok, line = _rpc(g, 20)
    assert ok and any(e.kind == "insight" and e.data.get("npc") == "老船長"
                      for e in g.ledger.entries), L
    L.clear()
    # --------------------------------------------------------------------

    # potion heals via SRD math and is consumed (out of combat: free order)
    g.party["依思"]["hp_now"] = 5  # deterministic setup for the heal assert
    hp0 = g.party["依思"]["hp_now"]
    run("依思 使用 治療藥水")
    assert g.party["依思"]["hp_now"] > hp0
    assert sum(q for n, q in g.inventory["依思"] if n == "治療藥水") == 1
    L.clear()

    # scene move is engine-owned; encounter springs automatically
    run("依思 go 破舊酒館")
    assert g.world.current == "tavern" and g.combat.active, L
    assert any("戰鬥開始" in x for x in L)
    L.clear()

    def await_turn(name):
        for _ in range(12):
            if not g.combat.active:
                return False
            c = g.combat.current()
            if c and not c.get("npc") and c["name"] == name:
                return True
            run("pass")
            L.clear()
        return False

    # engine-enforced rotation: acting out of turn is denied
    first, second = list(g.party)
    if await_turn(first):
        run(f"{second} 攻擊 哥布林①")
        assert any("輪到" in x for x in L), L
        L.clear()

    # search on your turn: pending check → player rolls → resolved
    (await_turn("大力蕉") and run("大力蕉 搜索")) or run("依思 搜索")
    # now the check is pending (player must /roll d20)
    pend = getattr(g, "_pending_check", None)
    if pend:
        from v4.rules_core import resolve_pending_check
        ok, line = resolve_pending_check(g, g.d20())
        if ok and g.world.here.hidden_items:
            for name, qty in g.world.here.hidden_items:
                g.give_item(pend["actor"], name, qty)
            g.world.here.hidden_items = []
    assert any(e.kind == "check" for e in g.ledger.entries)
    L.clear()

    # attack with a leveled move pays a slot (engine-enforced economy)
    if await_turn("依思"):
        before = g.party["依思"]["slots"]["1"]
        run("依思 攻擊 哥布林① 地獄斥喝")
        after = g.party["依思"]["slots"]["1"]
        assert after == before - 1, (before, after, L)
        assert any("🎲 依思 攻擊" in x for x in L)
    L.clear()

    # attacking a nonexistent target is denied deterministically
    run("大力蕉 攻擊 巨龍")
    assert any(x.startswith(("🚫", "⏳", "❓")) for x in L), L
    L.clear()

    # enemies strike back automatically; the rotation never parks on the
    # dead (engine-skipped); heroes use their cantrips and drink when hurt
    best_move = {"依思": "魔能爆", "大力蕉": "詛咒木杖"}
    for _ in range(40):
        if not g.combat.active:
            break
        cur = g.combat.current()
        if cur and not cur.get("npc"):
            hero = cur["name"]
            hurt = g.party[hero]["hp_now"] <= 6
            has_potion = any(n == "治療藥水"
                             for n, q in g.inventory[hero])
            if hurt and has_potion:
                run(f"{hero} 使用 治療藥水")
            else:
                mv = best_move.get(hero, "")
                foes = [f.name for f in g.enemies.values() if not f.dead]
                tgt = sorted(foes)[0] if foes else "哥布林①"
                run(f"{hero} 攻擊 {tgt}" + (f" {mv}" if mv else ""))
        else:
            run("pass")
        L.clear()
    assert not g.combat.active or g.alive(g.combat.current()["name"])

    # long rest restores every LIVING character; the downed STAY down
    living = [n for n in g.party if g.alive(n)]
    assert living, "engine recorded the TPK but demo assumes survivors"
    other = [n for n in g.party if n != living[0]][0]
    g.party[other]["hp_now"] = 0  # deterministic downed scenario
    run(f"{living[0]} 休息 長休")
    assert all(e["hp_now"] == e["hp_max"]
               for n, e in g.party.items() if g.alive(n))
    assert g.party[other]["hp_now"] == 0
    from engine.charlib import slots_for
    for n, e in g.party.items():
        assert e["slots"] == {str(k): v for k, v in
                              slots_for(e["occupation"], e["level"]).items()}
    assert g.party["依思"]["slots"]["1"] == 2
    L.clear()

    # ledger is complete, replayable and serializable
    kinds = {e.kind for e in g.ledger.entries}
    assert {"scene", "combat", "check", "attack", "heal",
            "deny"} <= kinds, kinds
    g.ledger.save("/tmp/v4-ledger-test.json")
    assert Ledger.load("/tmp/v4-ledger-test.json").turn == g.ledger.turn

    print("V4 P1 SELFTEST: PASS — engine-driven game fully playable, "
          "zero LLM involved")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
    else:
        repl()
