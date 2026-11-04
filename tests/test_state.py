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
                 'ORACLE_PASSWORD=pw\nALREADY=from-file\nBAD LINE\n', encoding="utf-8")
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
    f.write_text('---\nname: "ris-fusion-gateway-skill"\ndescription: x\n---\n# Body\nline', encoding="utf-8")
    assert skill_name(f) == "ris-fusion-gateway-skill" and skill_body(f).startswith("# Body")
    g = tmp_path / "other" / "SKILL.md"
    g.parent.mkdir()
    g.write_text("# no front matter", encoding="utf-8")
    assert skill_name(g) == "other"                                           # falls back to the folder name
    rt = prepare_runtime(f, tmp_path / "rt")
    dest = rt / ".claude" / "skills" / "ris-fusion-gateway-skill" / "SKILL.md"
    assert dest.read_bytes() == f.read_bytes()
    f.write_text(f.read_text(encoding="utf-8") + "\nmore", encoding="utf-8")
    prepare_runtime(f, tmp_path / "rt")                                       # a changed skill is refreshed
    assert dest.read_text(encoding="utf-8").endswith("more")


def test_skill_mode_falls_back_to_none_without_a_file(tmp_path):
    from bridge.config import Settings
    assert Settings(skill_mode="native").effective_skill_mode == "none"
    f = tmp_path / "SKILL.md"; f.write_text("x", encoding="utf-8")
    assert Settings(skill_mode="native", planner_skill=f).effective_skill_mode == "native"
    assert Settings(skill_mode="none", planner_skill=f).effective_skill_mode == "none"


def test_origin_duplicates_and_shell_overrides_are_reported(tmp_path, monkeypatch):
    import bridge.config as C
    f = tmp_path / ".env"
    f.write_text("ORACLE_FUSION_BASE_URL=https://YOUR-NONPROD-POD.fa.ocs.oraclecloud.com\nORACLE_USERNAME=svc\n"
                 "ORACLE_FUSION_BASE_URL=https://real-dev1.fa.ocs.oraclecloud.com\nMODEL=claude-haiku-5-5\n", encoding="utf-8")
    for k in ("ORACLE_FUSION_BASE_URL", "ORACLE_USERNAME", "MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(C, "_FILE_KEYS", {})
    C.load_env_file(f)
    import os
    assert os.environ["ORACLE_FUSION_BASE_URL"].startswith("https://YOUR-NONPROD")      # the first line wins
    assert C.origin("ORACLE_FUSION_BASE_URL") == ".env line 1" and C.origin("MODEL") == ".env line 4"
    assert C.origin("NOT_A_SETTING_XYZ") == "not set"
    problems = C.env_conflicts(f, ("ORACLE_FUSION_BASE_URL",))
    assert len(problems) == 1 and "lines 1, 3" in problems[0] and "first one wins" in problems[0]
    assert C.env_conflicts(f, ("ORACLE_USERNAME",)) == []
    # a shell variable that differs from the file
    g = tmp_path / "g.env"
    g.write_text("ORACLE_USERNAME=from-file\n", encoding="utf-8")
    monkeypatch.setattr(C, "_FILE_KEYS", {})
    monkeypatch.setenv("ORACLE_USERNAME", "from-shell")
    C.load_env_file(g)
    assert C.origin("ORACLE_USERNAME").startswith("your shell environment")
    assert any("shell wins" in x for x in C.env_conflicts(g, ("ORACLE_USERNAME",)))
    assert C.env_conflicts(tmp_path / "missing.env") == []


def test_the_session_settings_have_sensible_defaults_and_come_from_the_environment(monkeypatch, tmp_path):
    from pathlib import Path
    from bridge.config import Settings, load_settings
    d = Settings()
    assert d.session_idle_minutes == 120 and str(d.engine_dir).replace("\\", "/").endswith("data/engine")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "45")
    monkeypatch.setenv("ENGINE_DIR", str(tmp_path / "engine"))
    s = load_settings(env_file=None)
    assert s.session_idle_minutes == 45 and Path(s.engine_dir) == tmp_path / "engine"


def test_origin_says_whether_the_shell_value_hides_the_file(tmp_path, monkeypatch):
    import bridge.config as C
    f = tmp_path / ".env"
    f.write_text("ORACLE_USERNAME=svc\nORACLE_PASSWORD=pw\n", encoding="utf-8")
    monkeypatch.setattr(C, "_FILE_KEYS", {})
    monkeypatch.setenv("ORACLE_USERNAME", "svc")                      # the shell already holds the same value
    monkeypatch.setenv("ORACLE_PASSWORD", "other")                    # ... and a different one here
    monkeypatch.setenv("ONLY_IN_SHELL", "x")
    assert "same value as .env, so nothing is overridden" in C.origin("ORACLE_USERNAME", f)
    assert "OVERRIDES the different value" in C.origin("ORACLE_PASSWORD", f)
    assert "does not set it" in C.origin("ONLY_IN_SHELL", f)
    assert C.origin("NOT_ANYWHERE_XYZ", f) == "not set"


def test_the_recipes_file_is_found_by_default_and_by_environment(tmp_path, monkeypatch):
    import bridge.config as C
    from bridge.config import load_settings
    monkeypatch.setattr(C, "ROOT", tmp_path)
    monkeypatch.delenv("RECIPES_PATH", raising=False)
    assert load_settings(env_file=None).recipes_path is None                      # no file, no recipes
    (tmp_path / "skill").mkdir()
    (tmp_path / "skill" / "recipes.md").write_text("- a note", encoding="utf-8")
    assert str(load_settings(env_file=None).recipes_path).replace("\\", "/").endswith("skill/recipes.md")
    monkeypatch.setenv("RECIPES_PATH", str(tmp_path / "other.md"))
    assert load_settings(env_file=None).recipes_path == tmp_path / "other.md"


def test_every_path_belongs_to_the_project_not_to_the_folder_the_bot_was_started_from(tmp_path, monkeypatch):
    """Started from C:\\Windows\\System32 by a scheduler, or from a parent folder: the same db, .env and skill are used."""
    import bridge.config as C
    elsewhere = tmp_path / "somewhere" / "else"
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    for k in ("DB_PATH", "MCP_DIR", "RUNTIME_DIR", "ENGINE_DIR", "LOG_DIR", "PLANNER_SKILL_PATH", "RECIPES_PATH"):
        monkeypatch.delenv(k, raising=False)
    s = C.load_settings(env_file=None)
    for name in ("db_path", "mcp_dir", "runtime_dir", "engine_dir", "log_dir", "planner_skill", "recipes_path"):
        p = getattr(s, name)
        assert p is not None and p.is_absolute() and str(p).startswith(str(C.ROOT)), (name, p)
    assert s.planner_skill.is_file() and s.recipes_path.is_file()
    (C.ROOT / ".env.test-marker").write_text("TEST_MARKER_VALUE=from-project-root\n", encoding="utf-8")
    try:
        monkeypatch.delenv("TEST_MARKER_VALUE", raising=False)
        C.load_settings(env_file=".env.test-marker")                # a relative env file is looked up in the project folder
        import os
        assert os.environ["TEST_MARKER_VALUE"] == "from-project-root"
    finally:
        (C.ROOT / ".env.test-marker").unlink()
        os.environ.pop("TEST_MARKER_VALUE", None)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "elsewhere" / "bridge.db"))
    assert C.load_settings(env_file=None).db_path == tmp_path / "elsewhere" / "bridge.db"       # absolute paths are kept as given


def test_logging_survives_a_console_that_cannot_print_arabic(tmp_path):
    import logging
    from bridge.logsafe import configure_logging
    root = logging.getLogger()
    before = list(root.handlers)
    for h in before:
        root.removeHandler(h)
    try:
        configure_logging(logging.INFO, tmp_path / "logs")
        logging.getLogger("bridge").info("question about \u062f\u0641\u0627\u062a\u0631 \u0627\u0644\u0623\u0633\u062a\u0627\u0630 \U0001F600")
        for h in root.handlers:
            h.flush()
        assert "\u062f\u0641\u0627\u062a\u0631" in (tmp_path / "logs" / "bridge.log").read_text(encoding="utf-8")
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            h.close()
        for h in before:
            root.addHandler(h)


def test_a_printout_of_the_settings_never_shows_a_secret():
    """A failing test, a traceback or a debug print shows the settings object. It leaked three secrets once."""
    import dataclasses
    from bridge.config import Settings
    s = Settings(anthropic_api_key="sk-ant-usr-VERYSECRETKEYVALUE", telegram_token="1234567890:AAF-SecretTokenValueSecretTokenValue",
                 oracle_user="Some.Integration", oracle_password="Sup3r@Secret2025", oracle_base_url="https://pod.example.com")
    shown = repr(s) + str(s) + repr(dataclasses.replace(s, model="m")) + repr([s]) + f"{s!r}"
    for secret in ("sk-ant-usr", "VERYSECRET", "AAF-Secret", "Sup3r@Secret2025", "Some.Integration"):
        assert secret not in shown, secret
    assert "pod.example.com" in shown and "model=" in shown                    # the harmless settings are still visible
    assert s.oracle_password == "Sup3r@Secret2025"                              # and the values still work
    from bridge.live.session import RunContext
    assert "Sup3r@Secret2025" not in repr(RunContext(s, None, 1, None))        # nor through an object that holds the settings


def test_a_renamed_skill_does_not_leave_its_old_folder_behind(tmp_path):
    from bridge.live.skills import prepare_runtime, skill_name
    skills = tmp_path / "rt" / ".claude" / "skills"
    old = skills / "RIS_Fusion_Gateway-Skill"
    old.mkdir(parents=True)
    (old / "SKILL.md").write_text("OLD CONTENT", encoding="utf-8")
    (skills / "stray-file.txt").write_text("not a folder", encoding="utf-8")
    f = tmp_path / "SKILL.md"
    f.write_text('---\nname: ris-fusion-gateway-skill\ndescription: d\n---\nNEW CONTENT', encoding="utf-8")
    prepare_runtime(f, tmp_path / "rt")
    assert sorted(p.name for p in skills.iterdir() if p.is_dir()) == ["ris-fusion-gateway-skill"]
    assert (skills / "ris-fusion-gateway-skill" / "SKILL.md").read_text(encoding="utf-8").endswith("NEW CONTENT")
    assert (skills / "stray-file.txt").exists()                                  # only folders are cleaned
    prepare_runtime(f, tmp_path / "rt")                                          # running it twice changes nothing
    assert sorted(p.name for p in skills.iterdir() if p.is_dir()) == ["ris-fusion-gateway-skill"]


def test_the_skill_name_convention():
    from bridge.live.skills import valid_skill_name
    assert valid_skill_name("ris-fusion-gateway-skill") and valid_skill_name("planner2")
    for bad in ("RIS_Fusion_Gateway-Skill", "ris_fusion_gateway-skill", "Ris-Fusion", "-lead", "trail-", "two--hyphens", "a" * 65, ""):
        assert not valid_skill_name(bad), bad


def test_the_new_limits_and_cost_settings_have_defaults_and_come_from_the_environment(monkeypatch):
    from bridge.config import Settings, load_settings
    d = Settings()
    assert (d.user_daily_runs, d.burst_runs, d.burst_minutes, d.show_cost, d.max_mcp_output_tokens) == (100, 20, 10, "all", 50000)
    assert d.shutdown_grace_s == 3.0
    for k, v in (("USER_DAILY_RUNS", "0"), ("USER_BURST_RUNS", "5"), ("SHOW_COST", "Admin"), ("ADMIN_TELEGRAM_USER_IDS", "11, 22,abc,33")):
        monkeypatch.setenv(k, v)
    s = load_settings(env_file=None)
    assert (s.user_daily_runs, s.burst_runs, s.show_cost) == (0, 5, "admin") and s.admin_ids == frozenset({11, 22, 33})
