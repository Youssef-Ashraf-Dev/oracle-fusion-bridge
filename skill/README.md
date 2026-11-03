# The planner skill

`ris-fusion-gateway-skill/SKILL.md` is the skill that accompanies the RIS Claude Gateway MCP in Claude chat.

The file here is the one sent by the colleague who uses the MCP in chat. It replaced the copy first taken from the team's
`ris-fusion-gateway-skill` plugin. Its front matter says `name: RIS_Fusion_Gateway-Skill`, with the same description
as the shipped copy. The bridge reads the name from the file, so the folder name here does not matter.

The `.mcpb` as received contains no skill: its README describes an older bundle (49 read tools and a skill named
`fusion-research-planner`), and its manifest describes the current one (59 tools).

The bridge loads it the way chat does: as a folder under `.claude/skills/` that the model opens with the Skill tool
(SKILL_MODE=native). SKILL_MODE=inline pastes it into the system prompt instead; SKILL_MODE=none leaves it out.
After changing the skill file, run `python scripts/skill_check.py`.
