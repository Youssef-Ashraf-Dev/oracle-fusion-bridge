"""Query-plan approval and scope enforcement (FR-05).

The agent must call propose_query_plan before any data tool. The user approves
the plan once in Telegram. After that, a data call is allowed only if it matches
an approved plan step, and the total number of data calls is capped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from . import permissions as P

MAX_STEPS = 12
MAX_DATA_CALLS = 30


@dataclass(frozen=True)
class Step:
    tool: str                       # bare tool name, e.g. fusion_query_resource
    resource: Optional[str] = None  # resource name when the tool takes one
    note: str = ""                  # filters / fields, shown to the approver


@dataclass
class Plan:
    summary: str
    steps: list[Step]

    def render(self) -> str:
        lines = [f"Query plan: {self.summary}", ""]
        for i, s in enumerate(self.steps, 1):
            res = f" on {s.resource}" if s.resource else ""
            note = f" ({s.note})" if s.note else ""
            lines.append(f"{i}. {s.tool}{res}{note}")
        return "\n".join(lines)


class PlanError(ValueError):
    pass


def parse_plan(raw: dict[str, Any], allow_sensitive: bool = False) -> Plan:
    """Validate a plan the agent proposed. Raises PlanError with a message the agent can act on."""
    summary = str(raw.get("summary", "")).strip()[:300]
    steps_raw = raw.get("steps")
    if not summary:
        raise PlanError("summary is required")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanError("steps must be a non-empty list")
    if len(steps_raw) > MAX_STEPS:
        raise PlanError(f"at most {MAX_STEPS} steps")
    steps: list[Step] = []
    for i, s in enumerate(steps_raw, 1):
        if not isinstance(s, dict) or "tool" not in s:
            raise PlanError(f"step {i}: needs a tool")
        tool = P.bare(str(s["tool"]))
        verdict = P.classify(P.PREFIX + tool, allow_sensitive)
        if verdict.verdict == "deny":
            raise PlanError(f"step {i}: {verdict.reason}")
        if verdict.verdict == "allow":
            raise PlanError(f"step {i}: {tool} needs no approval, leave it out of the plan")
        res = s.get("resource")
        if tool in P.GENERIC_READERS and not allow_sensitive and P.touches_sensitive_resource({"resource": res}):
            raise PlanError(f"step {i}: {res} holds pay or identity data, which is not available")
        steps.append(Step(tool=tool, resource=_norm(res) if res else None,
                          note=str(s.get("note", ""))[:200]))
    return Plan(summary=summary, steps=steps)


def _norm(resource: Any) -> str:
    """Normalise a resource path. A segment holding a digit or a brace is a record id or a placeholder such as
    {personid}; it becomes * so that workers/{personid}/child/assignments matches workers/300000008800424/child/assignments."""
    parts = [("*" if re.search(r"[\d{}]", p) else p.lower()) for p in str(resource).strip().strip("/").split("/") if p]
    return "/".join(parts)


@dataclass
class PlanState:
    """What has been approved in this run."""
    approved: list[Plan] = field(default_factory=list)
    data_calls: int = 0

    def covers(self, tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
        if not self.approved:
            return False, "no approved plan: call propose_query_plan first"
        if self.data_calls >= MAX_DATA_CALLS:
            return False, f"data call limit ({MAX_DATA_CALLS}) reached for this run"
        name = P.bare(tool_name)
        scoped = name in P.GENERIC_READERS              # only generic readers are limited to a named resource
        res = tool_input.get("resource")
        res = _norm(res) if res else None
        same_tool = [st for plan in self.approved for st in plan.steps if st.tool == name]
        for st in same_tool:
            if not scoped or st.resource is None or st.resource == res:
                return True, ""
        if same_tool:                                   # the tool is in the plan, but for another resource
            allowed = ", ".join(sorted({st.resource or "any resource" for st in same_tool}))
            return False, (f"{name} on {res or 'no resource'} does not match the approved plan, which allows it on "
                           f"{allowed}; propose a new plan that names the resource you need")
        where = f" on {res}" if res else ""
        return False, f"{name}{where} is not in the approved plan; propose a new plan covering it"

    def note_call(self) -> None:
        self.data_calls += 1


Approver = Callable[[Plan], Awaitable[bool]]
