"""Run as a scheduled job: python scripts/assess_places.py --limit 20.

No raw queries are sent to OpenAI. Purges expired private search history even
when AI is disabled. The database is the durable queue; no in-process tasks.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal, engine
from app.services.place_catalog import drain, purge_search_history


async def main(limit: int) -> None:
    async with SessionLocal() as session:
        purged = await purge_search_history(session)
        processed = await drain(session, limit)
        print(f"Assessments processed: {processed}; expired search events removed: {purged}")
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=20, choices=range(1, 101))
    args = parser.parse_args()
    asyncio.run(main(args.limit))
