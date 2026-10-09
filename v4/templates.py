"""v4 narration bank: deterministic zh prose skeletons per ledger kind.

The engine PREDICTS most of the narration (scene, items, actions,
outcomes) from its own data. These skeleton sentences are the base prose
in degraded mode and the mandatory scaffold handed to the narrator LLM,
which may only add flavour — never numbers, outcomes or actions.

Variant choice is stable (no RNG): the same turn always renders the same
sentence, so replays are byte-identical.
"""


def _stable(key: str) -> int:
    return sum(ord(c) for c in str(key))


def _pick(variants: list, key: str) -> str:
    return variants[_stable(key) % len(variants)]


def render_hint(entry, game=None) -> str:
    """One prose sentence for a ledger entry (empty for silent kinds)."""
    d = entry.data or {}
    actor, target = entry.actor, d.get("target", "")
    kind = entry.kind

    if kind == "attack":
        if d.get("crit"):
            return _pick([f"{actor} 使出全力一擊，正中 {target} 的要害——漂亮的一擊！",
                          f"千鈞一髮之際，{actor} 的攻擊精準貫穿 {target} 的防禦！"],
                         actor + target)
        if d.get("hit"):
            margin = int(d.get("total", 0)) - int(d.get("ac", 0))
            if margin >= 5:
                return f"{actor} 的攻擊紮實命中 {target}，對方連退半步。"
            return _pick([f"{actor} 勉強破防，在 {target} 身上留下一道傷口。",
                          f"{target} 閃避不及，被 {actor} 擦中。"], actor + target)
        if int(d.get("d20", 2)) <= 1:
            return f"{actor} 失手了，招式大亂，門戶大開。"
        return _pick([f"{target} 擋下了 {actor} 的攻擊。",
                      f"{actor} 的攻擊被 {target} 側身避開。"], actor + target)

    if kind == "damage":
        dmg = int(d.get("dmg", 0))
        tier = "沉重的一擊" if dmg >= 6 else "一記打擊"
        return f"{target} 受到{tier}，搖搖欲墜。" if dmg >= 4 \
            else f"{target} 吃了一記輕傷。"

    if kind == "death":
        return f"{target} 應聲倒地，不再動彈。"

    if kind == "heal":
        return f"{actor} 的傷口在光芒中癒合，氣色好轉了些。"

    if kind == "item":
        return entry.text.lstrip("🎒 ")

    if kind == "scene" and game is not None:
        scene = game.world.scenes.get(d.get("scene", ""))
        if scene:
            return (f"眾人來到{scene.name}。{scene.description}"
                    + ("眼前可往「" + "」、「".join(scene.exits.values()) + "」去。"
                       if scene.exits else ""))
        return entry.text

    if kind == "check":
        if int(d.get("d20", 2)) >= 20:
            return f"{actor} 靈光一閃，表現遠超平常。"
        if d.get("ok"):
            margin = int(d.get("total", 0)) - int(d.get("dc", 0))
            return (f"{actor} 穩穩完成了挑戰。" if margin >= 4
                    else f"{actor} 驚險過關。")
        return _pick([f"{actor} 差了一步，未能如願。",
                      f"{actor} 的嘗試落了空。"], actor)

    if kind == "combat":
        if "結束" in entry.text:
            return "塵埃落定，戰鬥結束了。"
        return "殺聲乍起，戰鬥爆發！"

    if kind == "deny" and d.get("snark"):
        item = d.get("item", "那個東西")
        return _pick([
            f"{actor} 在背包裡翻了半天——「{item}」？這裡從來沒有這種東西。"
            f"眾人一臉茫然地看著 {actor}。",
            f"{actor} 神氣地伸手一掏……掏了個空。「{item}」大概存在於"
            "某個平行宇宙的背包裡。",
            f"全場安靜了三秒。{actor}，你要不要先看看自己有什麼？"
            f"（提示：不是{item}。）",
        ], actor + str(item))

    if kind == "creative":
        u = d.get("utterance", "大膽的嘗試")
        if d.get("ok"):
            return _pick([
                f"{actor} 靈機一動——{u}——居然真的奏效了！",
                f"沒人想到這一招，但{actor}的{u}漂亮地成功了。"],
                actor + u)
        return _pick([
            f"{actor} 的{u}……創意十足，執行可惜了點。",
            f"{u}——這主意不錯，但{actor}的手不答應。"],
            actor + u)

    if kind == "cast":
        return f"{actor} 低聲詠唱，魔力在指尖凝聚。"

    if kind == "rest":
        return "隊伍稍作喘息，恢復了體力。"

    if kind == "talk":
        return entry.text

    return ""  # deny/pass/auto/meta stay silent in prose
