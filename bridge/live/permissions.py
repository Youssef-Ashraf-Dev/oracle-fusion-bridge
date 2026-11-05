"""Tool policy for the live pipeline (NFR-04).

Default deny. The sets below come from the real tool list of the
ris-claude-gateway bundle (59 tools, inspected over stdio). A tool that is
not named here, including any tool a future bundle version adds, is denied.
"""
from __future__ import annotations

import re
from typing import Optional

from dataclasses import dataclass

SERVER = "fusion"          # MCP server name; tools appear as mcp__fusion__<tool>
PREFIX = f"mcp__{SERVER}__"

# Metadata only: no business rows. Runs without approval.
DISCOVERY = frozenset({
    "server_info", "list_resources", "describe_resource", "get_capabilities",
    "fusion_list_resources", "fusion_describe_resource", "fusion_find_relationships",
    "fusion_describe_actions", "fusion_create_requirements", "fusion_operation_inventory",
})

# Pure arithmetic over records the agent already holds. No Oracle call.
COMPUTE = frozenset({
    "aggregate_records", "sumif_records", "group_by_aggregate",
    "lookup_record", "percentage_of_total", "safe_divide",
})

# Return business rows. Allowed only inside an approved plan.
DATA = frozenset({
    "fusion_query_resource", "fusion_get_resource_record", "query_resource", "get_record",
    "search_invoices", "get_invoice_details", "search_payments", "get_payment_details",
    "list_payment_terms", "search_purchase_orders", "get_po_details", "search_suppliers",
    "get_supplier_details", "search_requisitions", "search_receiving_receipts",
    "search_approved_suppliers", "search_categories", "search_journal_batches",
    "search_gl_balances", "list_ledgers", "list_chart_of_accounts", "search_currency_rates",
    "search_ar_invoices", "get_ar_invoice_details", "search_receipts", "search_expense_reports",
    "list_direct_reports", "get_reporting_chain", "lookup_org", "list_absences", "list_changes",
})

# Only these tools take a `resource` argument. A plan step's resource scopes calls to them and nothing else:
# typed tools (search_invoices, ...) have no resource dimension, whatever the plan says.
GENERIC_READERS = frozenset({"fusion_query_resource", "fusion_get_resource_record", "query_resource", "get_record"})

# Resources the generic readers could reach that hold pay, bank, identity or credential data. The typed tools for these are
# already denied by name; this closes the same door for query_resource and friends. Substring match, case-insensitive.
SENSITIVE_RESOURCE_PARTS = ("salar", "compensation", "payroll", "bankaccount", "paymentmethod", "useraccount",
                            "nationalidentifier", "personalidentifier", "password", "credential")


def touches_sensitive_resource(tool_input: dict) -> str:
    """The first resource, child or expand value in a generic-reader call that names pay or identity data, else ''."""
    values: list = []
    for key in ("resource", "child", "expand"):
        v = tool_input.get(key)
        values += v if isinstance(v, (list, tuple)) else ([v] if v else [])
    for v in values:
        for part in str(v).lower().replace("_", "").replace("-", "").replace("/", " ").replace(",", " ").split():
            if any(bad in part for bad in SENSITIVE_RESOURCE_PARTS):
                return str(v)
    return ""


# Personal or pay data. Data tools, but denied unless ALLOW_HCM_SENSITIVE=true.
SENSITIVE = frozenset({
    "find_worker", "get_worker_profile", "get_current_compensation", "search_user_accounts",
})

# Every preview/execute tool. Denied unconditionally, never configurable.
WRITE_WORKFLOW = frozenset({
    "fusion_preview_write", "fusion_preview_batch", "fusion_preview_controlled_action",
    "fusion_preview_hcm_absence_update", "preview_draft_po_submit",
    "fusion_execute_confirmed_write", "fusion_execute_confirmed_batch",
})

# Known but not needed: hits Oracle for no user benefit.
UNUSED = frozenset({"test_oracle_connection"})

KNOWN = DISCOVERY | COMPUTE | DATA | SENSITIVE | WRITE_WORKFLOW | UNUSED

# In-process tools the bridge itself serves to the agent (no Oracle access).
BRIDGE_SERVER = "bridge"
BRIDGE_PREFIX = f"mcp__{BRIDGE_SERVER}__"
BRIDGE_TOOLS = frozenset({"propose_query_plan"})


@dataclass(frozen=True)
class Decision:
    verdict: str          # "allow" | "needs_plan" | "deny"
    reason: str = ""


def bare(tool_name: str) -> str:
    return tool_name[len(PREFIX):] if tool_name.startswith(PREFIX) else tool_name


_OR_WORD = re.compile(r"(?i)\sor\s")
WORKERS_OR_REASON = ("a query on workers that joins people with 'or' returns only the FIRST person on this pod (checked: ten person "
                     "numbers in one query returned one). Ask for ONE person per call: q PersonNumber = '<number>', fields "
                     "PersonNumber,DisplayName. Up to 10 such calls fit in the same approved plan step.")


def workers_or_lookup(tool_name: str, tool_input: dict) -> bool:
    """True for a generic HR read on workers whose filter joins conditions with 'or' (an 'or' inside a quoted value does not count)."""
    if bare(tool_name) != "query_resource" or str(tool_input.get("resource", "")).strip().lower() != "workers":
        return False
    q = re.sub(r"'[^']*'", "''", str(tool_input.get("q") or ""))
    return bool(_OR_WORD.search(q))


_CLAUSE_ATTRIBUTE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*(?:>=|<=|<>|!=|=|<|>|\b(?:LIKE|IN)\b)", re.I)


# The typed searches that turn date_from and date_to into two conditions on ONE attribute (read from the MCP's client code), and that attribute.
DATE_RANGE_SEARCHES = {"search_invoices": "InvoiceDate", "search_payments": "PaymentDate", "search_ar_invoices": "CreationDate",
                       "search_receipts": "ReceiptDate", "search_expense_reports": "ReportSubmitDate"}
GENERIC_FILTER_READERS = {"query_resource", "fusion_query_resource"}


def repeated_filter_attribute(tool_name: str, tool_input: dict) -> Optional[str]:
    """The attribute that a read names twice in its filter, or None. On this pod several conditions on the SAME attribute collapse silently to the
    last one (measured on absences: startDate >= 2026-01-01;startDate >= 2026-09-01 returned 16 rows, the same two reversed 4,115; and on invoices:
    InvoiceDate >= X;InvoiceDate <= Y returned the total of <= Y alone). A typed search with both date_from and date_to builds exactly such a pair.
    Text inside quotes is ignored."""
    name = bare(tool_name)
    if name not in GENERIC_FILTER_READERS and name not in DATE_RANGE_SEARCHES:
        return None
    named: list[str] = []
    for clause in re.sub(r"'[^']*'", "''", str(tool_input.get("q") or "")).split(";"):
        m = _CLAUSE_ATTRIBUTE.match(clause)
        if m:
            named.append(m.group(1))
    if name in DATE_RANGE_SEARCHES:
        named += [DATE_RANGE_SEARCHES[name]] * (bool(tool_input.get("date_from")) + bool(tool_input.get("date_to")))
    seen: set[str] = set()
    for attribute in named:
        if attribute.lower() in seen:
            return attribute
        seen.add(attribute.lower())
    return None


_AND_WORD = re.compile(r"(?i)\sand\s")
ABSENCES_AND_REASON = ("the absences resource on this pod rejects the word 'and' in a filter with a 400. Join conditions on DIFFERENT attributes with ';' "
                       "(absenceType LIKE 'Sick%';startDate >= 2025-01-01) and never repeat an attribute: the pod keeps only the last condition on the "
                       "same attribute, with no error. For a window on one date, send the lower bound (startDate >= FROM) with the other conditions and "
                       "drop the rows past the upper bound yourself. To COUNT a window, make two calls with limit 1 and total_results true, "
                       "'<other conditions>;startDate >= FROM' and '<other conditions>;startDate >= THE DAY AFTER TO', and subtract the second total "
                       "from the first. Do not report a total from a filter that was refused or ignored.")


def absences_and_keyword(tool_name: str, tool_input: dict) -> bool:
    """True for a generic read on absences whose filter joins conditions with the word 'and' (the pod answers 400). Quoted text does not count."""
    if bare(tool_name) != "query_resource" or str(tool_input.get("resource", "")).strip().lower() != "absences":
        return False
    return bool(_AND_WORD.search(re.sub(r"'[^']*'", "''", str(tool_input.get("q") or ""))))


def repeated_filter_reason(attribute: str, typed_search: bool = False) -> str:
    text = (f"on this pod a filter that names the same attribute twice ({attribute} appears twice) silently keeps only the LAST condition on it, with "
            f"no error (measured on absences and on invoices), so a range written as two {attribute} conditions loses its other bound. ")
    if typed_search:
        text += "A typed search turns date_from and date_to into exactly such a pair, so send only ONE of them. "
    return text + (f"Send ONE condition per attribute: the lower bound ({attribute} >= FROM) with conditions on other attributes, read the rows and "
                   f"drop those past the upper bound yourself. To count a window, ask for the total (limit 1) twice and subtract: "
                   f"total({attribute} >= FROM) minus total({attribute} >= THE DAY AFTER TO). Do not report a total from a filter that was refused or ignored.")


def classify(tool_name: str, allow_sensitive: bool = False) -> Decision:
    """Decide what the policy says about one tool call, before any plan check."""
    if tool_name.startswith(BRIDGE_PREFIX):
        if tool_name[len(BRIDGE_PREFIX):] in BRIDGE_TOOLS:
            return Decision("allow")
        return Decision("deny", f"{tool_name}: not on the allowlist")
    if not tool_name.startswith(PREFIX):
        return Decision("deny", f"{tool_name}: only the Fusion MCP tools are available")
    name = bare(tool_name)
    if name in WRITE_WORKFLOW:
        return Decision("deny", f"{name}: writes are not available in this bridge")
    if name in DISCOVERY or name in COMPUTE:
        return Decision("allow")
    if name in DATA:
        return Decision("needs_plan")
    if name in SENSITIVE:
        if allow_sensitive:
            return Decision("needs_plan")
        return Decision("deny", f"{name} returns profile, pay or account details and is disabled for this POC. Worker NAMES are "
                                "still available: use query_resource on the workers resource with fields PersonNumber,DisplayName. "
                                "Pay, bank, ID-document and user-account data are not available")
    if name in UNUSED:
        return Decision("deny", f"{name}: not needed")
    return Decision("deny", f"{name}: not on the allowlist")


def sdk_allowed_tools(allow_sensitive: bool = False) -> list[str]:
    """Names handed to the Agent SDK as pre-approvable. Data tools are excluded:
    they must always pass through can_use_tool so the plan check runs."""
    return sorted([PREFIX + t for t in DISCOVERY | COMPUTE] + [BRIDGE_PREFIX + t for t in BRIDGE_TOOLS])
