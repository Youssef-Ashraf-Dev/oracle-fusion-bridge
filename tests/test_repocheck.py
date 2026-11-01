import subprocess
from pathlib import Path

import pytest

from bridge.repocheck import NotAGitRepo, ignored_but_present


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
