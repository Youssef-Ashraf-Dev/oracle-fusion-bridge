"""Pin requirements.txt to the exact versions installed right now, and write constraints.txt for everything else.

    python scripts/pin_requirements.py            # show what would change (nothing is written)
    python scripts/pin_requirements.py --write    # rewrite requirements.txt and write constraints.txt
    python scripts/pin_requirements.py --check    # exit 1 if the installed versions differ from the pins

Run it in the project's .venv, with the packages you have been testing with. requirements.txt then holds the direct
dependencies at exact versions. constraints.txt holds every installed package, so a Docker or Linux install gets the same
versions of the packages underneath too. Install with: pip install -r requirements.txt -c constraints.txt
"""
import argparse
import sys
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import ROOT                                                    # noqa: E402
from bridge.pins import check_pins, constraints_text, installed_versions, pin_requirements   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--write", action="store_true")
ap.add_argument("--check", action="store_true")
a = ap.parse_args()

req_path, con_path = ROOT / "requirements.txt", ROOT / "constraints.txt"
old = req_path.read_text(encoding="utf-8")
if a.check:
    problems = check_pins(old, installed_versions())
    print("\n".join(problems) if problems else "OK: every requirement is pinned and matches what is installed.")
    sys.exit(1 if problems else 0)

if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
    print(f"WARNING: {sys.executable} is not a virtual environment. Activate the project's .venv first, or the pins will come from the wrong Python.")
    if a.write:
        sys.exit(2)
new, missing = pin_requirements(old, installed_versions())
print(f"Python: {sys.executable}\n\nrequirements.txt, before and after:")
for before, after in zip(old.splitlines(), new.splitlines()):
    print(f"  {before:34} ->  {after}" if before != after else f"  {before}")
if missing:
    print(f"\nNOT INSTALLED here (left as they were): {', '.join(missing)}. Install them first.")
con = constraints_text(metadata.distributions())
print(f"\nconstraints.txt would list {len(con.splitlines()) - 2} packages.")
if a.write:
    req_path.write_text(new, encoding="utf-8")
    con_path.write_text(con, encoding="utf-8")
    print("\nWROTE requirements.txt and constraints.txt. Commit both: git add -A; git commit")
else:
    print("\nNothing written. Run again with --write to apply.")
