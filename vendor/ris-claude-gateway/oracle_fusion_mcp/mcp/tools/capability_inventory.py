"""Curated capability inventory (data only, no logic, no I/O).

Every row below was verified against live /describe metadata captured in
``tests/fixtures`` or live GET responses during investigation. Statuses use
the task vocabulary: IMPLEMENTED_AND_CONTROLLED, READY_FOR_CONTROLLED_
IMPLEMENTATION, DISCOVERED_BUT_NOT_IMPLEMENTED, REQUIRES_WORKFLOW_
INVESTIGATION, REQUIRES_STATE_VERIFICATION, HIGH_RISK_BLOCKED, UNSUPPORTED,
NOT_RESOLVED.
"""

# Verified live 2026-09-30: invoices collection create is POST with 25
# mandatory attributes (mix of caller-supplied and system-managed). Stored
# here so validation code and tests share one reviewed contract instead of
# re-deriving it. Invoice creation stays HIGH risk: financial liability.
INVOICE_CREATE_CONTRACT = {
    "resource": "invoices",
    "scope": "collection",
    "operation": "create",
    "method": "POST",
    "request_types": [
        "application/vnd.oracle.adf.resourceitem+json",
        "application/json",
    ],
    "mandatory": [
        {"name": "InvoiceId", "type": "integer", "caller_supplied": False},
        {"name": "InvoiceNumber", "type": "string", "caller_supplied": True},
        {"name": "InvoiceCurrency", "type": "string", "caller_supplied": True},
        {"name": "PaymentCurrency", "type": "string", "caller_supplied": True},
        {"name": "InvoiceAmount", "type": "number", "caller_supplied": True},
        {"name": "InvoiceDate", "type": "date", "caller_supplied": True},
        {"name": "BusinessUnit", "type": "string", "caller_supplied": True},
        {"name": "SupplierNumber", "type": "string", "caller_supplied": True},
        {"name": "AccountingDate", "type": "date", "caller_supplied": True},
        {"name": "InvoiceSourceCode", "type": "string", "caller_supplied": True},
        {"name": "PaymentTerms", "type": "string", "caller_supplied": True},
        {"name": "TermsDate", "type": "date", "caller_supplied": True},
        {"name": "PaymentMethodCode", "type": "string", "caller_supplied": True},
        {"name": "PaymentMethod", "type": "string", "caller_supplied": True},
        {"name": "LegalEntity", "type": "string", "caller_supplied": True},
        {"name": "ApprovalStatus", "type": "string", "caller_supplied": False},
        {"name": "PaidStatus", "type": "string", "caller_supplied": False},
        {"name": "Party", "type": "string", "caller_supplied": False},
        {"name": "AccountCodingStatus", "type": "string", "caller_supplied": False},
        {"name": "FundsStatus", "type": "string", "caller_supplied": False},
        {"name": "CreationDate", "type": "datetime", "caller_supplied": False},
        {"name": "CreatedBy", "type": "string", "caller_supplied": False},
        {"name": "LastUpdatedBy", "type": "string", "caller_supplied": False},
        {"name": "LastUpdateDate", "type": "datetime", "caller_supplied": False},
        {"name": "Stream", "type": "string", "caller_supplied": False},
    ],
    "risk": "HIGH",
    "status": "HIGH_RISK_BLOCKED",
    "reason": "AP invoice creation posts a financial liability with workflow, "
              "tax, and payment consequences; no controlled executor exists.",
}

HCM_INVENTORY = [
    {"resource": "absences", "operation": "update comments", "method": "PATCH",
     "risk": "LOW", "status": "IMPLEMENTED_AND_CONTROLLED",
     "reason": "curated field allowlist + version binding + GET verification"},
    {"resource": "absences", "operation": "update approvalStatusCd",
     "method": "PATCH", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "approval outcome is workflow-owned; no approval action exposed "
               "in describe (only restrictAbsenceEdit, which governs edit locks)"},
    {"resource": "absences", "operation": "restrictAbsenceEdit",
     "method": "POST", "risk": "MEDIUM",
     "status": "REQUIRES_WORKFLOW_INVESTIGATION",
     "reason": "edit-lock rule action exists but manager/employee rule "
               "semantics and verification are unverified"},
    {"resource": "workers", "operation": "terminate (workRelationships)",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "employment termination is irreversible without explicit executor"},
    {"resource": "workers", "operation": "changeLegalEmployer",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "legal-employer change affects payroll, tax, and accruals"},
    {"resource": "salaries", "operation": "update", "method": "PATCH",
     "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "compensation amounts affect payroll; no executor exists"},
]

FSCM_INVENTORY = [
    {"resource": "draftPurchaseOrders", "operation": "submit",
     "method": "POST", "risk": "MEDIUM",
     "status": "IMPLEMENTED_AND_CONTROLLED",
     "reason": "curated allowlist + preview + confirmation + live-verified once"},
    {"resource": "purchaseOrders", "operation": "submit",
     "method": "POST", "risk": "MEDIUM",
     "status": "READY_FOR_CONTROLLED_IMPLEMENTATION",
     "reason": "metadata-resolved, representable, verifiable via Status"},
    {"resource": "purchaseOrders", "operation": "close",
     "method": "POST", "risk": "MEDIUM",
     "status": "READY_FOR_CONTROLLED_IMPLEMENTATION",
     "reason": "metadata-resolved, representable, verifiable via Status"},
    {"resource": "purchaseOrders", "operation": "reopen",
     "method": "POST", "risk": "MEDIUM",
     "status": "READY_FOR_CONTROLLED_IMPLEMENTATION",
     "reason": "metadata-resolved, representable, verifiable via Status"},
    {"resource": "purchaseOrders", "operation": "hold",
     "method": "POST", "risk": "MEDIUM",
     "status": "REQUIRES_STATE_VERIFICATION",
     "reason": "no authoritative hold-state field in describe or GET responses"},
    {"resource": "purchaseOrders", "operation": "removeHold",
     "method": "POST", "risk": "MEDIUM",
     "status": "REQUIRES_STATE_VERIFICATION",
     "reason": "no authoritative hold-state field in describe or GET responses"},
    {"resource": "purchaseOrders", "operation": "cancel",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "destructive lifecycle transition; no executor exists"},
    {"resource": "invoices", "operation": "create",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "financial liability; see INVOICE_CREATE_CONTRACT; no executor"},
    {"resource": "invoices", "operation": "validateInvoice/cancelInvoice",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "validation/cancellation drive accounting and payment outcomes"},
    {"resource": "invoices", "operation": "change_invoice_status (legacy wrapper)",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "legacy wrapper body does not match live action parameters"},
    {"resource": "payablesPayments", "operation": "initiateStopPayment/initiateEscheat",
     "method": "POST", "risk": "HIGH", "status": "HIGH_RISK_BLOCKED",
     "reason": "payment movement; no executor exists"},
    {"resource": "suppliers", "operation": "create",
     "method": "POST", "risk": "MEDIUM",
     "status": "DISCOVERED_BUT_NOT_IMPLEMENTED",
     "reason": "master-data create; contract and verification not yet curated"},
    {"resource": "purchaseRequisitions", "operation": "withdraw/cancel",
     "method": "POST", "risk": "MEDIUM",
     "status": "DISCOVERED_BUT_NOT_IMPLEMENTED",
     "reason": "actions exist in describe; no curated executor yet"},
]
