"""Curated HR workflow tools.

These wrap the generic client for the most common HR questions, encoding the
pod-verified ADF gotchas (DESIGN.md §13): people live on ``emps``; reporting
managers come via the assignment's ``managers`` HATEOAS link (nested expand
returns it empty); expanded child collections need a large limit; ``absences``
honors ``q`` only on person attributes.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from ..context import ServerContext

_EXPAND_LIMIT = 200  # expanded child collections are truncated by `limit`
_MAX_DEPTH = 10

# Compact field sets keep responses small.
_FIND_FIELDS = ["PersonNumber", "DisplayName", "WorkEmail"]
_PROFILE_FIELDS = [
    "PersonNumber",
    "DisplayName",
    "WorkEmail",
    "HireDate",
    "BusinessUnitName",
]

_ORG_MAP: dict[str, tuple[str, str]] = {
    "department": ("departments", "Name"),
    "location": ("locations", "LocationName"),
    "job": ("jobs", "Name"),
    "position": ("positions", "Name"),
    "grade": ("grades", "Name"),
}


def _child_items(value: Any) -> list[dict[str, Any]]:
    """Normalize Oracle expanded child collections to a list of items."""
    if isinstance(value, dict):
        items = value.get("items", [])
        return items if isinstance(items, list) else []

    if isinstance(value, list):
        return value

    return []


def _esc(value: str) -> str:
    return str(value).replace("'", "''")


def _find_link(obj: dict[str, Any], name: str) -> str | None:
    for link in obj.get("links", []) or []:
        if not isinstance(link, dict):
            continue

        if link.get("name") == name or str(link.get("rel", "")).endswith(name):
            return link.get("href")

    return None


def register(mcp: FastMCP, ctx: ServerContext) -> None:
    amounts_redacted = ctx.redactor.enabled

    # ---- people lookup ---------------------------------------------------

    @mcp.tool()
    async def find_worker(
        name: str | None = None,
        email: str | None = None,
        person_number: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Find a worker by exactly one of: name, email, or person number.

        Name is a partial (case-sensitive) match; email and person number are
        exact. Returns compact records (no PII).
        """
        given = [
            (k, v)
            for k, v in (
                ("name", name),
                ("email", email),
                ("person_number", person_number),
            )
            if v
        ]

        if len(given) != 1:
            return {
                "error": "Provide exactly one of: name, email, person_number."
            }

        key, value = given[0]

        if key == "name":
            q = f"DisplayName LIKE '%{_esc(value)}%'"
        elif key == "email":
            q = f"WorkEmail = '{_esc(value)}'"
        else:
            q = f"PersonNumber = '{_esc(value)}'"

        result = await ctx.client.query(
            "emps",
            q=q,
            fields=_FIND_FIELDS,
            limit=limit,
        )

        ctx.audit.record(
            tool="find_worker",
            resource="emps",
            count=result.get("count"),
        )

        return {
            "workers": result["items"],
            "count": result["count"],
        }

    @mcp.tool()
    async def get_worker_profile(
        person_number: str,
        include_assignments: bool = True,
    ) -> dict[str, Any]:
        """Return a worker's identity, contact, employment and (optionally) assignments."""
        expand = ["assignments"] if include_assignments else None

        result = await ctx.client.query(
            "emps",
            q=f"PersonNumber = '{_esc(person_number)}'",
            fields=_PROFILE_FIELDS if not include_assignments else None,
            expand=expand,
            limit=_EXPAND_LIMIT,
        )

        ctx.audit.record(
            tool="get_worker_profile",
            resource="emps",
            key=person_number,
        )

        if not result["items"]:
            return {
                "error": f"No worker found for person number {person_number}."
            }

        return {
            "profile": result["items"][0],
        }

    @mcp.tool()
    async def list_direct_reports(person_number: str) -> dict[str, Any]:
        """List the direct reports of a worker (one level down)."""
        reports = await _direct_reports(person_number)

        ctx.audit.record(
            tool="list_direct_reports",
            resource="emps",
            key=person_number,
            count=len(reports),
        )

        return {
            "manager": person_number,
            "direct_reports": reports,
            "count": len(reports),
        }

    async def _direct_reports(person_number: str) -> list[dict[str, Any]]:
        result = await ctx.client.query(
            "emps",
            q=f"PersonNumber = '{_esc(person_number)}'",
            expand=["directReports"],
            limit=_EXPAND_LIMIT,
        )

        if not result["items"]:
            return []

        dr = result["items"][0].get("directReports", {})
        items = (
            dr.get("items", [])
            if isinstance(dr, dict)
            else (dr or [])
        )

        out = []

        for r in items:
            out.append(
                {
                    "PersonNumber": r.get("PersonNumber"),
                    "DisplayName": r.get("DisplayName"),
                }
            )

        return out

    # ---- reporting chain -------------------------------------------------

    @mcp.tool()
    async def get_reporting_chain(
        person_number: str,
        direction: str = "up",
        depth: int = 5,
        effective_date: str | None = None,
    ) -> dict[str, Any]:
        """Walk the management chain UP (to CEO) or DOWN (org beneath a person).

        ``depth`` is capped at 10. For terminated workers an UP chain may be
        empty — pass ``effective_date`` (YYYY-MM-DD) to resolve as-of that date.
        """
        depth = max(1, min(depth, _MAX_DEPTH))

        if direction == "down":
            chain = await _chain_down(person_number, depth)

            ctx.audit.record(
                tool="get_reporting_chain",
                resource="emps",
                key=person_number,
                count=len(chain),
            )

            return {
                "person_number": person_number,
                "direction": "down",
                "chain": chain,
            }

        chain = await _chain_up(
            person_number,
            depth,
            effective_date,
        )

        ctx.audit.record(
            tool="get_reporting_chain",
            resource="workers",
            key=person_number,
            count=len(chain),
        )

        result: dict[str, Any] = {
            "person_number": person_number,
            "direction": "up",
            "chain": chain,
        }

        if not chain:
            result["note"] = (
                "No manager chain found. If this worker is terminated, retry with "
                "effective_date=YYYY-MM-DD as of their active period."
            )

        return result

    async def _chain_down(
        person_number: str,
        depth: int,
    ) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []

        seen = {person_number}
        frontier: list[tuple[str, int]] = [(person_number, 0)]

        while frontier:
            pn, level = frontier.pop(0)

            if level >= depth:
                continue

            for r in await _direct_reports(pn):
                rpn = r.get("PersonNumber")

                if not rpn or rpn in seen:
                    continue

                seen.add(rpn)

                chain.append(
                    {
                        **r,
                        "level": level + 1,
                    }
                )

                frontier.append(
                    (
                        rpn,
                        level + 1,
                    )
                )

        return chain

    async def _chain_up(
        person_number: str,
        depth: int,
        effective_date: str | None,
    ) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []

        seen: set[str] = set()
        current = person_number

        for _ in range(depth):
            if not current or current in seen:
                break

            seen.add(current)

            manager = await _resolve_manager(
                current,
                effective_date,
            )

            if not manager or not manager.get("PersonNumber"):
                break

            chain.append(manager)
            current = manager["PersonNumber"]

        return chain

    async def _resolve_manager(
        person_number: str,
        effective_date: str | None,
    ) -> dict[str, Any] | None:
        # keep_links so we can follow the assignment's managers child link;
        # nested expand (workRelationships.assignments.managers) returns it empty.
        res = await ctx.client.query(
            "workers",
            q=f"PersonNumber = '{_esc(person_number)}'",
            expand=["workRelationships.assignments"],
            keep_links=True,
            effective_date=effective_date,
            limit=_EXPAND_LIMIT,
        )

        if not res["items"]:
            return None

        worker = res["items"][0]

        for wr in _child_items(worker.get("workRelationships")):
            for asg in _child_items(wr.get("assignments")):
                href = _find_link(asg, "managers")

                if not href:
                    continue

                mgr_data = await ctx.client.get_href(
                    href,
                    effective_date=effective_date,
                )

                rows = (
                    mgr_data.get("items", [])
                    if isinstance(mgr_data, dict)
                    else []
                )

                line = next(
                    (
                        r
                        for r in rows
                        if r.get("ManagerType") == "LINE_MANAGER"
                    ),
                    None,
                )

                row = line or (rows[0] if rows else None)

                if not row:
                    continue

                return await _identify_manager(
                    row,
                    effective_date,
                )

        return None

    async def _identify_manager(
        row: dict[str, Any],
        effective_date: str | None,
    ) -> dict[str, Any] | None:
        manager_person_id = row.get("ManagerPersonId")
        manager_assignment = row.get("ManagerAssignmentNumber")

        if manager_person_id:
            lookup = await ctx.client.query(
                "workers",
                q=f"PersonId = {manager_person_id}",
                fields=["PersonNumber", "DisplayName"],
                effective_date=effective_date,
                limit=1,
            )

            if lookup["items"]:
                m = lookup["items"][0]

                return {
                    "PersonNumber": m.get("PersonNumber"),
                    "DisplayName": m.get("DisplayName"),
                    "ManagerType": row.get("ManagerType"),
                }

        if manager_assignment:
            # Default numbering: assignment number "E<PersonNumber>";
            # verify by lookup.
            candidate = str(manager_assignment).lstrip("Ee")

            lookup = await ctx.client.query(
                "emps",
                q=f"PersonNumber = '{_esc(candidate)}'",
                fields=["PersonNumber", "DisplayName"],
                limit=1,
            )

            if lookup["items"]:
                m = lookup["items"][0]

                return {
                    "PersonNumber": m.get("PersonNumber"),
                    "DisplayName": m.get("DisplayName"),
                    "ManagerType": row.get("ManagerType"),
                }

            return {
                "PersonNumber": None,
                "ManagerAssignmentNumber": manager_assignment,
                "ManagerType": row.get("ManagerType"),
                "note": "manager not resolvable to a person",
            }

        return None

    # ---- org lookup ------------------------------------------------------

    @mcp.tool()
    async def lookup_org(
        org_type: str,
        search: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Look up work-structure objects: department | location | job | position | grade."""
        mapping = _ORG_MAP.get(org_type)

        if not mapping:
            return {
                "error": (
                    f"Unknown org_type {org_type!r}. "
                    f"Use one of {sorted(_ORG_MAP)}."
                )
            }

        resource, name_attr = mapping

        result = await ctx.client.query(
            resource,
            q=f"{name_attr} LIKE '%{_esc(search)}%'",
            limit=limit,
        )

        ctx.audit.record(
            tool="lookup_org",
            resource=resource,
            count=result.get("count"),
        )

        return {
            "org_type": org_type,
            "results": result["items"],
            "count": result["count"],
        }

    # ---- compensation ----------------------------------------------------

    @mcp.tool()
    async def get_current_compensation(
        person_number: str,
    ) -> dict[str, Any]:
        """Return salary history per assignment for a worker (amounts redacted by default)."""
        emp = await ctx.client.query(
            "emps",
            q=f"PersonNumber = '{_esc(person_number)}'",
            expand=["assignments"],
            limit=_EXPAND_LIMIT,
        )

        if not emp["items"]:
            return {
                "error": f"No worker found for person number {person_number}."
            }

        assignments = emp["items"][0].get("assignments", {})
        asg_items = (
            assignments.get("items", [])
            if isinstance(assignments, dict)
            else []
        )

        salary_history: list[dict[str, Any]] = []

        for asg in asg_items:
            asg_id = asg.get("AssignmentId")

            if not asg_id:
                continue

            # client floor already redacts each salaries result before we wrap it.
            rows = await ctx.client.query(
                "salaries",
                q=f"AssignmentId = {asg_id}",
                order_by="DateFrom:desc",
                limit=_EXPAND_LIMIT,
            )

            salary_history.extend(rows["items"])

        ctx.audit.record(
            tool="get_current_compensation",
            resource="salaries",
            key=person_number,
            count=len(salary_history),
            sensitive=not ctx.redactor.enabled,
        )

        # Do NOT redact this wrapper: the key "salary_history" matches the salary
        # keyword and would mask the whole (already-row-redacted) list.
        return {
            "person_number": person_number,
            "salary_history": salary_history,
            "amounts_redacted": amounts_redacted,
        }

    # ---- absences --------------------------------------------------------

    @mcp.tool()
    async def list_absences(
        person_number: str,
        status: str | None = None,
        since: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """List a worker's absences. Status/since are filtered client-side.

        The ``absences`` resource honors ``q`` only on person attributes, so we
        query by person number then filter status/date locally.
        """
        result = await ctx.client.query(
            "absences",
            q=f"personNumber = '{_esc(person_number)}'",
            limit=_EXPAND_LIMIT,
        )

        rows = result["items"]

        if status:
            rows = [
                r
                for r in rows
                if str(r.get("absenceStatusCd", "")).lower()
                == status.lower()
            ]

        if since:
            rows = [
                r
                for r in rows
                if str(r.get("startDate", "")) >= since
            ]

        rows = rows[:limit]

        ctx.audit.record(
            tool="list_absences",
            resource="absences",
            key=person_number,
            count=len(rows),
        )

        return {
            "person_number": person_number,
            "absences": rows,
            "count": len(rows),
        }