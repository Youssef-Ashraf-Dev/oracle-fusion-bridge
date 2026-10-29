import sqlite3
import time

from bridge.auditview import mask, render, rows
from bridge.state.db import Database


def test_mask_hides_ids_and_literals_but_keeps_the_shape():
    m = mask("mcp__fusion__query_resource {\"resource\": \"workers\", \"q\": \"PersonNumber = 300000008800424 and LastName like 'Sm%'\", \"limit\": 10}")
    assert "300000008800424" not in m and "Sm%" not in m
    assert "query_resource" in m and "workers" in m and "limit" in m and "#####" in m


async def test_render_lists_recent_events_in_order_and_ignores_others(tmp_path):
    db = await Database(tmp_path / "a.db").open()
    await db.audit(1, "data_call", "mcp__fusion__search_invoices {\"limit\": 10}")
    await db.audit(1, "tool_denied", "mcp__fusion__query_resource {} :: limit 5000 is too large")
    await db.audit(1, "rejected_user", "ask")                                      # not shown
    await db.audit(1, "ask_finished", "ok cost=0.0100 turns=8 data_calls=2")
    await db.close()
    r = rows(tmp_path / "a.db", 10)
    assert [e for _, e, _ in r] == ["data_call", "tool_denied", "ask_finished"]
    text = render(tmp_path / "a.db", 10)
    assert "search_invoices" in text and "too large" in text and "rejected_user" not in text
    empty = await Database(tmp_path / "empty.db").open()
    await empty.close()
    assert render(tmp_path / "empty.db", 5) == "No audit rows yet."
