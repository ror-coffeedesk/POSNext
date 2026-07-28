# Copyright (c) 2026, Coffeedesk
# For license information, please see license.txt
import frappe
from frappe import _
from frappe.utils import flt, cint, nowdate, get_first_day, get_last_day, add_days


def _get_warehouse(pos_profile):
	if not pos_profile:
		return None
	return frappe.db.get_value("POS Profile", pos_profile, "warehouse")


@frappe.whitelist()
def get_daily_sales(pos_profile=None, date=None):
	"""Today's (or given date's) total sales, invoice count, avg ticket."""
	date = date or nowdate()
	conditions = "si.docstatus = 1 AND si.posting_date = %(date)s"
	values = {"date": date}
	if pos_profile:
		conditions += " AND si.pos_profile = %(pos_profile)s"
		values["pos_profile"] = pos_profile

	result = frappe.db.sql(
		f"""
		SELECT
			COUNT(DISTINCT si.name) AS invoice_count,
			COALESCE(SUM(si.grand_total), 0) AS total_sales,
			COALESCE(SUM(si.grand_total) / NULLIF(COUNT(DISTINCT si.name), 0), 0) AS avg_ticket
		FROM `tabSales Invoice` si
		WHERE {conditions}
		""",
		values,
		as_dict=True,
	)
	return result[0] if result else {"invoice_count": 0, "total_sales": 0, "avg_ticket": 0}


@frappe.whitelist()
def get_month_sales(pos_profile=None):
	"""Month-to-date total plus a day-by-day trend for the current month."""
	from_date = get_first_day(nowdate())
	to_date = nowdate()
	conditions = "si.docstatus = 1 AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s"
	values = {"from_date": from_date, "to_date": to_date}
	if pos_profile:
		conditions += " AND si.pos_profile = %(pos_profile)s"
		values["pos_profile"] = pos_profile

	totals = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(si.grand_total), 0) AS total_sales,
			COUNT(DISTINCT si.name) AS invoice_count
		FROM `tabSales Invoice` si
		WHERE {conditions}
		""",
		values,
		as_dict=True,
	)[0]

	trend = frappe.db.sql(
		f"""
		SELECT si.posting_date AS date, COALESCE(SUM(si.grand_total), 0) AS total_sales
		FROM `tabSales Invoice` si
		WHERE {conditions}
		GROUP BY si.posting_date
		ORDER BY si.posting_date
		""",
		values,
		as_dict=True,
	)
	return {"total_sales": totals.total_sales, "invoice_count": totals.invoice_count, "trend": trend}


@frappe.whitelist()
def get_group_wise_sales(pos_profile=None, from_date=None, to_date=None):
	"""Sales broken down by Item Group, for a date range (defaults to today)."""
	from_date = from_date or nowdate()
	to_date = to_date or nowdate()
	conditions = "si.docstatus = 1 AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s"
	values = {"from_date": from_date, "to_date": to_date}
	if pos_profile:
		conditions += " AND si.pos_profile = %(pos_profile)s"
		values["pos_profile"] = pos_profile

	return frappe.db.sql(
		f"""
		SELECT
			i.item_group,
			COALESCE(SUM(sii.qty), 0) AS qty_sold,
			COALESCE(SUM(sii.amount), 0) AS total_sales
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		INNER JOIN `tabItem` i ON i.name = sii.item_code
		WHERE {conditions}
		GROUP BY i.item_group
		ORDER BY total_sales DESC
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def get_item_wise_sales(pos_profile=None, from_date=None, to_date=None, limit=20):
	"""Top-selling items by revenue, for a date range (defaults to today)."""
	from_date = from_date or nowdate()
	to_date = to_date or nowdate()
	conditions = "si.docstatus = 1 AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s"
	values = {"from_date": from_date, "to_date": to_date, "limit": cint(limit)}
	if pos_profile:
		conditions += " AND si.pos_profile = %(pos_profile)s"
		values["pos_profile"] = pos_profile

	return frappe.db.sql(
		f"""
		SELECT
			sii.item_code,
			sii.item_name,
			i.item_group,
			COALESCE(SUM(sii.qty), 0) AS qty_sold,
			COALESCE(SUM(sii.amount), 0) AS total_sales
		FROM `tabSales Invoice Item` sii
		INNER JOIN `tabSales Invoice` si ON si.name = sii.parent
		INNER JOIN `tabItem` i ON i.name = sii.item_code
		WHERE {conditions}
		GROUP BY sii.item_code
		ORDER BY total_sales DESC
		LIMIT %(limit)s
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def get_payment_mode_breakdown(pos_profile=None, date=None):
	"""Sales split by payment mode (cash/card/etc), for a given date (defaults to today)."""
	date = date or nowdate()
	conditions = "si.docstatus = 1 AND si.posting_date = %(date)s"
	values = {"date": date}
	if pos_profile:
		conditions += " AND si.pos_profile = %(pos_profile)s"
		values["pos_profile"] = pos_profile

	return frappe.db.sql(
		f"""
		SELECT sip.mode_of_payment, COALESCE(SUM(sip.amount), 0) AS total_amount
		FROM `tabSales Invoice Payment` sip
		INNER JOIN `tabSales Invoice` si ON si.name = sip.parent
		WHERE {conditions}
		GROUP BY sip.mode_of_payment
		ORDER BY total_amount DESC
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def get_dashboard_summary(pos_profile=None):
	"""Single call bundling everything the dashboard needs on open — avoids
	5 separate round trips when the dialog first loads."""
	return {
		"daily": get_daily_sales(pos_profile),
		"month": get_month_sales(pos_profile),
		"group_wise": get_group_wise_sales(pos_profile),
		"item_wise": get_item_wise_sales(pos_profile, limit=10),
		"payment_modes": get_payment_mode_breakdown(pos_profile),
	}