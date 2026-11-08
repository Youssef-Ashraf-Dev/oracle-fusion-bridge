<!-- Verified notes about THIS pod. The model reads this file on every question, so keep it short and true.
     RULE: a note goes in only after you have seen it work, and it ends with a parenthesis saying how it was checked,
     for example (Checked with devtools/describe_probe.py ...) or (Seen in scenario S10 ...). A test refuses a note without one.
     Better still: add a scenario that exercises the note (S10 for holds, S16 for ledgers) and compare with and without the notes:
     python devtools/run_all.py --only S10,S16 --repeat 3 --skip-tests --no-recipes -->
- Invoices on hold: the invoice attribute is ActiveHoldFlag. Use fusion_query_resource on invoices with a filter on it. There is no child
  resource invoiceHolds or holds; HoldFlag exists only on invoiceInstallments. Before filtering, read 3 rows (fields
  InvoiceId,ActiveHoldFlag, limit 3) to see how the flag is written, then filter with that value. To count, ask for totalResults with limit 1.
  (Checked with devtools/describe_probe.py on the invoices description; scenario S10 passed with it.)
- The invoices description is about 41,000 tokens: do not describe it. List children and actions with fusion_find_relationships
  (source_resource invoices); real children: invoiceLines, invoiceInstallments, invoiceDistributions, attachments.
  (Measured with devtools/describe_probe.py: 163,737 characters.)
- Filters, HR and finance: conditions on different attributes combine (join with ";"), but two on the SAME attribute collapse silently to the
  last one, and so does a typed search given both date_from and date_to; the bridge refuses these. Send ONE condition per attribute: the lower
  bound plus other attributes, then drop rows past the upper bound yourself. To COUNT a window, make two calls (limit 1, totals) and subtract
  total(X >= FROM) minus total(X >= the day after TO). (Measured with devtools/query_probe.py: absences 2026-11-04; invoices 2026-11-05,
  where InvoiceDate >= A;InvoiceDate <= B gave the total of <= B alone.)
- HR absences: sort on the server with order_by startDate:desc (other spellings give a 400): "the latest N" is ONE call with limit N and
  q startDate <= today (planned leave is dated in the future) unless asked about planned leave. Dates are bare. Fields that work: personAbsenceEntryId, personNumber, absenceType, startDate, endDate, absenceDispStatus; AbsenceId,
  PersonNumber and StartDate give a 400, and one bad name fails the whole call. (Seen with devtools/field_probe.py
  and query_probe.py, 2026-11-04/05; the pod holds absences dated up to 2026-12-31.)
- The HR resource emps returns 403 here: do not use it. Worker names come from query_resource on workers (fields
  PersonNumber,DisplayName). (Seen in a run; names from workers worked in scenario S9.)
- Ledgers: the typed tool list_ledgers fails with 400 on this pod (the fields it hard-codes are not all valid). Do not use it. Describe
  ledgersLOV, then query it with fusion_query_resource using only the attribute names the describe shows. (Seen in five runs;
  the way around it works: scenario S16 passed with it.)
- Typed searches return wide rows (about 1 KB per invoice): limit 50 gave 61,152 characters, refused by the engine. Ask for
  25 rows or fewer, or use fusion_query_resource with fields. (Seen in scenario S7.)
- Looking up names: a query on workers that joins people with or returns ONLY THE FIRST person (10 numbers in one query gave 1, batches of 5
  gave 2, one per call gave 10 of 10). Ask for one person per call (q PersonNumber = '<number>', fields PersonNumber,DisplayName); the bridge
  refuses an or lookup on workers. (Checked twice with devtools/lookup_probe.py on this pod.)
