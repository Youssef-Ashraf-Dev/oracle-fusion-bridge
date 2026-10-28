# Oracle Fusion — Research Assistant (Claude Desktop, Combined FSCM+HCM, Read-Only, UV)

**One `.mcpb`** that gives Claude Desktop:

```
Claude Desktop
  ↓ .mcpb (this dir → oracle-fusion-claude-plugin.mcpb)
  ↓ server.py — single FastMCP "oracle-fusion-combined" (UV, 11.13.18.05)
  ├─ FSCM  oracle_fusion_mcp 0.2.1  — 35 read tools (fusion_* generic + procurement/GL/AR/math)
  └─ HCM   aj_fusion_hcm_mcp 0.1.1 — 14 read tools (list_resources/describe_resource/query_resource + worker/reporting)
  ↓
Oracle Fusion REST APIs (fscmRestApi + hcmRestApi, GET-only)
  +
skills/fusion-research-planner/SKILL.md  (read-only research workflow)
```

- **49 read tools** (35 FSCM + 14 HCM, 0 write). `MCP_MODE=readonly` in manifest + `server.py` belt; HCM `writes_register` is never called — `mutate_record`/`run_action` and FSCM `create_*` are absent from `tools/list`.
- **Single config** — `oracle_fusion_base_url` / `oracle_username` / `oracle_password` shared for both stacks (manifest maps `HCM_*` = `ORACLE_*`), `oracle_fusion_api_version` default `11.13.18.05`, password `sensitive`.
- **UV runtime** (`manifest_version 0.4`, `server.type uv`, `pyproject.toml`) — Claude Desktop provisions `python >=3.11` + deps via `uv` automatically; no host `pip install` needed if Claude supports 0.4 uv. Falls back to `python` command on older Claude.
- **Skill** — packaged copy of `fusion-research-planner` (14 sections: resource/schema/queryability/relationship discovery, safe inference, GET-only planning, validation, FSCM↔HCM curated maps). Originals at `~/.config/opencode/skills/...` and `~/.opencode/...` remain untouched.

## Contents (this isolated directory — ONLY place that is written)

- `manifest.json` — 0.4 uv combined, shared user_config
- `pyproject.toml` — union deps of both MCPs (mcp, httpx 0.28.1, pydantic 2.13, truststore, etc.)
- `server.py` — combined FastMCP (vendor-first, readonly, truststore, patch FSCM schemas)
- `oracle_fusion_mcp/` — vendored FSCM (33 .py, not modified)
- `aj_fusion_hcm_mcp/` — vendored HCM (core, auth, tools, not modified)
- `skills/fusion-research-planner/SKILL.md` — packaged skill (Claude-compatible, folder name matches `name`)
- `.mcpbignore` / `README.md`

## Prerequisites (manager machine)

- Claude Desktop with Extensions support; for `uv` auto-provision, Claude Desktop that implements MCPB 0.4 `uv` (experimental). Otherwise fallback to `python >=3.11` + `pip install -r` (see below).
- Network to `*.fa.ocs.oraclecloud.com` for live Fusion pod (GET only).

If Claude does not yet support `uv`, host fallback:

```powershell
pip install mcp httpx pydantic pydantic-settings python-dotenv python-jose cryptography truststore
# or: pip install mcp-oraclefusion fusion-hcm-mcp-server
```

## Install (DO NOT YET — per task, review first)

```powershell
cd C:\Users\Adelrahman\oracle-fusion-claude-plugin
npx --yes @anthropic-ai/mcpb validate manifest.json
npx --yes @anthropic-ai/mcpb pack . oracle-fusion-claude-plugin.mcpb
# Claude Desktop → Settings → Extensions → Install Extension… → select .mcpb
# Fill 4 prompts: Base URL, Username, Password (sensitive), API 11.13.18.05
```

Skill: Claude Desktop → Settings → Capabilities → Upload Skill → select `skills/fusion-research-planner.zip` (created via `Compress-Archive` with forward-slash fix or Node archiver). Alternatively, the skill is already inside the `.mcpb` as `skills/.../SKILL.md` for reference.

## Read-only verification (local, no destructive Fusion calls)

```powershell
$env:ORACLE_FUSION_BASE_URL="https://dummy.example.com"
$env:ORACLE_USERNAME="test@example.com"
$env:ORACLE_PASSWORD="dummy"
python server.py
# feed: {"jsonrpc":"2.0","id":1,"method":"initialize"} → {"protocolVersion":"2025-06-18", ...}
# feed: {"jsonrpc":"2.0","id":2,"method":"tools/list"} → 49 tools, no create/mutate/run_action
```

Only `GET`/describe/metadata (`fusion_list_resources`, `describe_resource`, `test_oracle_connection` with `limit=1`) are safe against a live pod.

## Validation performed (see final report)

- `py_compile` + imports + `mcp.list_tools()` → 49
- `mcpb validate` 0.4 → pass
- `mcpb pack` → ~80-90kB, `unpack` → 35+15+skill files, no credentials, no writes
- `__pycache__` excluded, originals hash-verified unchanged

## Limitations

- `uv` is experimental; older Claude may ignore `type uv` and use `python` fallback → still needs host Python.
- Skill inside `.mcpb` is not auto-loaded as a Claude skill by `.mcpb` install — upload the `skills/...` ZIP separately if auto-load fails.
- No `POST`/`PATCH`/`DELETE` — research only; use Oracle UI for writes.
