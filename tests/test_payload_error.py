import json
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ToolResultBlock, UserMessage

from bridge.live import permissions as P
from bridge.mcp_util import payload_error
from bridge.state.db import Database
from tests.test_live import FakeSDK, result, yes
from bridge.live.session import LiveRunner

ERR = "Error executing tool: [400] Filter references unknown attribute(s): HoldFlag"
SINGLE = json.dumps({"error": ERR})
DOUBLE = json.dumps({"content": [{"type": "text", "text": SINGLE}]})
TRIPLE = json.dumps({"content": [{"type": "text", "text": DOUBLE}]})
OK_DOUBLE = json.dumps({"content": [{"type": "text", "text": json.dumps({"items": [1, 2], "count": 2})}]})


@pytest.mark.parametrize("text,expected", [
    (SINGLE, ERR), (DOUBLE, ERR), (TRIPLE, ERR),
    ("Error executing tool query_resource: [404] not found", "Error executing tool query_resource: [404] not found"),
    ("Unknown tool: nope", "Unknown tool: nope"),
    (OK_DOUBLE, None), ('{"items": [], "count": 0}', None), ("", None), (None, None),
    ('{"error": ""}', None),
])
def test_failures_are_found_inside_every_wrapping_and_normal_results_are_not(text, expected):
    assert payload_error(text) == expected


def test_an_escaped_error_key_in_unparseable_text_is_still_caught():
    assert payload_error('garbled {\\"error\\": \\"boom\\"} text') is not None
    assert payload_error("a normal sentence about an error in general") is None


async def test_the_q4_case_a_double_wrapped_400_is_a_failure_not_a_data_call(tmp_path, settings):
    """The report said 'Data calls 1, Tool errors 0' although the query had failed with a 400."""
    async def script(c):
        await c.options.can_use_tool.__self__.propose_query_plan({"summary": "holds", "steps": [{"tool": "fusion_query_resource", "resource": "invoices"}]})
        await c.options.can_use_tool(P.PREFIX + "fusion_query_resource", {"resource": "invoices", "q": "HoldFlag = 'Y'"}, SimpleNamespace(tool_use_id="q1"))
        yield UserMessage(content=[ToolResultBlock("q1", [{"type": "text", "text": DOUBLE}], False)])    # isError false, error inside
        yield result(0.005, text="I could not count the holds.")
    db = await Database(tmp_path / "t.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(7, "holds?")
    assert res.data_calls == 0 and res.data_attempts == 1
    assert len(res.tool_errors) == 1 and "HoldFlag" in res.tool_errors[0]
    await db.close()


def test_long_http_errors_keep_the_failing_query_and_lose_the_boilerplate():
    from bridge.mcp_util import shorten_error
    raw = ("Error executing tool: Client error '400 Bad Request' for url "
           "'https://iaehkf-dev1.fa.ocs.oraclecloud.com/fscmRestApi/resources/11.13.18.05/ledgersLOV?limit=50&onlyData=true&fields=LedgerName&q=Foo=1' "
           "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/400")
    out = shorten_error(raw)
    assert "iaehkf-dev1" not in out and "11.13.18.05" not in out and "For more information" not in out and "mozilla" not in out
    assert out.startswith("Error executing tool: Client error '400 Bad Request' for url '\u2026/ledgersLOV?limit=50&onlyData=true&fields=LedgerName&q=Foo=1'")
    hr = "Client error '404 Not Found' for url 'https://pod.example.com/hcmRestApi/resources/11.13.18.05/workers/123/child/x'"
    assert shorten_error(hr) == "Client error '404 Not Found' for url '\u2026/workers/123/child/x'"
    assert shorten_error("plain text error") == "plain text error" and shorten_error("") == "" and shorten_error(None) == ""
    assert len(shorten_error("x" * 2000)) == 400 and len(shorten_error("x" * 2000, 50)) == 50


async def test_the_stored_tool_error_is_the_shortened_one(tmp_path, settings):
    long = ("Error executing tool: Client error '400 Bad Request' for url "
            "'https://pod.example.com/fscmRestApi/resources/11.13.18.05/ledgersLOV?limit=50&fields=Bad' For more information check: https://x.y/z")
    async def script(c):
        await c.options.can_use_tool.__self__.propose_query_plan({"summary": "x", "steps": [{"tool": "fusion_query_resource", "resource": "ledgersLOV"}]})
        await c.options.can_use_tool(P.PREFIX + "fusion_query_resource", {"resource": "ledgersLOV"}, SimpleNamespace(tool_use_id="e1"))
        yield UserMessage(content=[ToolResultBlock("e1", [{"type": "text", "text": long}], True)])
        yield result(0.004, text="It failed.")
    db = await Database(tmp_path / "t.db").open()
    res = await LiveRunner(settings, db, yes, lambda o: FakeSDK(o, script), tick_s=0.02).run(1, "q")
    assert res.tool_errors == ["Error executing tool: Client error '400 Bad Request' for url '\u2026/ledgersLOV?limit=50&fields=Bad'"]
    await db.close()
