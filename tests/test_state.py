import pytest

from bridge.state.db import Database


async def test_registry_and_state(tmp_path):
    db = await Database(tmp_path / "t.db").open()
    assert not await db.is_allowed(5)                         # FR-01
    await db.add_user(5, "sam")
    assert await db.is_allowed(5)
    await db.set_live_session(9, "sess-1")
    assert (await db.get_chat(9))["live_session_id"] == "sess-1"
    await db.close()
    db2 = await Database(tmp_path / "t.db").open()            # NFR-08: survives restart
    assert (await db2.get_chat(9))["live_session_id"] == "sess-1"
    await db2.reset_chat(9)
    assert (await db2.get_chat(9))["live_session_id"] is None
    await db2.record_run(5, "ask", "ok", 0.2, 3.0)
    assert await db2.runs_today(5) == 1 and await db2.spend_since(0) == pytest.approx(0.2)
    await db2.close()


async def test_plans_and_audit_recorded_and_purged(tmp_path):
    db = await Database(tmp_path / "t.db").open()
    await db.record_plan(5, {"summary": "x", "steps": []}, "approved", 5)
    await db.audit(5, "data_call", "mcp__fusion__search_invoices {}")
    rows = await (await db.db.execute("SELECT event FROM audit")).fetchall()
    assert [r["event"] for r in rows] == ["data_call"]
    await db.db.execute("UPDATE audit SET ts=0"); await db.db.execute("UPDATE plans SET ts=0"); await db.db.commit()
    await db.purge_old(90)                                    # NFR-09: 90-day retention
    assert (await (await db.db.execute("SELECT COUNT(*) c FROM audit")).fetchone())["c"] == 0
    assert (await (await db.db.execute("SELECT COUNT(*) c FROM plans")).fetchone())["c"] == 0
    await db.close()


def test_env_file_loader(tmp_path, monkeypatch):
    from bridge.config import load_env_file, load_settings
    f = tmp_path / ".env"
    f.write_text('# comment\n\nANTHROPIC_API_KEY=sk-test\nORACLE_USERNAME="svc view"\nORACLE_FUSION_BASE_URL=https://np.example.com  # the pod\n'
                 'ORACLE_PASSWORD=pw\nALREADY=from-file\nBAD LINE\n')
    for k in ("ANTHROPIC_API_KEY", "ORACLE_USERNAME", "ORACLE_FUSION_BASE_URL", "ORACLE_PASSWORD", "ALREADY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALREADY", "from-shell")
    assert load_env_file(f) == 4
    import os
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-test" and os.environ["ORACLE_USERNAME"] == "svc view"
    assert os.environ["ORACLE_FUSION_BASE_URL"] == "https://np.example.com" and os.environ["ALREADY"] == "from-shell"
    assert load_env_file(tmp_path / "missing") == 0
    s = load_settings(f)
    assert s.anthropic_api_key == "sk-test" and s.live_ready


def test_skill_name_and_runtime_folder(tmp_path):
    from bridge.live.skills import prepare_runtime, skill_body, skill_name
    f = tmp_path / "somefolder" / "SKILL.md"
    f.parent.mkdir()
    f.write_text('---\nname: "ris-fusion-gateway-skill"\ndescription: x\n---\n# Body\nline')
    assert skill_name(f) == "ris-fusion-gateway-skill" and skill_body(f).startswith("# Body")
    g = tmp_path / "other" / "SKILL.md"
    g.parent.mkdir()
    g.write_text("# no front matter")
    assert skill_name(g) == "other"                                           # falls back to the folder name
    rt = prepare_runtime(f, tmp_path / "rt")
    dest = rt / ".claude" / "skills" / "ris-fusion-gateway-skill" / "SKILL.md"
    assert dest.read_bytes() == f.read_bytes()
    f.write_text(f.read_text() + "\nmore")
    prepare_runtime(f, tmp_path / "rt")                                       # a changed skill is refreshed
    assert dest.read_text().endswith("more")


def test_skill_mode_falls_back_to_none_without_a_file(tmp_path):
    from bridge.config import Settings
    assert Settings(skill_mode="native").effective_skill_mode == "none"
    f = tmp_path / "SKILL.md"; f.write_text("x")
    assert Settings(skill_mode="native", planner_skill=f).effective_skill_mode == "native"
    assert Settings(skill_mode="none", planner_skill=f).effective_skill_mode == "none"
