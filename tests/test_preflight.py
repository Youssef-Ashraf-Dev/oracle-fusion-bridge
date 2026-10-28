import dataclasses
import json
import os
import shutil
from pathlib import Path

import pytest

from bridge.config import Settings
from bridge.mcp_util import ToolError, unwrap
from bridge.preflight import FAIL, PASS, WARN, check_anthropic, check_config, check_mcp, check_runtime, format_checks

GOOD = Settings(anthropic_api_key="k", oracle_base_url="https://np-pod.example.com", oracle_user="u", oracle_password="p",
                oracle_allowed_base_urls="np-pod.example.com")


def by(checks):
    return {c.name: c for c in checks}


def test_config_passes_when_complete_and_flags_the_pod_for_a_human():
    c = by(check_config(GOOD))
    assert c["Required settings present"].status == PASS
    assert c["Pod host (must be NON-PRODUCTION)"].status == WARN and "np-pod.example.com" in c["Pod host (must be NON-PRODUCTION)"].detail
    assert c["Pod guard ORACLE_ALLOWED_BASE_URLS"].status == PASS


@pytest.mark.parametrize("change,name,status", [
    (dict(oracle_password=""), "Required settings present", FAIL),
    (dict(oracle_allowed_base_urls="other-host"), "Pod guard ORACLE_ALLOWED_BASE_URLS", FAIL),
    (dict(oracle_allowed_base_urls=""), "Pod guard ORACLE_ALLOWED_BASE_URLS", WARN),
    (dict(mcp_environment="prod"), "MCP_ENVIRONMENT", FAIL),
    (dict(mcp_prod_writes_enabled=True), "MCP_PROD_WRITES_ENABLED is off", FAIL),
])
def test_config_failures(change, name, status):
    assert by(check_config(dataclasses.replace(GOOD, **change)))[name].status == status


def test_placeholder_pod_url_is_a_failure_with_a_plain_explanation():
    c = by(check_config(dataclasses.replace(GOOD, oracle_base_url="https://YOUR-NONPROD-POD.fa.ocs.oraclecloud.com")))
    assert c["Pod URL is still the placeholder"].status == FAIL and ".env.example" in c["Pod URL is still the placeholder"].detail
    assert "Pod URL is still the placeholder" not in by(check_config(GOOD))
    assert "from " in by(check_config(GOOD))["Pod host (must be NON-PRODUCTION)"].detail           # the source is shown


def test_runtime_checks_need_uv():
    assert by(check_runtime(lambda n: None))["uv on PATH (launches the MCP)"].status == FAIL
    assert by(check_runtime(lambda n: "/bin/uv"))["uv on PATH (launches the MCP)"].status == PASS


def models(*ids):
    return (200, json.dumps({"data": [{"id": i} for i in ids]}))


def test_anthropic_key_and_model_checks():
    m = GOOD.model
    name = f"Model {m} is available"
    ok = by(check_anthropic(GOOD, lambda u, h: models(m, "claude-haiku-5-0")))
    assert ok["Anthropic API key"].status == PASS and ok[name].status == PASS
    missing = by(check_anthropic(GOOD, lambda u, h: models("some-other-model")))
    assert missing[name].status == FAIL and "some-other-model" in missing[name].detail
    bad = check_anthropic(GOOD, lambda u, h: (401, '{"error":"invalid x-api-key"}'))
    assert bad[0].status == FAIL and "rejected" in bad[0].detail
    net = check_anthropic(GOOD, lambda u, h: (0, "ConnectError"))
    assert net[0].status == FAIL and "network" in net[0].detail
    assert check_anthropic(dataclasses.replace(GOOD, anthropic_api_key=""), lambda u, h: models())[0].status == FAIL
    seen = {}
    check_anthropic(GOOD, lambda u, h: seen.update(h=h) or models(m))
    assert seen["h"]["x-api-key"] == "k"                                       # the key is sent as a header, never in the URL


def test_format_reports_ready_or_not():
    from bridge.preflight import Check
    assert "READY for the Day 1 spike" in format_checks([Check("a", PASS), Check("b", WARN, "look")])
    assert "NOT READY: 1" in format_checks([Check("a", FAIL, "why")])


def test_unwrap_handles_both_wrappers_and_hidden_failures():
    inner = {"result": "2.50"}
    wrapped = {"content": [{"text": json.dumps({"content": [{"text": json.dumps(inner)}]})}], "isError": False}
    assert unwrap(wrapped) == inner
    assert unwrap({"content": [{"text": json.dumps({"a": 1})}], "isError": False}) == {"a": 1}
    with pytest.raises(ToolError):
        unwrap({"content": [{"text": json.dumps({"error": "Name or service not known"})}], "isError": False})
    with pytest.raises(ToolError):
        unwrap({"content": [{"text": "Unknown tool: x"}], "isError": True})


@pytest.mark.skipif(not os.environ.get("RUN_MCP_TESTS") or shutil.which("uv") is None,
                    reason="set RUN_MCP_TESTS=1 (needs uv and network on first run) to launch the real bundle")
async def test_preflight_against_the_real_bundle_with_an_unreachable_pod():
    s = dataclasses.replace(GOOD, oracle_base_url="https://np-pod.example.invalid", oracle_allowed_base_urls="np-pod.example.invalid",
                            mcp_dir=Path(__file__).resolve().parents[1] / "vendor" / "ris-claude-gateway")
    c = by(await check_mcp(s))
    assert c["MCP starts and lists its tools"].status == PASS and "59 tools" in c["MCP starts and lists its tools"].detail
    assert c["server_info"].status == PASS and "writes_enabled=False" in c["server_info"].detail
    assert c["Fusion login and pod reachable (test_oracle_connection)"].status == FAIL      # the pod does not exist


@pytest.mark.skipif(not os.environ.get("RUN_MCP_TESTS") or shutil.which("uv") is None, reason="needs the real bundle")
async def test_a_bad_allowlist_stops_the_mcp_and_preflight_says_so():
    s = dataclasses.replace(GOOD, oracle_base_url="https://np-pod.example.invalid", oracle_allowed_base_urls="somewhere-else",
                            mcp_dir=Path(__file__).resolve().parents[1] / "vendor" / "ris-claude-gateway")
    c = by(await check_mcp(s))
    assert c["MCP starts and lists its tools"].status == FAIL


@pytest.mark.skipif(not os.environ.get("RUN_MCP_TESTS") or shutil.which("uv") is None, reason="needs the real bundle and CLI")
@pytest.mark.parametrize("mode,expect_skill_tool", [("native", True), ("inline", False), ("none", False)])
async def test_sdk_startup_check_against_the_real_cli_spends_nothing(tmp_path, mode, expect_skill_tool):
    from bridge.preflight import check_sdk
    skill = tmp_path / "demo-skill" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text('---\nname: "demo-skill"\ndescription: "Use for Fusion"\n---\n# Demo')
    s = dataclasses.replace(GOOD, oracle_base_url="https://np-pod.example.invalid", oracle_allowed_base_urls="np-pod.example.invalid",
                            mcp_dir=Path(__file__).resolve().parents[1] / "vendor" / "ris-claude-gateway",
                            planner_skill=skill, skill_mode=mode, runtime_dir=tmp_path / "rt")
    c = by(await check_sdk(s))
    assert c["Agent SDK starts the CLI with the bridge's options"].status == PASS
    assert c["Both MCP servers connect (fusion, bridge)"].status == PASS
    assert c["Fusion tools offered to the model"].detail.startswith("59")
    assert c["No built-in tools except what is intended"].status == PASS
    assert ("Planner skill demo-skill discovered (native, as in chat)" in c) == expect_skill_tool
    if expect_skill_tool:
        assert c["Planner skill demo-skill discovered (native, as in chat)"].status == PASS
