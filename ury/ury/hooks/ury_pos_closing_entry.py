import frappe
from frappe.utils import get_datetime, flt
from math import isfinite


def populate_server_closing(doc, method=None):
    """Derive accounting inputs on draft creation and submission, never from UI totals.

    Keep native create/submit permissions and the existing URY closing hooks.
    Submitted historical documents and cancellation retain their native lifecycle.
    """
    previous = doc.get_doc_before_save()
    if doc.docstatus == 2 or (previous and previous.docstatus == 1):
        return
    opening = frappe.get_doc("POS Opening Entry", doc.pos_opening_entry, for_update=True)
    profile = frappe.get_doc("POS Profile", opening.pos_profile)
    opening.check_permission("read")
    profile.check_permission("read")
    if (opening.docstatus != 1 or opening.status != "Open"
            or opening.user != frappe.session.user or doc.user != opening.user
            or doc.pos_profile != opening.pos_profile or doc.company != opening.company
            or profile.company != opening.company
            or get_datetime(opening.period_start_date) > get_datetime()):
        frappe.throw("Closing must match the current cashier and open shift.")

    declared = {}
    for row in doc.payment_reconciliation:
        if row.mode_of_payment in declared:
            frappe.throw("Duplicate closing payment mode.")
        try:
            values = (float(row.closing_amount or 0), float(row.custom_closing_amount or 0))
        except (TypeError, ValueError):
            frappe.throw("Invalid counted payment amount.")
        if not all(isfinite(value) for value in values):
            frappe.throw("Invalid counted payment amount.")
        declared[row.mode_of_payment] = values

    calculated = build_server_closing(opening)
    modes = {row.mode_of_payment for row in calculated.payment_reconciliation}
    if len(modes) != len(calculated.payment_reconciliation) or modes != set(declared):
        frappe.throw("Payment modes changed; reload the closing preview.")
    for row in calculated.payment_reconciliation:
        row.closing_amount, row.custom_closing_amount = declared[row.mode_of_payment]
        row.difference = row.closing_amount - row.expected_amount
    for field in ("period_start_date", "period_end_date", "grand_total", "net_total",
                  "total_quantity", "pos_transactions", "taxes", "payment_reconciliation"):
        doc.set(field, calculated.get(field))
    doc.posting_date = get_datetime(calculated.period_end_date).date()
    doc.posting_time = get_datetime(calculated.period_end_date).time()


def before_save(doc, method):
    sub_pos_close_check(doc, method)

def validate(doc, method):
    calculate_closing_amount(doc, method)
    validate_cashier(doc, method)


def sub_pos_close_check(doc,method):
    cashier = None
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:
        get_cashier = frappe.get_doc("POS Profile", doc.pos_profile)
        for user_details in get_cashier.applicable_for_users:
            if not user_details.custom_main_cashier:
                cashier = user_details.user
        if frappe.session.user != cashier:
            branch=frappe.db.get_value("POS Profile",doc.pos_profile,"branch")
            pos_opening_list = frappe.get_all(
                "POS Opening Entry",
                fields=["name", "docstatus", "status", "posting_date"],
                filters={"branch": branch,"user":cashier},
            )
            flag = 0
            for pos_opening in pos_opening_list:
                if pos_opening.status == "Open" and pos_opening.docstatus == 1:
                    flag = 1
            if flag == 1:
                frappe.throw(("Sub Cashier POS  must be closed"), title=("Sub Cashier POS Closing Required"))
                
            return flag
    else:
        pass

def calculate_closing_amount(doc, method):
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:  
        sub_pos_closing = frappe.get_all(
            "Sub POS Closing",
            filters=[
                ["posting_date", "<=", doc.posting_date],
                ["period_start_date", ">=", doc.period_start_date],
                ["docstatus", "=", 1]
            ],
            fields=["name"] 
        )
        if sub_pos_closing:
            for closing_details in doc.payment_reconciliation:
                sub_closing_amount = frappe.db.get_value("Sub POS Closing Payment",{"parent":sub_pos_closing[0].name,"mode_of_payment":closing_details.mode_of_payment},"closing_amount") or 0
                main_closing_amount = closing_details.custom_closing_amount or 0
                total_closing_amount = sub_closing_amount + main_closing_amount
                closing_details.closing_amount = total_closing_amount
                closing_details.difference = total_closing_amount - closing_details.expected_amount
        else:
            frappe.throw("No Sub POS Closing entries found between the given dates")
            return None
    else:
        pass
def validate_cashier(doc, method):
    cashier = None
    multiple_cashier = frappe.db.get_value("POS Profile",doc.pos_profile,"custom_enable_multiple_cashier")
    if multiple_cashier:
        get_cashier = frappe.get_doc("POS Profile", doc.pos_profile)
        for user_details in get_cashier.applicable_for_users:
            if not user_details.custom_main_cashier:
                cashier = user_details.user
        if frappe.session.user == cashier:
            frappe.throw("Sub Cashiers are not allowed to make POS Closing Entries.")
    else:
        pass


# Arithmetic follows ERPNext 4cea6a7 make_closing_entry_from_opening (GPLv3).
# Locked current reads replace its snapshot query; all totals remain server-derived.
def build_server_closing(opening_entry):
    closing_entry = frappe.new_doc("POS Closing Entry")
    closing_entry.pos_opening_entry = opening_entry.name
    closing_entry.period_start_date = opening_entry.period_start_date
    closing_entry.period_end_date = frappe.utils.get_datetime()
    closing_entry.pos_profile = opening_entry.pos_profile
    closing_entry.user = opening_entry.user
    closing_entry.company = opening_entry.company
    closing_entry.grand_total = 0
    closing_entry.net_total = 0
    closing_entry.total_quantity = 0

    frappe.has_permission("POS Invoice", throw=True)
    rows = frappe.db.sql(
        """SELECT name FROM `tabPOS Invoice`
        WHERE owner=%s AND docstatus=1 AND pos_profile=%s
        AND IFNULL(consolidated_invoice, '')=''
        AND TIMESTAMP(posting_date, posting_time) BETWEEN %s AND %s
        ORDER BY name FOR UPDATE""",
        (opening_entry.user, opening_entry.pos_profile,
         closing_entry.period_start_date, closing_entry.period_end_date), as_dict=True,
    )
    invoices = []
    for row in rows:
        invoice = frappe.get_doc("POS Invoice", row.name, for_update=True)
        invoice.check_permission("read")
        timestamp = get_datetime(str(invoice.posting_date) + " " + str(invoice.posting_time))
        profile = frappe.get_doc("POS Profile", opening_entry.pos_profile)
        if (invoice.docstatus != 1 or invoice.consolidated_invoice
                or invoice.owner != opening_entry.user or invoice.cashier != opening_entry.user
                or invoice.pos_profile != opening_entry.pos_profile
                or invoice.company != opening_entry.company or invoice.branch != profile.branch
                or not get_datetime(closing_entry.period_start_date) <= timestamp
                <= get_datetime(closing_entry.period_end_date)):
            frappe.throw("Ineligible POS Invoice in closing.")
        invoices.append(invoice)

    pos_transactions = []
    taxes = []
    payments = []
    for detail in opening_entry.balance_details:
        payments.append(
            frappe._dict(
                {
                    "mode_of_payment": detail.mode_of_payment,
                    "opening_amount": detail.opening_amount,
                    "expected_amount": detail.opening_amount,
                }
            )
        )

    for d in invoices:
        pos_transactions.append(
            frappe._dict(
                {
                    "pos_invoice": d.name,
                    "posting_date": d.posting_date,
                    "grand_total": d.grand_total,
                    "customer": d.customer,
                }
            )
        )
        closing_entry.grand_total += flt(d.grand_total)
        closing_entry.net_total += flt(d.net_total)
        closing_entry.total_quantity += flt(d.total_qty)

        for t in d.taxes:
            existing_tax = [tx for tx in taxes if tx.account_head == t.account_head and tx.rate == t.rate]
            if existing_tax:
                existing_tax[0].amount += flt(t.tax_amount)
            else:
                taxes.append(
                    frappe._dict({"account_head": t.account_head, "rate": t.rate, "amount": t.tax_amount})
                )

        for p in d.payments:
            existing_pay = [pay for pay in payments if pay.mode_of_payment == p.mode_of_payment]
            if existing_pay:
                existing_pay[0].expected_amount += flt(p.amount)
            else:
                payments.append(
                    frappe._dict(
                        {
                            "mode_of_payment": p.mode_of_payment,
                            "opening_amount": 0,
                            "expected_amount": p.amount,
                        }
                    )
                )

    closing_entry.set("pos_transactions", pos_transactions)
    closing_entry.set("payment_reconciliation", payments)
    closing_entry.set("taxes", taxes)

    return closing_entry
