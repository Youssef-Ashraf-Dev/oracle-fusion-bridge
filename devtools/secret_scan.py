"""Is there a secret in what you are about to commit or share? Reports where it is, never what it is.

    python devtools/secret_scan.py            # the files git would commit, plus the local logs and reports
    python devtools/secret_scan.py --repo     # only the files git would commit

It looks for the shapes of Anthropic keys and Telegram bot tokens, and for the exact current values of
ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN and ORACLE_PASSWORD from your .env. Exit code 1 if anything is found.
Run it before every `git commit`, and after a test session.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge.config import ROOT                                            # noqa: E402
from bridge.secretscan import candidate_files, known_secrets, local_data_files, scan_files   # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--repo", action="store_true", help="only the files git would commit")
a = ap.parse_args()
secrets = known_secrets(os.environ, ROOT / ".env")
print(f"Checking for {len(secrets)} known secret value(s) from .env/the environment, and for key and token shapes.")
repo = scan_files(ROOT, candidate_files(ROOT), secrets)
print(f"\nFiles git would commit: {len(candidate_files(ROOT))} scanned, {len(repo)} finding(s)")
for f in repo:
    print(f"  {f.path}:{f.line}  {f.kind}")
local = []
if not a.repo:
    files = local_data_files(ROOT)
    local = scan_files(ROOT, files, secrets)
    print(f"\nLocal logs and reports (stay on this machine): {len(files)} scanned, {len(local)} finding(s)")
    for f in local:
        print(f"  {f.path}:{f.line}  {f.kind}")
if repo or local:
    print("\nFOUND: remove it, rotate the secret if it was ever shared, and run this again.")
    sys.exit(1)
print("\nCLEAN: no secret found.")
