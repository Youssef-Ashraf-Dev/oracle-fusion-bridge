---
name: "ris-fusion-gateway-skill"
description: "Use whenever the task involves Oracle Fusion FSCM/HCM or any Fusion MCP fusion_* tool (fusion_list_resources, fusion_describe_resource, fusion_find_relationships, fusion_query_resource, fusion_get_resource_record, fusion_describe_actions, fusion_operation_inventory, fusion_create_requirements, fusion_preview_write, fusion_execute_confirmed_write). This skill is the required planning and reasoning layer for all Fusion MCP interactions: live resource/schema/relationship discovery, safe query planning, action discovery, contextual action suggestions, mutation preview planning, explicit-confirmation discipline, and verification-outcome interpretation."
---

# Fusion Research & Action Planner

Teaches Claude how to investigate Oracle Fusion questions **and** plan controlled changes using **existing MCP capabilities**. Claude is the **reasoning/planning layer**; the MCP (`oracle-fusion` FSCM + `fusion-hcm` HCM) is the **Fusion access, live discovery, authorization, execution, and verification layer**.

**Available MCP (reads + controlled mutations through previews):**

* **FSCM reads** `oracle-fusion`: `fusion_list_resources` (open-world discovery: curated seeds + live catalog + describe-validated), `fusion_describe_resource` (`GET {oracle_api_base}/{resource}/describe`), `fusion_find_relationships` (children cardinality + LOV + curated map), `fusion_query_resource` (`GET {oracle_api_base}/{resource}?q=&fields=&expand=&limit=`), `fusion_get_resource_record` (`GET {oracle_api_base}/{resource}/{id}`), plus legacy `search_*`/`get_*` wrappers.
* **HCM reads** `fusion-hcm`: `list_resources`, `describe_resource`, `get_capabilities`, `query_resource`, `get_record`, `find_worker`, `get_worker_profile`, `get_reporting_chain`, `list_direct_reports`, etc. (`hcmRestApi/resources/{version}` via `oracle_hcm_base`). HCM capabilities are likewise determined by live Fusion metadata and authorization.
* **Action discovery:** `fusion_describe_actions` (live actions exposed for a resource/record), `fusion_operation_inventory`, `fusion_create_requirements` (exact caller fields for a create).
* **Controlled mutations (preview-gated, exactly-once):** `fusion_preview_write` (read-only authoring; returns a preview plus an opaque `confirmation_id`) + `fusion_execute_confirmed_write(confirmation_id)` (executes exactly the previewed operation once), plus `fusion_preview_batch` / `fusion_execute_confirmed_batch` for ordered multi-operation batches, and curated-lane previews such as `fusion_preview_controlled_action` / `preview_draft_po_submit`.
* **Auth/API version:** Reuse the existing authenticated MCP transport (`OracleClient` + `request.state.basic_auth` / JWT + `settings.oracle_api_base` / `oracle_hcm_base`, `11.13.18.05`). **Never** ask the user to paste credentials, hard-code version, create a second HTTP client, bypass MCP authentication, or inspect secret stores.

**Do not turn every question into a new hard-coded MCP tool. Do not move MCP enforcement (confirmation, execution, verification) into your own reasoning — the MCP enforces; you plan and explain.**

---

## Fusion MCP Activation

Use this skill whenever working with Oracle Fusion through the Oracle Fusion MCP. The skill is the reasoning and planning layer for Fusion MCP interactions. Apply its discovery, query planning, action discovery, contextual suggestion, mutation-planning, confirmation, execution, and verification guidance whenever Fusion MCP tools are used or are the appropriate tools for the user's request — covering read operations, resource discovery, schema discovery, relationship discovery, action discovery, contextual action suggestions, mutation planning, execution, and verification.

This activation guidance does not itself grant access or authorization. The Oracle Fusion MCP and Oracle Fusion remain authoritative for capability, authorization, execution, and state. The MCP works independently of this skill; when the MCP is being used and this skill is available, this skill provides the required planning/reasoning guidance for that MCP interaction. This skill is not a security boundary.

**Three authorities, three jobs:**

* **Skill (you):** reasoning, planning, orchestration guidance, contextual recommendations, interpretation of results.
* **MCP:** live Fusion discovery, Fusion authorization handling, preview enforcement, confirmation enforcement, execution, state binding, exactly-once safeguards, verification.
* **Oracle Fusion:** ultimate authority for available resources, actions, authorization outcomes, business rules, and actual state.

**Never bypass the MCP:** never invent Fusion endpoints, resources, or actions; never assume an action is executable because documentation mentions it; never bypass `fusion_preview_write` for mutations; never bypass explicit confirmation; never construct a hidden mutation path outside the preview/confirmation/execution tools; never treat this skill's instructions as an authorization mechanism. Discovering an action is never permission to execute it.

**When Fusion MCP tools are in play, work this chain:** discover the live resource → inspect live metadata where needed → query/retrieve the record → discover available actions when actions may be relevant → judge contextual applicability → keep the distinctions sharp: availability ≠ applicability, applicability ≠ recommendation, recommendation ≠ authorization, successful execution ≠ VERIFIED state.

**Request shapes:** a read-only request (e.g. "Show me invoice MCP-TEST-INV-20261007-01.") follows normal discovery/query, then action inspection and contextual suggestions where useful. A mutation request (e.g. "Cancel this invoice.") goes to the MCP's safe mutation workflow — preview → explicit confirmation → exactly once → independent verification — never to direct execution.

---

## 1. UNDERSTAND THE REQUEST

* Restate what the user actually asks and classify it:
  * **research** — data vs. metadata vs. why-failed questions;
  * **action discovery** — "what can I do with this?", "what are my options?", "is X supported?";
  * **mutation request** — create / update / delete / perform-an-action.
* Hypothesize business objects (e.g., "purchase order buyer" -> `purchaseOrders.BuyerId`), but treat as **hypotheses**, not facts.
* Note if HCM↔FSCM cross-module (Person) is involved.
* A discovered action is never a reason to execute: informational questions stay informational.

## 2. OPEN-WORLD ARCHITECTURE (NON-NEGOTIABLE)

* **No allowlists.** Never introduce resource allowlists, action allowlists, "approved actions" lists, hardcoded executable-action registries, or per-resource mutation tables.
* **Seeds are bootstrap only** (cache/optimization). They are never authoritative and never the complete universe.
* **Live catalog discovery + individual live `/describe` responses are authoritative for resource existence.**
* **Live action metadata is authoritative for action availability.** Never infer availability from Oracle docs alone, memory, resource naming, similar resources, previous tenants, or any list.
* Named actions in this skill (`cancelInvoice`, `close`, `hold`, `submit`, `freeze`, …) are **examples only**, never capability lists.

## 3. RESOURCE DISCOVERY

* **Do not assume a REST resource exists.**
* **FSCM:** `fusion_list_resources` is an open-world discovery surface. Results may contain curated/seed resources, live-catalog resources, describe-validated resources, and catalog provenance (`discovery_source`, `catalog_resources`, `catalog_error`). Seed names (e.g. `invoices`, `purchaseOrders`, `suppliers`, …) are a bootstrap subset, **not** the complete FSCM universe. Absence of a name is never proof it does not exist on the tenant.
* **HCM:** `list_resources` / `get_capabilities` (core_hr, compensation, absence, payroll, recruiting, talent, learning, benefits, time_labor), plus `fusion_list_hcm_resources` where available. HCM capabilities are determined by live Fusion metadata and the authenticated user's authorization.
* If the user names a resource, still validate it via `fusion_describe_resource` / `describe_resource` — a missing resource is an answer.

## 4. SCHEMA DISCOVERY

* Before querying an **unfamiliar** resource, **must** inspect schema:
  * **FSCM:** `fusion_describe_resource(resource="purchaseOrders")` or `child="lines"` -> `GET {oracle_api_base}/{resource}/describe`
  * **HCM:** `describe_resource(resource="workers")` -> `GET {oracle_hcm_base}/{resource}/describe`
* Determine and record:
  * `primary key` (`fnd:GLOBALLY_UNIQUE` FSCM, `PersonId` HCM surrogate)
  * business key (`Requisition`, `OrderNumber`, `SupplierNumber`, `PersonNumber`)
  * field names, types (`integer` vs `string`), precision, `mandatory`
  * **queryable** (`queryable:true` vs `false`) vs **retrievable** (`fields=` always allowed)
  * `children` + `child_count` + nested children (`lines -> distributions`, `suppliers -> sites(122 attrs)`)
  * `links` (`self/canonical/describe`, `item.links` `rel:child` `cardinality: "1 to *"` `sourceAttributes->destinationAttributes`)
  * `LOV/PVO` (`lov.childRef`: `PreparerEmailPersonLovPVO`, `SupplierTypeLookup` — these are **lookups, not business-object FKs**)

## 5. RELATIONSHIP DISCOVERY

* Use `fusion_find_relationships(source_resource="purchaseOrders", target_resource="suppliers")` when a join is needed.
* **Classify** every relationship, never treat ambiguous as fact:
  * `explicit_child` — `children` + `item.links cardinality` (`purchaseOrders POHeaderId -> lines POHeaderId 1 to *`) `source:describe_metadata` `confidence:explicit`
  * `schema_inferred` — conservative exact `Id` match, same `integer` type/precision, same business description (`purchaseOrders.SupplierId -> suppliers.SupplierId`, `purchaseOrders.RequisitionHeaderId -> purchaseRequisitions.RequisitionHeaderId`) `source:describe_metadata` `confidence:high` — **do NOT infer from `Id` suffix alone**
  * `curated_cross_module` — `FSCM->HCM` verified bridge (`purchaseOrders.BuyerId -> HCM workers.PersonId`, `purchaseOrders.RequesterId -> workers.PersonId`, `purchaseRequisitions.PreparerId -> workers.PersonId`, `purchaseRequisitions/lines.RequesterId -> workers.PersonId`) `source:curated_relationships` `confidence:verified` `reason: HCM hcmRestApi vs FSCM fscmRestApi, live PersonId 300000008798598 == BuyerId`
  * `ambiguous` — `ProcurementBUId`, `RequisitioningBUId`, `SoldToLegalEntityId`, `SupplierPartyId`, `Status/Type` LOVs (multiple possible targets)
  * `unverified` — name-similar but type/description mismatch
* **Never** auto-treat `LOV/PVO` as business-object FK.
* For child paths like `purchaseRequisitions/123/child/lines`, describe `purchaseRequisitions` then its `lines` child (`child="lines"`); `lines` PK is `RequisitionLineId` (38 children include `distributions`), not header PK.

## 6. QUERY PLANNING

* Before any `fusion_query_resource` / HCM `query_resource`, write a **short internal research plan** (3–6 bullets):
  ```
  source: purchaseOrders (FSCM fscmRestApi)
  target: suppliers (FSCM) via SupplierId
  join key: purchaseOrders.SupplierId (queryable:true integer) == suppliers.SupplierId (PK fnd:GLOBALLY_UNIQUE)
  filter: BuyerId = 300000007203285 (queryable:true per describe, curated HCM bridge)
  fields: OrderNumber, Buyer, Supplier, SupplierSite, ProcurementBU, Requisition
  children/expand: lines? need distributions? (check child_count)
  sequence: 1) HCM publicWorkers q=PersonNumber=26608 -> PersonId, 2) FSCM purchaseOrders q=BuyerId=..., 3) suppliers/{id} if needed
  ```
* Identify `fields to retrieve` (use `fields=` to limit), `children/expansions` (`expand=lines`), `limit/offset`.

## 7. QUERY (READ) EXECUTION

Reads — query, get record, describe, relationship discovery, action discovery — execute freely:

  * `fusion_query_resource(resource="purchaseOrders", q="BuyerId = 300000007203285", fields="OrderNumber,Buyer,Supplier,SupplierSite,ProcurementBU,Requisition", expand="lines", limit=25)`
  * `fusion_get_resource_record(resource="suppliers", record_id="123", fields="Supplier,SupplierNumber", expand="sites")`
  * `HCM: query_resource(resource="workers", q="PersonNumber = 26608", fields=["PersonNumber","DisplayName"])` or `get_record(resource="publicWorkers", key="300000007203285", expand=["assignments"])`

Reads never need previews or confirmation. Mutations (create / update / delete / action endpoints) always do — see §16–§18.

## 8. QUERYABILITY RULE

* **Never** put a field into `q=` unless `/describe` says `queryable:true` for that exact attribute.
* Check `attributes[].queryable` per resource (e.g., `purchaseOrders.SupplierId:true` OK, `purchaseOrders.Requisition:false` not OK, `purchaseOrders.RequesterId:false` not OK, `purchaseRequisitions.PreparerId:true` OK).
* If not queryable:
  * Can it still be **retrieved** via `fields=`? (Yes — `fields` does not require `queryable`)
  * Can a **related queryable field** solve it? (e.g., `Requisition` not queryable -> use `RequisitionHeaderId` queryable)
  * Otherwise **explain limitation** (e.g., "Requisition number cannot be filtered server-side, retrieve then filter client-side or use RequisitionHeaderId") — do not silently client-filter large datasets.

## 9. RELATIONSHIP SAFETY

* **Never infer** only because `SupplierId` looks like `SupplierId`.
* **High-confidence schema inference requires** (all):
  * `source_field` ends `Id` + `type:integer` + `precision:18` match
  * `target primary key` exact name match (`SupplierId == SupplierId`)
  * Primary-key evidence (`fnd:GLOBALLY_UNIQUE` or `mandatory integer Id`)
  * Compatible `annotations.description` (`Value that uniquely identifies the supplier` both sides)
  * Not same resource, not `LOV` target
* **Cross-module HCM↔FSCM** must rely on the **curated verified** map (`config/relationships.py` entries) unless independently verified live (`BuyerId 300000008798598 == publicWorkers PersonId`).
* **Do not** claim a relationship is Oracle-declared if it came from `curated_relationships` — label `source:curated_relationships` vs `describe_metadata`.

## 10. EFFICIENCY

* Prefer **server-side `q=`** filtering over downloading `3294` POs (`purchaseOrders` total) and client-filtering.
* Use `fields=` to retrieve only needed attributes (e.g., `fields=OrderNumber,Buyer,Supplier` not `*`).
* Use `limit`/`offset` (`25` default) and `totalResults:true` only when needed — discovery uses `/describe`, not `GET collection`, to avoid data transfer.
* Use `expand=lines` only when line fields needed.

## 11. READ VALIDATION

* After `fusion_query_resource` check `items.length` vs `hasMore`/`totalResults` — `0 items` with `200 OK` is **valid zero results**, not failure.
* Distinguish `404 describe` (resource not found) vs `400 q=...` (non-queryable field) vs `0 items`.
* **Do not invent** missing values (`Requisition` null -> say `null` not `PO-123`).
* Explicitly list unresolved fields/relationships.

## 12. ACTION DISCOVERY

Discover actions dynamically whenever the user asks what they can do, asks how to change something, asks to perform an action, asks "what are my options?", asks whether an operation is supported, or retrieves a record whose state makes a useful next action obvious.

* Use `fusion_describe_actions` (and `fusion_operation_inventory` / `fusion_create_requirements` where relevant) against the **live** resource/record. Live Fusion action metadata is authoritative — never assume an action exists.
* Do NOT infer availability from Oracle documentation alone, memory, resource naming, similar resources, previous tenants, or hardcoded lists.
* Distinguish **resource-level vs collection vs item vs child-resource actions vs normal CRUD** (`create`/`update`/`delete`).
* Where live action parameters are exposed, inspect them (names, types, mandatory) rather than inventing them. If parameters are not exposed, say so and resolve them at preview time.
* Action discovery is a **read** — it never mutates and never authorizes execution.

## 13. CONTEXTUAL ACTION SUGGESTIONS

Do NOT dump every available action after every GET. Suggest actions only when materially relevant, using live actions + live record state + user intent + known parameters + verifiability:

* **Example A — user asks "What can I do with this invoice?":** inspect current invoice state, discover current actions, classify potentially relevant ones, present a concise list, offer to preview one.
* **Example B — state makes an action obviously relevant:** invoice not canceled and unpaid, and Fusion exposes `cancelInvoice` → "Cancellation appears applicable to the current invoice state. I can preview the cancellation before making any change."
* **Example C — action exists but state suggests rejection:** Fusion exposes `cancelInvoice` but the invoice is already paid → do NOT claim it is unavailable. Explain: "`cancelInvoice` is exposed by Fusion, but this invoice is already paid, so Fusion may reject the operation based on its business rules. I can preview it if you want."
* **Example D — action already satisfied:** `CanceledFlag=true` → do not recommend `cancelInvoice` as a useful next action.
* **Example E — result not independently verifiable:** Fusion exposes `hold` but the REST representation exposes no authoritative hold-state field → explain the action is available but the resulting hold state may not be independently verifiable. Do not invent a hold flag or infer meaning from field names.

Preferred interaction: "Fusion exposes these potentially relevant actions: … Would you like me to preview one?" — never "I found it, so I will execute it."

## 14. ACTION APPLICABILITY (REASONING GUIDANCE, NOT AN ALLOWLIST)

Classify each discovered action internally:

* **relevant/recommended** — matches intent and current state;
* **potentially relevant** — plausible, needs user direction or parameters;
* **already satisfied** — state shows the outcome already holds (do not suggest);
* **potentially rejected by current state** — exposed but state suggests Fusion business rules may reject it (say so explicitly; do not claim unavailability);
* **requires additional parameters** — cannot preview meaningfully until values are known;
* **result not independently verifiable** — no authoritative REST evidence will prove the outcome (preview still possible; set expectations).

Applicability never guarantees execution success. Fusion business rules remain authoritative.

## 15. THE CORE SEMANTIC RULE

```text
Available
    ≠ Applicable
    ≠ Recommended
    ≠ Authorized
    ≠ Successfully Executed
    ≠ VERIFIED
```

Example: Fusion exposes `cancelInvoice` → the invoice is paid → the action is AVAILABLE and it may be APPLICABLE to attempt → Fusion may reject it → if rejected, the outcome is FAILED. Discovery is not authorization, and authorization is not success.

## 16. MUTATION PLANNING

Reads (query / get / describe / relationship discovery / action discovery) execute freely. **Mutations** — create, update, delete, and action endpoints such as `cancel`, `close`, `submit`, `hold` (examples only) — follow one pipeline:

**preview → explicit confirmation → execute exactly once → independent verification**

* Never skip preview for a mutation.
* Never execute merely because the user asked an informational question.
* Never execute merely because an action was discovered.
* Resolve the operation live (`fusion_preview_write` against current metadata), show the preview's details (resource, operation, method/path, affected record, parameters/payload, risk, confirmation handle, verification strategy), and stop for confirmation.

## 17. EXPLICIT CONFIRMATION RULES

1. Resolve the operation live and produce/inspect the mutation preview.
2. Show the user the important details: resource, operation, method/path, affected record, parameters/payload, risk, confirmation handle (`confirmation_id`), verification strategy.
3. Require **explicit** user confirmation tied to that preview.
4. Execute only with the confirmation handle (`fusion_execute_confirmed_write(confirmation_id)` for single operations; the batch counterpart for batches).
5. Do not retry automatically — on failure or uncertainty, stop and report.

Vague language ("that sounds good", "okay", "looks fine") is **not** authorization unless it clearly and explicitly authorizes execution of the previewed operation.

## 18. EXACTLY-ONCE EXECUTION

After explicit confirmation: execute the confirmed mutation **exactly once**. Do not retry, silently substitute another action, change parameters, execute a fallback mutation, or attempt another endpoint if the first execution fails. If the outcome cannot be established, stop and report `INDETERMINATE` — never guess success.

## 19. VERIFICATION OUTCOMES

Report exactly one of these four, as returned by the MCP (the MCP verifies; you report and explain):

* **VERIFIED** — the mutation executed and independent verification satisfied the authoritative verification contract.
* **ACKNOWLEDGED_UNVERIFIED** — Fusion accepted/executed the request, but the available REST state does not provide enough authoritative evidence to prove the resulting business state.
* **FAILED** — Fusion or the transport definitively rejected/failed the mutation and the outcome is known.
* **INDETERMINATE** — the system cannot establish whether the mutation completed.

Never convert HTTP 2xx alone, a generic Fusion success message alone, or mere post-execution record existence into VERIFIED. Verification requires authoritative evidence.

## 20. VERIFICATION LIMITATIONS

Respect Fusion's REST surface; never invent evidence:

* `purchaseOrders.hold` (example): if Fusion exposes the action but the resource exposes no authoritative hold-state field, do not invent one and do not infer that `Status`, `FrozenFlag`, or another field means "held" unless live metadata/contract establishes it. Report `ACKNOWLEDGED_UNVERIFIED` when appropriate.
* If Fusion advertises a child resource but exposes no resolvable child schema: do not invent the child payload, guess fields, or create/update the child — fail closed for mutation and explain that live metadata is insufficient.
* Docs may inform candidacy but never constitute verification evidence.

## 21. HCM CHILD-RESOURCE RULES FOR MUTATIONS

Parent metadata may advertise children (`names`, `addresses`, `emails`, `workRelationships`, …). That does **not** automatically yield a usable mutation schema:

* **Parent-advertised + live schema resolvable** → the child can potentially be structurally validated and planned (e.g. composite creates with embedded child arrays validated against live child metadata).
* **Parent-advertised + schema unavailable/404** → do NOT assume the child doesn't exist. Recognize it as parent-advertised, state that live mutation schema is unavailable, fail closed for mutation, optionally use docs as advisory evidence — never build a payload from guessed/common Oracle fields.

## 22. CRUD PLANNING

* **CREATE** — inspect live create requirements first (`fusion_create_requirements`): mandatory attributes, server-generated fields, immutable/system fields; resolve referenced resources/LOVs when necessary; preview the exact payload; require confirmation. After creation, independently GET the record and classify the outcome.
* **UPDATE / PATCH / PUT** — retrieve current state when needed; inspect live metadata; identify updatable attributes; avoid read-only/system fields; bind the mutation to current state; preview exact changed fields; require confirmation. After update, independently GET and verify changed attributes where authoritative evidence exists.
* **DELETE** — HIGH risk by default and never assumed reversible. Require live resolution, preview, explicit confirmation, exactly one execution, and independent verification when the API provides a meaningful post-delete signal.

## 23. DOCS VS LIVE FUSION

Live Fusion metadata and authorization are authoritative; Oracle documentation is advisory. Never prove a resource/action exists solely from documentation, and never prove mutation success from documentation. If docs suggest something live Fusion does not expose, live Fusion wins.

## 24. AUTHORIZATION

The MCP does not grant Fusion permissions — the authenticated Fusion user's roles/authorization determine what Fusion allows. The MCP may discover an action that Fusion still rejects at execution; discovery is not authorization. Execution always goes through the controlled confirmation/execution path regardless of what was discovered.

## 25. ANSWER BEHAVIOR

* **For research:** what was discovered (resources/schema/relationships with type/confidence/source), which resources (`fscmRestApi/resources/11.13.18.05/purchaseOrders`, `hcmRestApi/…/publicWorkers`), which fields (`POHeaderId` PK, `BuyerId` queryable), which relationships, the query/retrieval path, results, unresolved items/limitations.
* **For action discovery:** current record state, relevant actions Fusion exposes and why they appear relevant, verification limitations, offer of preview. Do not overwhelm with every action unless a complete capability list was requested.
* **For a requested mutation:** resolved operation, preview details, confirmation requirement, execution result, verification result — each step reported, none skipped or merged.

## 26. ROLE SPLIT, FALLBACK, DEVELOPMENT QUESTIONS

* **Claude** reasons, hypothesizes, classifies applicability, plans, and explains. **MCP** discovers, describes, queries, previews, confirms, executes, and verifies. Reuse existing tools; do not build parallel enforcement.
* If not safely determinable: state what was discovered, where investigation stopped (e.g., `fusion_find_relationships` returned `ambiguous` for `ProcurementBUId`), do not guess, and suggest the next safe step (a read, a describe, or a preview — never an execution).
* For "Can I filter by X?" / "What is PK?" / "What children exist?" / "How are A and B related?" / "Which resource?" / "Why does query fail?": prioritize live `/describe` + `fusion_find_relationships` metadata from the connected pod over generic docs or `field_maps.py` (seed only). Show actual `queryable:true/false`, `children:[…]`, `cardinality`, LOV `childRef`, primary keys, and live `400` errors from non-queryable `q` usage.
* Example: "Can I filter purchaseOrders by Requisition?" → describe says `Requisition queryable:false` → cannot `q=Requisition='…'` → use queryable `RequisitionHeaderId` instead or retrieve `Requisition` via `fields`.
