"""The CI guide (docs/CI.md) must describe the workflow (.github/workflows/tests.yml) as it is.

The team asked to be informed of every change to CI. A guide only helps while it is true, so this test reads the facts out of the workflow file
and fails when the guide does not state them. If it fails after a workflow change: update the guide and add a row to its change log.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
DOC = " ".join((ROOT / "docs" / "CI.md").read_text(encoding="utf-8").split())


def fact(pattern: str) -> str:
    m = re.search(pattern, WORKFLOW, re.M)
    assert m, f"the workflow no longer contains: {pattern}"
    return m.group(1)


def test_the_ci_guide_states_the_facts_of_the_workflow_as_they_are():
    systems = [x.strip() for x in fact(r"os: \[(.+?)\]").split(",")]
    assert systems and all("`" + s + "`" in DOC for s in systems)
    python = fact(r'python-version: "([\d.]+)"')
    assert "Python " + python in DOC
    minutes = fact(r"timeout-minutes: (\d+)")
    assert minutes + " minutes per job" in DOC
    assert fact(r"run: (python -m pip install [^\n]+)") in DOC
    assert fact(r"run: (python -m pytest -q)") in DOC
    mcp_flag = fact(r'RUN_MCP_TESTS: "(\d)"')
    assert "RUN_MCP_TESTS=" + mcp_flag in DOC
    assert "fail-fast: false" in WORKFLOW and "`fail-fast: false`" in DOC
    assert "cancel-in-progress: true" in WORKFLOW and "cancels the older run" in DOC
    assert re.search(r"permissions:\s*\n\s+contents: read", WORKFLOW) and "read-only permissions" in DOC
    assert re.search(r"^on:\s*\n\s+push:\s*\n\s+pull_request:", WORKFLOW, re.M) and "Every push" in DOC and "every pull request" in DOC
    for action in re.findall(r"uses: ([\w./-]+@v\d+)", WORKFLOW):
        assert "`" + action + "`" in DOC, action + " is used by the workflow but not described in the guide"


def test_the_guide_keeps_its_rules_its_change_log_and_a_place_in_the_readme():
    for rule in ("What changes, in plain language", "A line-by-line explanation", "What to look for in the Actions tab", "The cost:",
                 "A proof that a new check can fail", "How to undo it", "Real secrets are never put in the workflow"):
        assert rule in DOC, rule
    assert "## Change log" in DOC and "2026-11-03" in DOC and "2026-11-08" in DOC
    assert "anything that needs the API key or the pod stays out of CI" in DOC
    assert "docs/CI.md" in (ROOT / "README.md").read_text(encoding="utf-8")
