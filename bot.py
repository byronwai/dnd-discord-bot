"""Entry point: the Discord adapter (Discord-only bot)."""

import asyncio
import logging
import os
import sys

from engine.dm import DMEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("dnd-bot")


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


async def main():
    llm_url = env("LLM_URL", "http://127.0.0.1:8080")
    model = env("LLM_MODEL", "dnd-dm")
    data_dir = env("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))

    # optional rules retrieval (SRD) via the local embedding server
    rules_index = None
    rules_db = env("RULES_DB", os.path.join(data_dir, "rules.db"))
    embed_url = env("EMBED_URL", "http://127.0.0.1:8081")
    if os.path.exists(rules_db):
        from engine.rules import RulesIndex
        rules_index = RulesIndex(rules_db, embed_url, env("EMBED_MODEL"))
        log.info("Rules index found: %s", rules_db)
    else:
        log.info("No rules index at %s — SRD retrieval disabled", rules_db)

    engine = DMEngine(llm_url=llm_url, model=model, data_dir=data_dir,
                      max_history=int(env("MAX_HISTORY", "24")),
                      max_tokens=int(env("MAX_TOKENS", "700")), rules_index=rules_index)
    engine.embed_url = embed_url  # used by /status backend health

    discord_token = env("DISCORD_TOKEN")
    if not discord_token:
        log.error("No DISCORD_TOKEN configured. "
                  "Create a .env file from .env.example.")
        sys.exit(1)

    from adapters.discord_bot import run_discord
    try:
        await run_discord(engine, discord_token)
    except Exception as e:
        log.error("discord adapter crashed: %s", e)
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
