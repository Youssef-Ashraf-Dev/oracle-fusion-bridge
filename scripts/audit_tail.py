"""What did the bot ask Fusion? The last audit rows with ids and literals masked, for building pod recipes.

    python scripts/audit_tail.py            # last 40 rows
    python scripts/audit_tail.py --last 100
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from bridge.auditview import render      # noqa: E402
from bridge.config import load_settings  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--last", type=int, default=40)
ap.add_argument("--db", default=None, help="path to the SQLite file (default: DB_PATH from .env)")
a = ap.parse_args()
db = Path(a.db) if a.db else load_settings().db_path
if not db.is_file():
    sys.exit(f"No database at {db}. Run a question first.")
print(render(db, a.last))
