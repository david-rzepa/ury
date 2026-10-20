# Future native closing regression

This draft is based on URY fa1aca8a725ccdaee0eaa9eb32392b6daf4ea59d.
It does not change the current deployment or its pinned input. The observed
closing had no transaction children and produced no merge log, Sales Invoice,
or GL entries. A Submitted closing alone is not accounting acceptance.

The before_validate hook derives the shift window, invoice references, totals,
taxes and expected payments on both draft creation and submission. It requires
the authenticated cashier's submitted Open opening and matching profile/company.
Only counted payment amounts are retained from the UI; invoice references and
financial totals are replaced. Locked current reads include invoice children;
normal native document permissions and the existing URY multi-cashier hooks stay
in effect. Opening-row locking makes a second close re-read the Closed status.
The server end time also supplies the posting date and time.

The builder arithmetic follows ERPNext
4cea6a7f839286b69dd30b38b0991e6ae234dac5. Test fixtures preserve the exact
upstream GPLv3 source files and hashes, with their original copyright notices:

- https://github.com/frappe/erpnext/blob/4cea6a7f839286b69dd30b38b0991e6ae234dac5/erpnext/accounts/doctype/pos_closing_entry/pos_closing_entry.py
- https://github.com/frappe/erpnext/blob/4cea6a7f839286b69dd30b38b0991e6ae234dac5/erpnext/accounts/doctype/pos_invoice_merge_log/pos_invoice_merge_log.py

Run the database-free regressions with:

```
python3 -B -m unittest discover -s tests -p test_server_closing_unit.py
```

They execute the actual hook/builder and pinned native query/arithmetic and
submission/dispatch functions with database boundaries replaced. The exact
STG00002/STG00003 fixture IDs each carry 25 MXN and quantity 1, and must become
two reference rows, total 50, quantity 2, with synchronous consolidation dispatch.
These are regression fixtures, not a replay or mutation of the real invoices.
Tests cover identity/permissions, company/profile/period filters, injected rows
and totals, payment declarations and cancellation while acquiring invoice locks.
They do not establish database isolation behavior, SI/GL posting, or complete
multi-cashier accounting acceptance.

Before accepting a fresh deployment, use its new native image input and normal
UI to open a shift, create and pay two distinct MXN invoices, then close once.
Record both actual invoice IDs and verify exactly those transaction children,
totals, reconciliation and Closed opening. Verify native submitted merge logs,
each invoice's consolidated_invoice link, submitted linked Sales Invoice(s),
and balanced nonempty GL entries for the correct company/currency. Exercise
permission refusal, concurrent/repeated closing, and native accounting failure;
do not accept a Failed closing or treat the underlying native finally-commit
behavior as a transaction rollback guarantee. Multi-cashier behavior requires its
own fresh native regression, including retained custom counted amounts and the
existing sub-cashier closing checks. No current invoice should be repaired by
this test or draft.
