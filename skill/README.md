# The planner skill

`ris-fusion-gateway-skill/SKILL.md` is the skill that accompanies the RIS Claude Gateway MCP in Claude chat.
It is copied here from the team's `ris-fusion-gateway-skill` plugin, unchanged.

The `.mcpb` as received contains no skill: its README describes an older bundle (49 read tools and a skill named
`fusion-research-planner`), and its manifest describes the current one (59 tools). Confirm with whoever uses the MCP
in chat that this is the skill they load, and whether they add project instructions on top.

The bridge loads it the way chat does: as a folder under `.claude/skills/` that the model opens with the Skill tool
(SKILL_MODE=native). SKILL_MODE=inline pastes it into the system prompt instead; SKILL_MODE=none leaves it out.
