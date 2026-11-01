"""Will a fresh clone of this repository work? Run it before you push, and again after you commit.

    python scripts/fresh_clone_check.py           # fast: files here that git would NOT carry into a clone
    python scripts/fresh_clone_check.py --run     # also: export the last commit to a temp folder and start the real MCP there

A clone is what Day 3's server, a colleague's laptop and a Docker build will see. The fast check finds files hidden by the
ignore rules. The --run check proves the committed files really start (it needs the network the first time, for the MCP's
packages, and takes a minute or two). Commit first, because --run tests what is committed.
"""
import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import ROOT                       # noqa: E402
from bridge.repocheck import NotAGitRepo, ignored_but_present, uncommitted     # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--run", action="store_true", help="export the last commit and start the real MCP from it")
ap.add_argument("--keep", action="store_true", help="with --run: keep the exported folder and print where it is, so you can look inside")
a = ap.parse_args()

try:
    bad = ignored_but_present(ROOT)
except NotAGitRepo as e:
    sys.exit(f"Cannot check: {e}")
print(f"Files that exist here but that git ignores (a clone would not have them): {len(bad)}")
for path, rule in bad:
    print(f"  {path}   <- ignore rule '{rule}'")
if bad:
    print("\nFIX: anchor or narrow the rule in .gitignore (for example write /data/ instead of data/), then `git add -A`.")
status = 1 if bad else 0
pending = uncommitted(ROOT)
if pending:
    print(f"\nNOT IN THE LAST COMMIT: {len(pending)} changed or new file(s). A clone, and the --run test below, only have what is committed:")
    for p in pending[:15]:
        print(f"  {p}")
    print("  (M = changed, ?? = never added, A = added but not committed). Run `git add -A` and `git commit`, then run this again.")
    status = 1

if a.run:
    tmp_root = tempfile.mkdtemp(prefix="freshclone_")
    try:
        tmp = tmp_root
        zpath = Path(tmp) / "head.zip"
        subprocess.run(["git", "archive", "--format=zip", "-o", str(zpath), "HEAD"], cwd=ROOT, check=True)
        out = Path(tmp) / "clone"
        with zipfile.ZipFile(zpath) as z:
            z.extractall(out)
        print(f"\nExported the last commit to a temp folder ({sum(1 for _ in out.rglob('*') if _.is_file())} files). Starting the real MCP there...")
        env = {**os.environ, "RUN_MCP_TESTS": "1"}
        p = subprocess.run([sys.executable, "-m", "pytest", "tests/test_preflight.py", "-q", "-p", "no:cacheprovider", "-k", "real_bundle"],
                           cwd=out, env=env, capture_output=True, text=True, timeout=900)
        lines = [ln for ln in (p.stdout + p.stderr).splitlines() if ln.strip()]
        print("\n".join("  " + ln for ln in (lines[-3:] if p.returncode == 0 else lines[-40:])))
        if p.returncode != 0:
            print("\nFAIL: the committed files do not pass the real-MCP test. The error above says why; "
                  "the usual cause is a file that was never committed.")
            status = 1
        else:
            print("\nPASS: the committed files start the real MCP.")
        if a.keep:
            print(f"\nKept the exported folder: {out}\nLook inside, or run: cd \"{out}\"; python -m pytest tests/test_preflight.py -q -k real_bundle")
    finally:
        if not a.keep:
            import shutil
            shutil.rmtree(tmp_root, ignore_errors=True)
sys.exit(status)
