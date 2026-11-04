"""Rules that keep the project working on Windows as well as Linux, enforced by reading the source.

Each rule names the Windows behaviour behind it. The project runs on Windows (the team's machines) and in a Linux container,
and most of it was first written on Linux: a test that signals its own process silently ended the whole run on Windows.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("bridge", "scripts", "devtools")
ALL_DIRS = CODE_DIRS + ("tests",)
THIS = Path(__file__).resolve()


def py_files(dirs):
    for d in dirs:
        for f in sorted((ROOT / d).rglob("*.py")):
            if f.resolve() != THIS:
                yield f


def calls(dirs):
    for f in py_files(dirs):
        src = f.read_text(encoding="utf-8")
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "attr", getattr(n.func, "id", ""))
                yield f, n, name, {k.arg for k in n.keywords}


def where(f, n):
    return f"{f.relative_to(ROOT).as_posix()}:{n.lineno}"


def test_text_files_are_always_read_and_written_with_a_named_encoding():
    """Windows defaults to a legacy code page: Arabic text is garbled or raises, and a UTF-8 file is misread."""
    bad = []
    for f, n, name, kws in calls(ALL_DIRS):
        if name == "read_text" and not n.args and "encoding" not in kws:
            bad.append(where(f, n))
        elif name == "write_text" and len(n.args) == 1 and "encoding" not in kws:
            bad.append(where(f, n))
        elif name == "open" and isinstance(n.func, ast.Name) and "encoding" not in kws:
            mode = n.args[1].value if len(n.args) > 1 and isinstance(n.args[1], ast.Constant) else ""
            if "b" not in str(mode):
                bad.append(where(f, n))
    assert bad == [], f"name the encoding (encoding='utf-8'), or use bridge.textio.read_text for files people edit by hand: {bad}"


def test_text_captured_from_a_subprocess_names_its_encoding():
    """git and pytest print UTF-8; reading it with the Windows code page raises on bytes such as those in Arabic letters."""
    bad = [where(f, n) for f, n, name, kws in calls(ALL_DIRS)
           if name in ("run", "check_output", "Popen") and "encoding" not in kws
           and any(k.arg in ("text", "universal_newlines") and isinstance(k.value, ast.Constant) and k.value.value for k in n.keywords)]
    assert bad == [], f"add encoding='utf-8', errors='replace': {bad}"


POSIX_MODULES = {"fcntl", "pwd", "grp", "resource", "termios", "tty", "pty", "syslog", "readline"}
POSIX_ATTRS = {("os", "getuid"), ("os", "geteuid"), ("os", "getgid"), ("os", "fork"), ("os", "killpg"), ("os", "setsid"), ("os", "chown"),
               ("os", "symlink"), ("os", "mkfifo"), ("os", "uname"), ("signal", "SIGKILL"), ("signal", "SIGHUP"), ("signal", "SIGUSR1"),
               ("signal", "SIGUSR2"), ("signal", "SIGALRM"), ("signal", "SIGCHLD"), ("signal", "SIGQUIT"), ("signal", "alarm")}


def test_nothing_that_exists_only_on_linux_is_used():
    bad = []
    for f in py_files(ALL_DIRS):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                bad += [f"{f.name}:{n.lineno} import {a.name}" for a in n.names if a.name.split(".")[0] in POSIX_MODULES]
            elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in POSIX_MODULES:
                bad.append(f"{f.name}:{n.lineno} from {n.module}")
            elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and (n.value.id, n.attr) in POSIX_ATTRS:
                bad.append(f"{f.name}:{n.lineno} {n.value.id}.{n.attr}")
    assert bad == [], f"these do not exist on Windows: {bad}"


def test_the_event_loop_signal_api_is_used_only_in_the_one_place_that_guards_it():
    """loop.add_signal_handler raises NotImplementedError on Windows; bridge/main.py picks the right method per platform."""
    where_used = {where(f, n) for f, n, name, _ in calls(CODE_DIRS) if name in ("add_signal_handler", "add_reader", "add_writer",
                                                                                "connect_read_pipe", "connect_write_pipe")}
    assert all(w.startswith("bridge/main.py:") for w in where_used), where_used


def test_no_shell_true_and_no_temporary_file_that_has_to_be_reopened():
    """shell=True means cmd.exe on Windows and sh on Linux, with different quoting. A NamedTemporaryFile cannot be reopened on Windows."""
    bad = []
    for f, n, name, kws in calls(ALL_DIRS):
        if "shell" in kws and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords):
            bad.append(where(f, n))
        if name == "NamedTemporaryFile":
            bad.append(where(f, n))
    assert bad == []


def test_dates_are_never_formatted_with_the_computers_language():
    """%A, %B and friends return Arabic names on an Arabic-locale PC. Use bridge.dates for names, numeric formats otherwise."""
    rx = re.compile(r"%[AaBbcxXp]")
    bad = []
    for f in py_files(CODE_DIRS):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if ("strftime" in code or re.search(r"\{[^{}]*:[^{}]*%", code)) and rx.search(code):
                bad.append(f"{f.relative_to(ROOT).as_posix()}:{n}")
    assert bad == [], bad


@pytest.mark.parametrize("prefix", ["/tmp/", "/home/", "/var/", "/usr/", "/etc/", "/opt/"])
def test_no_hard_coded_linux_paths_in_code_or_tests(prefix):
    """'/tmp/x' is not an absolute path on Windows (no drive), so code and tests treat it differently there. Build paths from tmp_path."""
    bad = []
    for f in py_files(ALL_DIRS):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(prefix):
                bad.append(f"{f.relative_to(ROOT).as_posix()}:{n.lineno}")
    assert bad == [], bad


def test_every_script_protects_its_console_from_characters_it_cannot_show():
    """Output redirected to a file, or a legacy console, cannot show Arabic names from the pod, and print() would raise."""
    missing = [f.relative_to(ROOT).as_posix() for d in ("scripts", "devtools") for f in sorted((ROOT / d).glob("*.py"))
               if "safe_streams()" not in f.read_text(encoding="utf-8")]
    assert missing == [], f"add `from bridge.logsafe import safe_streams` and call safe_streams(): {missing}"


def test_files_that_people_edit_by_hand_are_read_through_the_tolerant_reader():
    """A .env, skill or recipes file saved with a BOM or as UTF-16 (Notepad, PowerShell 5) must still be read correctly."""
    for rel, needle in (("bridge/config.py", "read_text(path)"), ("bridge/live/skills.py", "read_text(skill_md)"),
                        ("bridge/live/prompt.py", "read_text(path)"), ("bridge/preflight.py", "read_text(req)")):
        assert needle in (ROOT / rel).read_text(encoding="utf-8"), f"{rel} should read through bridge.textio.read_text"


def test_the_ci_workflow_runs_the_suite_on_both_windows_and_linux_with_the_pinned_packages():
    text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "windows-latest" in text and "ubuntu-latest" in text and "fail-fast: false" in text
    assert "-c constraints.txt" in text and 'RUN_MCP_TESTS: "1"' in text and "python -m pytest" in text
    assert "\t" not in text                                                          # YAML does not allow tabs for indentation
    for key in ("name:", "on:", "permissions:", "jobs:", "runs-on:", "steps:", "uses: actions/checkout@v4", "astral-sh/setup-uv"):
        assert key in text, key
