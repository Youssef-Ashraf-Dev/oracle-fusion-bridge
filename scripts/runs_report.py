"""What has the bot been doing? Recent questions with status, cost, time, plans and tools, plus the reasons for refusals.

    python scripts/runs_report.py             # the last 30 questions
    python scripts/runs_report.py --last 100
    python scripts/runs_report.py --db data/runall/run.db

Reads data/bridge.db (the bot's own record). Questions and answers are not in it, only the shape of each run.
The text log is data/logs/bridge.log: one 'run ...' line per question, 'deny ...' per refusal, 'engine: ...' for the engine's errors.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.logsafe import safe_streams     # noqa: E402
safe_streams()

from bridge.config import load_settings   # noqa: E402
from bridge.runsreport import render      # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--last", type=int, default=30)
ap.add_argument("--db", default=None)
a = ap.parse_args()
db = Path(a.db) if a.db else load_settings().db_path
if not db.is_file():
    sys.exit(f"No database at {db}. Run a question first.")
print(render(db, a.last))
