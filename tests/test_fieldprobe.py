import asyncio
from pathlib import Path

import pytest

from bridge.fieldprobe import field_map_keys, probe_fields
from bridge.mcp_util import ToolError

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "ris-claude-gateway" / "oracle_fusion_mcp" / "oracle" / "field_maps.py"


def fake_call(bad):
    async def call(name, args):
        assert name == "fusion_query_resource" and args["limit"] == 1
        used = args["fields"].split(",")
        hit = [f for f in used if f in bad]
        if hit:
            raise ToolError(f"Error executing tool: Client error '400 Bad Request' for url 'https://pod.example.com/fscmRestApi/resources/11.13.18.05/{args['resource']}?fields={args['fields']}' For more information check: https://x")
        return {"items": [{}]}
    return call


def test_one_bad_field_among_good_ones_is_found():
    fields = ["LedgerName", "LedgerType", "AccountingCalendar", "CurrencyCode"]
    r = asyncio.run(probe_fields(fake_call({"AccountingCalendar"}), "ledgersLOV", fields))
    assert r["bad"] == ["AccountingCalendar"] and r["good"] == ["LedgerName", "LedgerType", "CurrencyCode"]
    assert r["together_ok"] is False and "400 Bad Request" in r["together_error"] and "mozilla" not in r["together_error"]
    assert dict((f, ok) for f, ok, _ in r["fields"])["AccountingCalendar"] is False


def test_all_good_fields_report_ok_together():
    r = asyncio.run(probe_fields(fake_call(set()), "ledgersLOV", ["A", "B"]))
    assert r["bad"] == [] and r["together_ok"] is True and r["good"] == ["A", "B"]


def test_each_failure_is_shown_without_the_host_or_boilerplate():
    r = asyncio.run(probe_fields(fake_call({"B"}), "ledgersLOV", ["A", "B"]))
    err = next(e for f, ok, e in r["fields"] if f == "B")
    assert "pod.example.com" not in err and "For more information" not in err and "\u2026/ledgersLOV?fields=B" in err


def test_field_map_keys_are_read_from_a_python_file_without_importing_it(tmp_path):
    f = tmp_path / "maps.py"
    f.write_text('import os_that_does_not_exist\nX = 1\nA_FIELD_MAP = {"One": "one", "Two": "two"}\n')
    assert field_map_keys(f, "A_FIELD_MAP") == ["One", "Two"]
    with pytest.raises(KeyError):
        field_map_keys(f, "NOPE")


@pytest.mark.skipif(not VENDOR.is_file(), reason="the vendored MCP is not present")
def test_the_ledger_fields_in_the_failing_request_are_the_mcps_own_defaults():
    """The S8 error listed exactly these fields: the 400 comes from the MCP's list_ledgers tool, not from a model's guess."""
    assert field_map_keys(VENDOR, "LEDGER_FIELD_MAP") == ["LedgerName", "LedgerShortName", "LedgerType", "CurrencyCode",
                                                          "ChartOfAccountsStructureId", "AccountingCalendar", "BudgetaryControlEnabledFlag"]
