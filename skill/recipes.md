<!-- Verified notes about THIS pod. The model reads this file on every question, so keep it short and true.
     RULE: a note goes in only after you have seen it work, and it ends with a parenthesis saying how it was checked,
     for example (Checked with scripts/describe_probe.py ...) or (Seen in scenario S10 ...). A test refuses a note without one.
     Better still: add a scenario that exercises the note (S10 for holds, S16 for ledgers) and compare with and without the notes:
     python scripts/run_all.py --only S10,S16 --repeat 3 --skip-tests --no-recipes -->
- Invoices on hold: the invoice record has the attribute ActiveHoldFlag. Use fusion_query_resource on invoices with a
  filter on ActiveHoldFlag. There is no child resource called invoiceHolds or holds, and HoldFlag is not an invoice
  field (it exists only on the invoiceInstallments child). Before filtering, read 3 rows with fields
  InvoiceId,ActiveHoldFlag and limit 3 to see how the flag is written, then filter with that same value.
  To count, ask for totalResults with limit 1. (Checked with scripts/describe_probe.py on the invoices description; scenario S10 passed with it.)
- The description of invoices is about 41,000 tokens, too large to read: do not describe it. Child resources and actions
  can be listed with fusion_find_relationships (source_resource invoices); real children include invoiceLines,
  invoiceInstallments, invoiceDistributions and attachments. (Measured with scripts/describe_probe.py: 163,737 characters.)
- HR absences: this pod rejects order_by on absences, so read a date window and sort the rows yourself. Dates in q are
  written bare, like startDate >= 2025-01-01, not in quotes. (Seen in the pod's own error messages and in a working run.)
- The HR resource emps returns 403 for this account: do not use it. Worker names come from query_resource on workers
  (fields PersonNumber,DisplayName). (Seen in a run for emps; names from workers worked in scenario S9.)
- Ledgers: the typed tool list_ledgers fails with 400 Bad Request on this pod, because the fields it hard-codes are not all
  valid here. Do not use it. Describe ledgersLOV (it is small), then query it with fusion_query_resource using only
  the attribute names the describe shows. (The failing request was read from the MCP's source and seen in five runs; the way
  around it is not yet confirmed: scenario S16 tests it.)
- Typed searches return wide rows: each invoice from search_invoices is about 1 KB, so limit 50 gave 61,152 characters and the
  engine refused it as too large. Ask for 25 rows or fewer, or use fusion_query_resource with fields. (Seen in scenario S7: 61,152 characters were refused.)
