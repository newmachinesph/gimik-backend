"""Preview Wikidata places; --apply writes, --reviewed approves reviewed sources.

Example: python scripts/import_places.py Q12345
Use real Wikidata identifiers. Default is read-only, with no database access.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.wikidata_places import fetch_entity, upsert


async def main(args) -> None:
    rows = [await fetch_entity(key) for key in dict.fromkeys(args.ids)]
    print(json.dumps(rows, ensure_ascii=True, indent=2))
    if args.apply:
        from app.db import SessionLocal, engine

        async with SessionLocal() as session:
            for row in rows:
                await upsert(session, row, reviewed=args.reviewed)
            await session.commit()
        await engine.dispose()
        print(f"Imported {len(rows)} places; reviewed={args.reviewed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ids", nargs="+")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--reviewed", action="store_true")
    asyncio.run(main(parser.parse_args()))
