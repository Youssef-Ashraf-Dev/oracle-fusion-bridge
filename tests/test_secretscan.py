import subprocess
from pathlib import Path

from bridge.secretscan import Finding, candidate_files, known_secrets, local_data_files, scan_files, scan_text

KEY = "sk-ant-usr-" + "A1b2C3d4E5f6G7h8I9j0"
TOKEN = "1234567890:" + "AAFakeTokenForTestsOnly_0123456789abc"


def test_shapes_and_exact_values_are_found_by_line_without_returning_the_secret():
    text = f"line one\nthe key is {KEY} here\nand Sup3rSecretPw appears\n"
    hits = scan_text(text, {"ORACLE_PASSWORD": "Sup3rSecretPw"})
    assert (2, "Anthropic API key") in hits and (3, "the current value of ORACLE_PASSWORD") in hits
    assert all(KEY not in str(h) and "Sup3rSecretPw" not in str(h) for h in hits)
    assert (1, "Telegram bot token") in scan_text(f"POST /bot{TOKEN}/getMe", {})
    assert scan_text("nothing here, not even sk-ant- or 12:30", {}) == []
    assert scan_text("-----BEGIN RSA PRIVATE KEY-----", {}) == [(1, "private key block")]


def test_known_secrets_come_from_the_environment_and_the_env_file_and_short_values_are_ignored(tmp_path):
    f = tmp_path / ".env"
    f.write_text('ANTHROPIC_API_KEY="from-the-file-value"\nORACLE_PASSWORD=x\nOTHER=ignored-value-here\n')
    s = known_secrets({"TELEGRAM_BOT_TOKEN": "env-token-value-1234"}, f)
    assert s == {"ANTHROPIC_API_KEY": "from-the-file-value", "TELEGRAM_BOT_TOKEN": "env-token-value-1234"}   # "x" is too short
    assert known_secrets({}, None) == {}


def test_scanning_a_folder_without_git_skips_env_and_virtualenvs_and_reports_paths_only(tmp_path):
    (tmp_path / "app").mkdir(); (tmp_path / ".venv").mkdir(); (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "config.py").write_text(f"KEY = '{KEY}'\n")
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n")                  # the one place a secret belongs
    (tmp_path / ".venv" / "lib.py").write_text(f"X = '{KEY}'\n")
    (tmp_path / "tests" / "test_x.py").write_text(f"FAKE = '{TOKEN}'\n")            # fake tokens in tests are expected
    files = candidate_files(tmp_path)
    found = scan_files(tmp_path, files, {})
    assert found == [Finding("app/config.py", 1, "Anthropic API key")]
    real = scan_files(tmp_path, files, {"TELEGRAM_BOT_TOKEN": TOKEN})                 # but a REAL token in a test is still caught
    assert Finding("tests/test_x.py", 1, "the current value of TELEGRAM_BOT_TOKEN") in real


def test_with_git_only_files_git_would_commit_are_scanned(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("data/\n.env\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "log.txt").write_text(f"{KEY}\n")                            # ignored: not part of a commit
    (tmp_path / "notes.md").write_text(f"oops {KEY}\n")
    names = sorted(p.name for p in candidate_files(tmp_path))
    assert "notes.md" in names and "log.txt" not in names
    assert scan_files(tmp_path, candidate_files(tmp_path), {}) == [Finding("notes.md", 1, "Anthropic API key")]


def test_local_logs_and_reports_are_scanned_but_databases_are_not(tmp_path):
    (tmp_path / "data" / "logs").mkdir(parents=True); (tmp_path / "data" / "runall").mkdir()
    (tmp_path / "data" / "logs" / "bridge.log").write_text(f"ok line\n{TOKEN}\n")
    (tmp_path / "data" / "runall" / "run.db").write_bytes(b"\x00binary")
    files = local_data_files(tmp_path)
    assert [p.name for p in files] == ["bridge.log"]
    assert scan_files(tmp_path, files, {}) == [Finding("data/logs/bridge.log", 2, "Telegram bot token")]


def test_this_projects_own_files_are_clean():
    """The repository as shipped holds no key or token shape outside tests, and no real value."""
    root = Path(__file__).resolve().parents[1]
    assert scan_files(root, candidate_files(root), {}) == []


def test_vendor_code_is_skipped_for_shapes_but_not_for_real_values(tmp_path):
    (tmp_path / "vendor" / "mcp").mkdir(parents=True)
    (tmp_path / "vendor" / "mcp" / "jwt.py").write_text('HEADER = "-----BEGIN PRIVATE KEY-----"\n')
    files = candidate_files(tmp_path)
    assert scan_files(tmp_path, files, {}) == []
    (tmp_path / "vendor" / "mcp" / "oops.py").write_text("PASSWORD = 'Sup3rSecretPw'\n")
    assert scan_files(tmp_path, candidate_files(tmp_path), {"ORACLE_PASSWORD": "Sup3rSecretPw"}) == [
        Finding("vendor/mcp/oops.py", 1, "the current value of ORACLE_PASSWORD")]


def test_the_preflights_deliberately_invalid_key_does_not_look_like_a_real_key():
    from bridge.secretscan import PATTERNS
    text = Path(__file__).resolve().parents[1].joinpath("bridge", "preflight.py").read_text()
    assert "sk-ant-no-credit" in text and not PATTERNS["Anthropic API key"].search(text)
