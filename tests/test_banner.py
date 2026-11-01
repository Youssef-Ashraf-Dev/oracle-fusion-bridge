from pathlib import Path

from bridge.banner import startup_lines
from bridge.config import Settings
from bridge.live.skills import skill_name


def test_the_startup_banner_states_versions_mode_and_limits_but_no_secret(tmp_path):
    s = Settings(oracle_base_url="https://iaehkf-dev1.fa.ocs.oraclecloud.com", oracle_user="svc", oracle_password="Sup3r-Secret-pw",
                 anthropic_api_key="sk-ant-api03-verysecretkeyvalue", telegram_token="1234567890:AAFake-Token_For_Tests_Only_0123456789ab",
                 planner_skill=Path("skill/ris-fusion-gateway-skill/SKILL.md"), recipes_path=Path("skill/recipes.md"), log_dir=tmp_path)
    text = "\n".join(startup_lines(s, 2))
    for needle in ("oracle-fusion-bridge 0.2.0", "claude-agent-sdk", "mode: live", "iaehkf-dev1.fa.ocs.oraclecloud.com", "claude-haiku-5-5",
                   f"skill: native ({skill_name(Path('skill/ris-fusion-gateway-skill/SKILL.md'))})", "recipes:", "$0.60 per run", "30 data calls", "users: 2 registered", "bridge.log"):
        assert needle in text, needle
    for secret in ("Sup3r-Secret-pw", "sk-ant-api03", "AAFake-Token", "svc"):
        assert secret not in text
    demo = "\n".join(startup_lines(Settings(demo_mode=True), 1))
    assert "DEMO (invented answers)" in demo and "skill: none" in demo
