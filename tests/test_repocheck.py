import subprocess
from pathlib import Path

import pytest

from bridge.repocheck import NotAGitRepo, ignored_but_present, uncommitted


def make_repo(tmp_path, ignore: str):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(ignore)
    for rel in ("pkg/data/seed.json", "data/local.db", ".venv/lib.py", "a/__pycache__/x.pyc", "vendor/m/audit/audit.jsonl", "keep/code.py"):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    (tmp_path / ".env").write_text("SECRET=1")


def test_a_packages_own_data_folder_hidden_by_an_unanchored_rule_is_found(tmp_path):
    """The real failure: 'data/' also hid aj_fusion_hcm_mcp/data, so a fresh clone could not start the MCP."""
    make_repo(tmp_path, ".env\ndata/\n.venv/\n__pycache__/\nvendor/**/audit/\n")
    found = ignored_but_present(tmp_path)
    assert found == [("pkg/data/seed.json", "data/")]                      # not the root data/, venv, caches, audit logs or .env


def test_anchoring_the_rule_leaves_nothing_hidden(tmp_path):
    make_repo(tmp_path, ".env\n/data/\n.venv/\n__pycache__/\nvendor/**/audit/\n")
    assert ignored_but_present(tmp_path) == []


def test_this_projects_ignore_file_anchors_the_data_folder_and_nothing_needed_is_hidden():
    root = Path(__file__).resolve().parents[1]
    lines = (root / ".gitignore").read_text().splitlines()
    assert "/data/" in lines and "data/" not in lines and ".env" in lines
    if not (root / ".git").exists():
        pytest.skip("this copy of the project is not a git repository")
    assert ignored_but_present(root) == []                                   # the vendored MCP's data/ folder is committable now


def test_the_docker_context_leaves_out_secrets_and_environments():
    root = Path(__file__).resolve().parents[1]
    lines = (root / ".dockerignore").read_text().splitlines()
    for needed in (".env", ".git", ".venv", "data"):
        assert needed in lines
    assert "stop_grace_period" in (root / "docker-compose.yml").read_text()
    assert "* text=auto" in (root / ".gitattributes").read_text()
    assert '"python.terminal.useEnvFile": false' in (root / ".vscode" / "settings.json").read_text()


def test_a_folder_that_is_not_a_repository_gets_a_clear_error_not_a_traceback(tmp_path):
    with pytest.raises(NotAGitRepo) as e:
        ignored_but_present(tmp_path)
    assert "not a git repository" in str(e.value)


def test_files_that_were_never_committed_are_listed_but_local_data_and_env_are_not(tmp_path):
    """A file that exists only in your folder works for you and is missing for everyone who clones."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "committed.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=tmp_path, check=True)
    assert uncommitted(tmp_path) == []
    (tmp_path / "committed.py").write_text("a = 2\n")                          # changed
    (tmp_path / "bridge").mkdir()
    (tmp_path / "bridge" / "new_module.py").write_text("b = 1\n")             # never added
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "bridge.db").write_text("x")                           # local state: not reported
    (tmp_path / ".env").write_text("SECRET=1")                                  # never reported
    got = uncommitted(tmp_path)
    assert sorted(got) == ["?? bridge/new_module.py", "M committed.py"]


def test_the_fresh_clone_check_refuses_a_python_that_lacks_the_projects_packages(tmp_path):
    """The reported failure: the script ran under the conda base Python, so the exported copy could not import claude_agent_sdk."""
    import shutil
    import sys
    proj = Path(__file__).resolve().parents[1]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    shutil.copytree(proj / "bridge", tmp_path / "bridge", ignore=shutil.ignore_patterns("__pycache__"))
    (tmp_path / "devtools").mkdir()
    shutil.copy(proj / "devtools" / "fresh_clone_check.py", tmp_path / "devtools")
    code = ("import sys, runpy; sys.modules['claude_agent_sdk'] = None; sys.argv = ['fresh_clone_check.py', '--run']; "
            "runpy.run_path('devtools/fresh_clone_check.py', run_name='__main__')")
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=90)
    out = r.stdout + r.stderr
    assert r.returncode != 0 and "Python used for the test:" in out
    assert "STOP: this Python does not have the project's packages" in out and "source .venv/Scripts/activate" in out
    assert "Activate-ps1" not in out and "Activate.ps1" in out


def test_the_runtime_requirements_leave_out_test_tools_and_the_dev_file_includes_them():
    root = Path(__file__).resolve().parents[1]
    runtime = (root / "requirements.txt").read_text()
    dev = (root / "requirements-dev.txt").read_text()
    assert "pytest" not in runtime and "-r requirements.txt" in dev and "pytest" in dev and "pytest-asyncio" in dev
    for name in ("claude-agent-sdk", "python-telegram-bot", "aiosqlite", "mcp"):
        assert name in runtime


def test_the_docker_image_gets_the_bot_and_the_operator_tools_but_not_the_developer_tools():
    root = Path(__file__).resolve().parents[1]
    docker = (root / "Dockerfile").read_text()
    copied = [ln.split()[1] for ln in docker.splitlines() if ln.startswith("COPY ")]
    assert sorted(copied) == ["bridge", "requirements.txt", "scripts", "skill", "vendor"]       # no tests, docs or devtools
    assert "requirements.txt" in docker and "requirements-dev" not in docker
    ignored = (root / ".dockerignore").read_text().split()
    for needed in ("devtools", "tests", "docs", "requirements-dev.txt", ".env"):
        assert needed in ignored


def test_outdated_day_one_material_is_gone_and_the_tools_are_where_the_readme_says():
    root = Path(__file__).resolve().parents[1]
    for gone in ("scripts/day1_spike.py", "scripts/spike_live.py", "scripts/telegram_smoke.py", "bridge/day1.py", "docs/DAY1.md",
                 "docs/DAY2.md", "docs/SAMPLE_day1_results.md", "docs/DAY2_ACCEPTANCE.md", "scripts/day1_preflight.py"):
        assert not (root / gone).exists(), gone
    for ops in ("preflight", "add_user", "telegram_ids", "runs_report", "audit_tail", "check_mcp", "pod_probe", "skill_check"):
        assert (root / "scripts" / f"{ops}.py").is_file(), ops
    for dev in ("run_all", "secret_scan", "fresh_clone_check", "pin_requirements", "describe_probe", "field_probe"):
        assert (root / "devtools" / f"{dev}.py").is_file() and not (root / "scripts" / f"{dev}.py").exists(), dev
    readme = (root / "README.md").read_text()
    for name in [p.name for p in (root / "scripts").glob("*.py")] + [p.name for p in (root / "devtools").glob("*.py")]:
        assert name in readme, f"{name} is not described in the README"
