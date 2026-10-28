"""Tool policy for the live pipeline (NFR-04).

Default deny. The sets below come from the real tool list of the
ris-claude-gateway bundle (59 tools, inspected over stdio). A tool that is
not named here, including any tool a future bundle version adds, is denied.
"""
from __future__ import annotations

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
        return Decision("deny", f"{name}: personal and pay data is disabled for this POC")
    if name in UNUSED:
        return Decision("deny", f"{name}: not needed")
    return Decision("deny", f"{name}: not on the allowlist")


def sdk_allowed_tools(allow_sensitive: bool = False) -> list[str]:
    """Names handed to the Agent SDK as pre-approvable. Data tools are excluded:
    they must always pass through can_use_tool so the plan check runs."""
    return sorted([PREFIX + t for t in DISCOVERY | COMPUTE] + [BRIDGE_PREFIX + t for t in BRIDGE_TOOLS])
