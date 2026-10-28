"""python scripts/add_user.py 123456789 "Sam Consultant"   (ask them to message @userinfobot for their id)"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.config import load_settings   # noqa: E402
from bridge.state.db import Database      # noqa: E402


async def main():
    db = await Database(load_settings().db_path).open()
    await db.add_user(int(sys.argv[1]), " ".join(sys.argv[2:]))
    await db.close()
    print("added")

asyncio.run(main())
