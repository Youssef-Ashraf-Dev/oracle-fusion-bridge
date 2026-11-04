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
    f.write_text('ANTHROPIC_API_KEY="from-the-file-value"\nORACLE_PASSWORD=x\nOTHER=ignored-value-here\n', encoding="utf-8")
    s = known_secrets({"TELEGRAM_BOT_TOKEN": "env-token-value-1234"}, f)
    assert s == {"ANTHROPIC_API_KEY": "from-the-file-value", "TELEGRAM_BOT_TOKEN": "env-token-value-1234"}   # "x" is too short
    assert known_secrets({}, None) == {}


def test_scanning_a_folder_without_git_skips_env_and_virtualenvs_and_reports_paths_only(tmp_path):
    (tmp_path / "app").mkdir(); (tmp_path / ".venv").mkdir(); (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "config.py").write_text(f"KEY = '{KEY}'\n", encoding="utf-8")
    (tmp_path / ".env").write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")                  # the one place a secret belongs
    (tmp_path / ".venv" / "lib.py").write_text(f"X = '{KEY}'\n", encoding="utf-8")
    (tmp_path / "tests" / "test_x.py").write_text(f"FAKE = '{TOKEN}'\n", encoding="utf-8")            # fake tokens in tests are expected
    files = candidate_files(tmp_path)
    found = scan_files(tmp_path, files, {})
    assert found == [Finding("app/config.py", 1, "Anthropic API key")]
    real = scan_files(tmp_path, files, {"TELEGRAM_BOT_TOKEN": TOKEN})                 # but a REAL token in a test is still caught
    assert Finding("tests/test_x.py", 1, "the current value of TELEGRAM_BOT_TOKEN") in real


def test_with_git_only_files_git_would_commit_are_scanned(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("data/\n.env\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "log.txt").write_text(f"{KEY}\n", encoding="utf-8")                            # ignored: not part of a commit
    (tmp_path / "notes.md").write_text(f"oops {KEY}\n", encoding="utf-8")
    names = sorted(p.name for p in candidate_files(tmp_path))
    assert "notes.md" in names and "log.txt" not in names
    assert scan_files(tmp_path, candidate_files(tmp_path), {}) == [Finding("notes.md", 1, "Anthropic API key")]


def test_local_logs_and_reports_are_scanned_but_databases_are_not(tmp_path):
    (tmp_path / "data" / "logs").mkdir(parents=True); (tmp_path / "data" / "runall").mkdir()
    (tmp_path / "data" / "logs" / "bridge.log").write_text(f"ok line\n{TOKEN}\n", encoding="utf-8")
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
    (tmp_path / "vendor" / "mcp" / "jwt.py").write_text('HEADER = "-----BEGIN PRIVATE KEY-----"\n', encoding="utf-8")
    files = candidate_files(tmp_path)
    assert scan_files(tmp_path, files, {}) == []
    (tmp_path / "vendor" / "mcp" / "oops.py").write_text("PASSWORD = 'Sup3rSecretPw'\n", encoding="utf-8")
    assert scan_files(tmp_path, candidate_files(tmp_path), {"ORACLE_PASSWORD": "Sup3rSecretPw"}) == [
        Finding("vendor/mcp/oops.py", 1, "the current value of ORACLE_PASSWORD")]


def test_the_preflights_deliberately_invalid_key_does_not_look_like_a_real_key():
    from bridge.secretscan import PATTERNS
    text = Path(__file__).resolve().parents[1].joinpath("bridge", "preflight.py").read_text(encoding="utf-8")
    assert "sk-ant-no-credit" in text and not PATTERNS["Anthropic API key"].search(text)


# ---- scanning every commit ----------------------------------------------------------------------------------------

def commit(repo, files: dict, message: str, name="Dev One", email="one@example.com"):
    for rel, text in files.items():
        f = repo / rel
        if text is None:
            f.unlink()
        else:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    import os
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", message], cwd=repo, check=True, env={**os.environ, **env})
    return subprocess.run(["git", "rev-parse", "--short=8", "HEAD"], cwd=repo, capture_output=True, text=True, check=True, encoding="utf-8", errors="replace").stdout.strip()


def history_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    a = commit(tmp_path, {"app/config.py": f"KEY = '{KEY}'\n", "notes.md": "hello\n"}, "adds a key", "Dev One", "one@example.com")
    b = commit(tmp_path, {"app/config.py": "KEY = ''\n"}, "removes the key", "Dev Two", "two@example.com")
    return a, b


def test_a_secret_that_was_removed_later_is_still_found_in_the_commit_that_had_it(tmp_path):
    """The old token sat in 8 commits and was removed in a later one: the files today are clean, the history is not."""
    from bridge.secretscan import HistoryFinding, scan_history
    a, b = history_repo(tmp_path)
    found = scan_history(tmp_path, {})
    assert found == [HistoryFinding(a, "app/config.py", "Anthropic API key")]          # the commit that had it, not the one that fixed it
    assert KEY not in str(found)                                                        # where and what kind, never the value
    assert scan_files(tmp_path, candidate_files(tmp_path), {}) == []                    # the working files alone would have said CLEAN


def test_exact_values_and_extra_words_are_found_everywhere_including_tests_and_vendor(tmp_path):
    from bridge.secretscan import scan_history
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    commit(tmp_path, {"tests/test_x.py": "pw = 'Sup3rSecretPw'\n", "vendor/m/manifest.json": '{"host": "Client-Dev1.example.com"}\n',
                      "tests/fake.py": f"FAKE = '{TOKEN}'\n"}, "one")
    found = scan_history(tmp_path, {"ORACLE_PASSWORD": "Sup3rSecretPw"}, also=["client-dev1"])
    kinds = {(f.path, f.kind.split(" (")[0]) for f in found}
    assert ("tests/test_x.py", "a current secret value") in kinds                       # a real value is caught even inside tests
    assert ("vendor/m/manifest.json", "an extra word you asked for") in kinds           # extra words are case-insensitive
    assert not any(f.path == "tests/fake.py" for f in found)                            # fake tokens in tests are expected
    assert "Sup3rSecretPw" not in str(found) and "ORACLE_PASSWORD" in str(found)


def test_a_clean_history_is_clean_and_chunking_does_not_change_the_answer(tmp_path):
    from bridge.secretscan import identities, scan_history
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for i in range(7):
        commit(tmp_path, {f"f{i}.txt": f"line {i}\n"}, f"c{i}")
    assert scan_history(tmp_path, {}, chunk=2) == [] == scan_history(tmp_path, {}, chunk=50)
    a, b = history_repo(tmp_path)
    assert scan_history(tmp_path, {}, chunk=2) == scan_history(tmp_path, {}, chunk=50) and len(scan_history(tmp_path, {}, chunk=2)) == 1
    assert identities(tmp_path)[:2] == ["Dev One <one@example.com>", "Dev Two <two@example.com>"] or "Dev Two <two@example.com>" in identities(tmp_path)


def test_every_branch_is_searched_not_only_the_current_one(tmp_path):
    from bridge.secretscan import scan_history
    a, b = history_repo(tmp_path)
    subprocess.run(["git", "checkout", "-q", "-b", "other", a], cwd=tmp_path, check=True)                 # a branch that still has the key
    subprocess.run(["git", "checkout", "-q", "-"], cwd=tmp_path, check=True)
    subprocess.run(["git", "branch", "-q", "-f", "main", b], cwd=tmp_path, check=False)
    assert any(f.commit == a for f in scan_history(tmp_path, {}))


def test_the_history_flag_exists_and_the_scanner_explains_it():
    root = Path(__file__).resolve().parents[1]
    text = (root / "devtools" / "secret_scan.py").read_text(encoding="utf-8")
    assert "--history" in text and "--also" in text and "identities" in text
