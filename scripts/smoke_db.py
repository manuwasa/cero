"""Smoke test: build every table in a temp SQLite DB, list what was created."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from cero.config import load_config
from cero.db.models import Base


async def main() -> None:
    cfg, _ = load_config()
    print(f"config db url: {cfg.database.url}")

    tmp = Path(tempfile.gettempdir()) / "cero_smoke.db"
    if tmp.exists():
        tmp.unlink()

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        print(f"created tables: {sorted(tables)}")
        for t in sorted(tables):
            cols = await conn.run_sync(
                lambda c, t=t: [col["name"] for col in inspect(c).get_columns(t)]
            )
            idxs = await conn.run_sync(
                lambda c, t=t: [i["name"] for i in inspect(c).get_indexes(t)]
            )
            print(f"  {t}: {len(cols)} cols, indexes={idxs}")
    await engine.dispose()
    tmp.unlink()
    print("OK smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
