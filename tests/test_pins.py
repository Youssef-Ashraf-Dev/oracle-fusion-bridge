import json
import sys
from types import SimpleNamespace

from bridge.pins import check_pins, constraints_text, installed_versions, norm, pin_requirements, requirements
from bridge.preflight import PASS, WARN, check_runtime

REQ = """# the bot
claude-agent-sdk>=0.2.174
python-telegram-bot[job-queue]>=22.8   # Telegram
mcp>=1.2,<2
uvloop>=0.19 ; sys_platform != "win32"  # linux only
-r other.txt

pytest>=8
"""


def dist(name, version, direct_url=None):
    return SimpleNamespace(metadata={"Name": name}, version=version,
                           read_text=lambda f, _d=direct_url: json.dumps(_d) if (_d is not None and f == "direct_url.json") else None)


def test_names_are_compared_the_way_pip_compares_them():
    assert norm("Python_Telegram.Bot") == "python-telegram-bot" == norm("python-telegram-bot")


def test_requirement_lines_are_read_and_comments_options_and_blanks_are_skipped():
    got = requirements(REQ)
    assert [n for n, _ in got] == ["claude-agent-sdk", "python-telegram-bot", "mcp", "uvloop", "pytest"]
    assert dict(got)["mcp"] == ">=1.2,<2" and dict(got)["pytest"] == ">=8"


def test_pinning_rewrites_each_line_to_the_installed_version_and_keeps_extras_markers_and_comments():
    versions = {"claude-agent-sdk": "0.2.174", "python-telegram-bot": "22.8", "mcp": "1.30.0", "pytest": "9.2.0"}
    new, missing = pin_requirements(REQ, versions)
    lines = new.splitlines()
    assert "claude-agent-sdk==0.2.174" in lines and "mcp==1.30.0" in lines and "pytest==9.2.0" in lines
    assert any(l.startswith("python-telegram-bot[job-queue]==22.8") and "# Telegram" in l for l in lines)
    assert missing == ["uvloop"] and any(l.startswith("uvloop>=0.19") and "linux only" in l for l in lines)   # not installed: left as it was
    assert lines[0] == "# the bot" and "-r other.txt" in lines                                                  # comments and options untouched


def test_the_check_reports_unpinned_missing_and_different_versions():
    pinned = "a==1.0\nb==2.0\nc==3.0\nd>=4\n"
    problems = check_pins(pinned, {"a": "1.0", "b": "2.1"})
    assert problems == ["b: pinned 2.0, installed 2.1", "c: pinned 3.0 but not installed", "d: not pinned (>=4)"]
    assert check_pins("a==1.0\n", {"a": "1.0"}) == []


def test_constraints_list_everything_installed_once_sorted_and_leave_out_pip_and_local_installs():
    dists = [dist("Zeta", "1.0"), dist("alpha", "2.0"), dist("pip", "24.0"), dist("setuptools", "70"),
             dist("mine", "0.1", direct_url={"url": "file:///c:/work/mine", "dir_info": {"editable": True}}),
             dist("alpha", "9.9"), dist("fromgit", "1.0", direct_url={"url": "https://x/y.git", "vcs_info": {}})]
    text = constraints_text(dists)
    body = [l for l in text.splitlines() if not l.startswith("#")]
    assert body == ["alpha==2.0", "fromgit==1.0", "Zeta==1.0"]
    assert text.startswith("# Exact versions installed") and "pip install -r requirements.txt -c constraints.txt" in text


def test_this_machines_installed_versions_can_be_read():
    v = installed_versions()
    assert "pytest" in v and v["pytest"][0].isdigit()


def test_the_preflight_reports_whether_the_environment_and_the_pins_are_in_order(tmp_path, monkeypatch):
    by = lambda checks: {c.name: c for c in checks}
    v = installed_versions()
    good = tmp_path / "good.txt"
    good.write_text(f"pytest=={v['pytest']}\n")
    assert by(check_runtime(lambda n: "/bin/uv", good))["Dependencies pinned and matching"].status == PASS
    loose = tmp_path / "loose.txt"
    loose.write_text("pytest>=8\npytest-asyncio>=0.24\n")
    c = by(check_runtime(lambda n: "/bin/uv", loose))["Dependencies pinned and matching"]
    assert c.status == WARN and "2 requirement(s) not pinned" in c.detail and "pin_requirements.py --write" in c.detail
    stale = tmp_path / "stale.txt"
    stale.write_text("pytest==0.0.1\n")
    assert "pinned 0.0.1, installed" in by(check_runtime(lambda n: "/bin/uv", stale))["Dependencies pinned and matching"].detail
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)                       # not inside a virtual environment
    venv = by(check_runtime(lambda n: "/bin/uv", good))["Virtual environment active"]
    assert venv.status == WARN and "Activate the project's .venv" in venv.detail and "source .venv/Scripts/activate" in venv.detail
