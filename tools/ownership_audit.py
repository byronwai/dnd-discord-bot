"""Ownership audit: is the correct player controlling each character?

Reads the campaign DB and reports, per session:
  1. every character: occupation, owner (name + id), HP, inventory count
  2. actual message activity grouped by (name, user_id)
  3. flags:
     - NAME-COLLISION  same display name used by 2+ different user ids
     - WRONG-CONTROL   messages sent under a character's name by a user
                       whose id != the character's owner_id
     - NO-ACTIVITY     character exists but its owner never posted
     - PRE-TRACKING    activity predates user_id capture (cannot verify)

Usage: ~/dnd-dm-bot/venv/bin/python ~/dnd-dm-bot/tools/ownership_audit.py
"""

import os
import sys
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from engine.dm import DMEngine  # noqa: E402


def main():
    data_dir = os.path.join(BASE, "data")
    e = DMEngine("http://127.0.0.1:8080", "dnd-dm", data_dir)

    sessions = e.db.execute(
        "SELECT platform, chat_id FROM sessions ORDER BY updated_at DESC").fetchall()
    if not sessions:
        print("(no sessions)")
        return
    problems = 0
    seen = set()
    for platform, cid in sessions:
        r_platform, r_cid = platform, cid
        if (r_platform, r_cid) in seen:
            continue
        seen.add((r_platform, r_cid))
        party = e.get_party(platform, cid)
        print("=" * 64)
        print(f"session {platform}/{cid}  -> storage {r_platform}/{r_cid}")

        # author activity: name -> {user_id: count}
        activity = defaultdict(Counter)
        rows = e.db.execute(
            "SELECT name, user_id, COUNT(*) FROM messages "
            "WHERE platform=? AND chat_id=? AND role='user' GROUP BY name, user_id",
            (r_platform, r_cid)).fetchall()
        for name, uid, cnt in rows:
            activity[name][uid or "(pre-tracking)"] += cnt

        if not party:
            print("  party: (empty)")
            continue

        for char, entry in party.items():
            if not isinstance(entry, dict):
                print(f"  ? {char}: legacy entry {str(entry)[:60]}")
                continue
            owner = entry.get("owner", "?")
            owner_id = str(entry.get("owner_id") or "")
            inv = e.inv_list(r_platform, r_cid, char).get(char, [])
            hp = (f"{entry.get('hp_now', '?')}/{entry.get('hp_max', '?')}"
                  if "hp_now" in entry else "no HP yet")
            print(f"  char {char} [{entry.get('occupation', '?')}] "
                  f"owner={owner} ({owner_id or 'no id'}) HP={hp} items={len(inv)}")

            # who actually posts under this character's name?
            authors = activity.get(char, {})
            if not authors:
                print(f"    - NO-ACTIVITY: owner never posted under this name")
                problems += 1
                continue
            for uid, cnt in authors.items():
                if uid.startswith("("):
                    print(f"    - PRE-TRACKING: {cnt} messages, id unknown")
                    problems += 1
                elif owner_id and uid != owner_id:
                    print(f"    !! WRONG-CONTROL: {cnt} messages from user {uid} "
                          f"but owner is {owner_id}")
                    problems += 1
                else:
                    print(f"    ok: {cnt} messages from owner {uid}")

        # name collisions between different users
        for name, by_uid in activity.items():
            real = [u for u in by_uid if not u.startswith("(")]
            if len(real) > 1:
                print(f"  !! NAME-COLLISION: '{name}' used by ids {real}")
                problems += 1

    print("=" * 64)
    print("PROBLEMS:", problems)


if __name__ == "__main__":
    main()
