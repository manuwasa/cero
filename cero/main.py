"""
Cero — entry point.

Starts all background workers and the web server as concurrent asyncio tasks.
Run with:  python -m cero
"""
from __future__ import annotations

import asyncio
import signal
import sys

from loguru import logger


async def main() -> None:
    """Boot Cero. All workers run as asyncio tasks in this single process."""
    # TODO: implement in Claude Code
    #
    # 1. Load config (cero.config.load_config())
    # 2. Initialize logging (cero.utils.logging.setup(config))
    # 3. Initialize DB (cero.db.init(config))
    # 4. Construct shared state (cero.state.AppState)
    # 5. Start workers as tasks:
    #       - price_worker
    #       - account_worker
    #       - news_worker
    #       - calendar_worker
    #       - brain scheduler
    #       - telegram bot
    #       - FastAPI web server
    # 6. Wait for shutdown signal (SIGINT/SIGTERM)
    # 7. Cancel all tasks gracefully
    # 8. Close DB, log exit
    logger.info("Cero starting (not yet implemented — open in Claude Code)")
    logger.info("See CLAUDE.md and docs/ARCHITECTURE.md for implementation order")


def run() -> None:
    """Console entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Bye.")
        sys.exit(0)


if __name__ == "__main__":
    run()
