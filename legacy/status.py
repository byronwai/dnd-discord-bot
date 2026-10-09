"""Build the /status report shared by all adapters."""

import time


def _ago(ts: float | None) -> str:
    if not ts:
        return "never"
    d = max(0, int(time.time() - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


async def build_status(engine, platform: str, chat_id: str) -> str:
    """One /status report: session, progress, party, backend health."""
    info = engine.session_info(platform, chat_id)

    lines = ["📊 **Game Status / 遊戲狀態**", ""]
    if info["created_at"]:
        started = time.strftime("%m-%d %H:%M", time.localtime(info["created_at"]))
        lines.append(f"🗓 Session: started {started} · "
                     f"{info['messages']} messages · last turn {_ago(info['updated_at'])}")

    # progress: scene / objective / story
    from .commands import DIFFICULTY_LABELS
    diff = info.get("difficulty") or "easy"
    lines.append(f"🎚 難度 Difficulty: **{DIFFICULTY_LABELS.get(diff, diff)}**")
    if info.get("scene"):
        lines.append(f"📍 **Scene / 目前場景**: {info['scene']}")
    if info.get("objective"):
        lines.append(f"🎯 **Objective / 目前目標**: {info['objective']}")
    summary = (info["summary"] or "").strip()
    if summary:
        if len(summary) > 300:
            summary = summary[:299] + "…"
        lines.append(f"📖 Story so far / 劇情摘要: {summary}")
    elif info.get("beats"):
        lines.append("📖 Story so far / 最近進展:")
        for b in info["beats"]:
            b = b.replace("\n", " ").strip()
            lines.append(f"  › {b[:200]}{'…' if len(b) > 200 else ''}")
    else:
        lines.append("📖 Story: not started yet — `/new` 開場 / 仍未開場")

    pending = engine.get_pending_check(platform, chat_id)
    if pending:
        ab = f" {pending['ability']}" if pending.get("ability") else ""
        lines.append(f"🎯 **Pending check / 待決檢定**: {pending['char']}{ab} vs "
                     f"DC {pending['dc']} — reply `骰`/`roll` to roll it")

    # DM notebook: named NPCs & established facts
    npcs = engine.npc_list(platform, chat_id, limit=12)
    lore = engine.lore_list(platform, chat_id, limit=12)
    if npcs or lore:
        lines.append("")
        lines.append("📝 **DM 筆記 / DM Notes**")
        for n in npcs:
            st = f"（{n['status']}）" if n["status"] else ""
            lines.append(f"  🧑 {n['name']}：{n['desc']}{st}")
        for l in lore:
            lines.append(f"  📜 {l['key']}：{l['fact']}")
    combat = engine.combat_get(platform, chat_id)
    if combat and combat.get("order"):
        cur = combat["order"][combat["idx"] % len(combat["order"])]
        wait_min = int((time.time() - combat.get("since", time.time())) / 60)
        lines.append("")
        lines.append(f"⚔️ **Combat / 戰鬥中** — Round {combat.get('round', 1)} · "
                     f"current move: **{cur['name']}** (waiting {wait_min} min) · "
                     f"order: " + " → ".join(o["name"] for o in combat["order"]))

    # party with HP + inventory — same code-block format as /party
    lines.append("")
    lines.append(party_header())
    lines.append(f"```\n{engine.party_text(platform, chat_id)}\n```")
    return "\n".join(lines)


def party_header() -> str:
    return "🧙 Party / 隊伍一覽："
