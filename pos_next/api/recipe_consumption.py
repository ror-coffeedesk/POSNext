"""
Recipe-based ingredient consumption for POS Next, run ONCE per POS Closing Shift.

Flow (all driven by the POS Closing Shift submit):

1. on_closing_shift_submit  -> marks the shift "Pending" and queues a background job
   (after the closing shift transaction commits, so a closing never fails because of stock).
2. process_closing_shift    -> the job:
     - collects the shift's submitted, non-return invoices
     - keeps only items in the POS Profile "Consumable Item Group" (and its children)
     - explodes each item's Default BOM and aggregates ingredients across ALL invoices
     - adds the "Additional Wastage" rows typed in the closing popup (item, qty, reason)
     - ONE Stock Entry (Material Issue, type "POS BOM Consumption") for stock ingredients,
       issued from the POS Profile "Consumable Warehouse"; every row carries an HTML
       breakdown (invoice / drink / qty sold / qty used)
     - ONE Journal Entry for non-stock ingredients (e.g. water) at their fixed rate
       (see NOTE below), only if the POS Profile has the two accounts configured
     - marks the shift "Processed" only after both documents are submitted
3. on_closing_shift_cancel  -> cancels the linked Stock Entry / Journal Entry.

NOTE on "Additional Costs": ERPNext wipes the Additional Costs table of any Stock Entry that
has no incoming (target warehouse) row (stock_entry.py: distribute_additional_costs), and a
Material Issue never has one. So non-stock cost cannot ride on the Material Issue; it is
posted as a Journal Entry linked to the same closing shift instead.
"""

from collections import OrderedDict

import frappe
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	escape_html,
	flt,
	get_link_to_form,
	getdate,
	now_datetime,
	nowdate,
	nowtime,
	strip_html_tags,
)

STOCK_ENTRY_TYPE = "POS BOM Consumption"
DONE_STATUSES = ("Processed", "Nothing to Consume", "Not Applicable")
JOB = "pos_next.api.recipe_consumption.process_closing_shift"


# --------------------------------------------------------------------------------------
# Hooks
# --------------------------------------------------------------------------------------


def on_closing_shift_submit(doc, method=None):
	"""POS Closing Shift on_submit: queue the consumption job."""
	group = frappe.get_cached_value("POS Profile", doc.pos_profile, "custom_item_group_drinks")
	if not group and not (doc.get("custom_wastage") or []):
		# retail-only profile and no wastage: nothing to do
		doc.db_set("custom_consumption_status", "Not Applicable", update_modified=False)
		return

	doc.db_set("custom_consumption_status", "Pending", update_modified=False)
	_enqueue(doc.name)


def on_closing_shift_cancel(doc, method=None):
	"""POS Closing Shift on_cancel: cancel what the consumption created."""
	for doctype in ("Journal Entry", "Stock Entry"):
		for name in frappe.get_all(
			doctype, filters={"custom_pos_closing_shift": doc.name, "docstatus": 1}, pluck="name"
		):
			linked = frappe.get_doc(doctype, name)
			linked.flags.ignore_permissions = True
			linked.cancel()

	doc.db_set(
		{
			"custom_consumption_status": "Cancelled",
			"custom_consumption_stock_entry": None,
			"custom_consumption_journal_entry": None,
		},
		update_modified=False,
	)


def validate_wastage(doc, method=None):
	"""POS Closing Shift validate: every wastage row needs a stock item, a positive qty and a reason."""
	for row in doc.get("custom_wastage") or []:
		item = frappe.db.get_value(
			"Item", row.item_code, ["item_name", "stock_uom", "is_stock_item", "disabled"], as_dict=True
		)
		if not item or item.disabled:
			frappe.throw(_("Wastage row {0}: item {1} not found or disabled.").format(row.idx, row.item_code))
		if not cint(item.is_stock_item):
			frappe.throw(_("Wastage row {0}: {1} is not a stock item.").format(row.idx, row.item_code))
		if flt(row.qty) <= 0:
			frappe.throw(_("Wastage row {0}: quantity must be greater than zero.").format(row.idx))
		if not (row.reason or "").strip():
			frappe.throw(_("Wastage row {0}: a reason is required.").format(row.idx))
		row.item_name = item.item_name
		row.stock_uom = item.stock_uom


@frappe.whitelist()
def search_wastage_items(txt, limit=15):
	"""Item search for the closing popup's wastage table (stock items only; cashiers need no Item access)."""
	txt = (txt or "").strip()
	if len(txt) < 2:
		return []
	like = f"%{txt}%"
	return frappe.get_all(
		"Item",
		filters={"is_stock_item": 1, "disabled": 0, "has_variants": 0},
		or_filters=[["item_code", "like", like], ["item_name", "like", like]],
		fields=["item_code", "item_name", "stock_uom"],
		order_by="item_name",
		limit=cint(limit) or 15,
	)


def retry_pending():
	"""Hourly: re-queue shifts left 'Pending' (for example if a worker was down)."""
	cutoff = add_to_date(now_datetime(), minutes=-30)
	for name in frappe.get_all(
		"POS Closing Shift",
		filters={"docstatus": 1, "custom_consumption_status": "Pending", "modified": ["<", cutoff]},
		pluck="name",
		limit=20,
	):
		_enqueue(name)


@frappe.whitelist()
def retry_closing_shift(closing_shift):
	"""Button on the POS Closing Shift form for 'Failed' / 'Pending' shifts."""
	frappe.has_permission("Stock Entry", "create", throw=True)
	status = frappe.db.get_value("POS Closing Shift", closing_shift, "custom_consumption_status")
	if status not in ("Failed", "Pending"):
		frappe.throw(_("Consumption status is {0}; nothing to retry.").format(status))
	frappe.db.set_value(
		"POS Closing Shift", closing_shift, "custom_consumption_status", "Pending", update_modified=False
	)
	_enqueue(closing_shift)


def _enqueue(closing_shift):
	frappe.enqueue(
		JOB,
		queue="long",
		timeout=1800,
		job_id=f"recipe_consumption::{closing_shift}",
		deduplicate=True,
		enqueue_after_commit=True,
		closing_shift=closing_shift,
	)


# --------------------------------------------------------------------------------------
# The job
# --------------------------------------------------------------------------------------


def process_closing_shift(closing_shift, raise_errors=False):
	"""The job. raise_errors=True is for the console: the traceback is shown instead of only logged."""
	shift = None
	savepoint = "recipe_consumption"
	savepoint_set = False
	try:
		# serialise runs for one shift: a second job waits here, then sees the final status
		frappe.db.sql("select name from `tabPOS Closing Shift` where name = %s for update", closing_shift)

		shift = frappe.get_doc("POS Closing Shift", closing_shift)
		if shift.docstatus != 1 or shift.get("custom_consumption_status") in DONE_STATUSES:
			return

		frappe.db.savepoint(savepoint)
		savepoint_set = True
		outcome = _consume(shift)

		# only now, after every document is submitted, is the shift marked processed
		shift.db_set(
			{
				"custom_consumption_status": outcome["status"],
				"custom_consumption_stock_entry": outcome.get("stock_entry"),
				"custom_consumption_journal_entry": outcome.get("journal_entry"),
				"custom_consumption_log": "\n".join(outcome["log"]),
				"custom_consumption_processed_on": now_datetime(),
			},
			update_modified=False,
		)
		frappe.db.commit()
	except Exception as e:
		# nothing from this attempt survives: no half-made Stock Entry / Journal Entry
		try:
			if savepoint_set:
				frappe.db.rollback(save_point=savepoint)
			else:
				frappe.db.rollback()
		except Exception:
			frappe.db.rollback()
		text = _error_text(e)
		frappe.log_error(title=f"Recipe consumption failed: {closing_shift}", message=frappe.get_traceback())
		frappe.db.set_value(
			"POS Closing Shift",
			closing_shift,
			{"custom_consumption_status": "Failed", "custom_consumption_log": text},
			update_modified=False,
		)
		frappe.db.commit()
		if shift:
			_notify_failure(shift, text)
		if raise_errors:
			raise


def run_now(closing_shift):
	"""bench console helper: reset to Pending, run inline, show errors, return the final state."""
	frappe.db.set_value(
		"POS Closing Shift", closing_shift, "custom_consumption_status", "Pending", update_modified=False
	)
	frappe.db.commit()
	process_closing_shift(closing_shift, raise_errors=True)
	return frappe.db.get_value(
		"POS Closing Shift",
		closing_shift,
		[
			"custom_consumption_status",
			"custom_consumption_stock_entry",
			"custom_consumption_journal_entry",
			"custom_consumption_log",
		],
		as_dict=True,
	)


def _consume(shift):
	profile = frappe.db.get_value(
		"POS Profile",
		shift.pos_profile,
		[
			"custom_item_group_drinks",
			"custom_consumables_warehouse",
			"custom_consumption_expense_account",
			"custom_nonstock_cost_debit_account",
			"custom_nonstock_cost_credit_account",
			"cost_center",
		],
		as_dict=True,
	)
	wastage_rows = [
		{"item_code": w.item_code, "qty": flt(w.qty), "reason": (w.reason or "").strip()}
		for w in (shift.get("custom_wastage") or [])
	]
	if not profile or not (profile.custom_item_group_drinks or wastage_rows):
		return {"status": "Not Applicable", "log": []}
	if not profile.custom_consumables_warehouse:
		frappe.throw(_("Set 'Consumable Warehouse' on POS Profile {0}.").format(shift.pos_profile))

	# duplicate guard, per document: never create a second Stock Entry / Journal Entry for a shift,
	# but DO create the missing one if only one of the two exists (resume after an interrupted run)
	existing = {}
	for doctype in ("Stock Entry", "Journal Entry"):
		for row in frappe.get_all(
			doctype,
			filters={"custom_pos_closing_shift": shift.name, "docstatus": ["<", 2]},
			fields=["name", "docstatus"],
		):
			if row.docstatus == 0:
				frappe.throw(
					_("Draft {0} {1} already exists for this closing shift. Delete it and retry.").format(
						doctype, row.name
					)
				)
			existing[doctype] = row.name

	if profile.custom_item_group_drinks:
		groups = _item_group_tree(profile.custom_item_group_drinks)
		sold_lines, skipped_legacy = _collect_sold_lines(_invoice_refs(shift), groups)
	else:
		sold_lines, skipped_legacy = [], []
	boms, items = _load_boms_and_items(sold_lines, {w["item_code"] for w in wastage_rows})
	plan = aggregate_consumption(sold_lines, boms, items)
	add_wastage(plan, wastage_rows, items)

	log = []
	if plan["no_bom"]:
		log.append("No active Default BOM, nothing consumed for: " + ", ".join(sorted(plan["no_bom"])))
	if skipped_legacy:
		log.append(
			"Skipped invoices already consumed by the old per-invoice hook: " + ", ".join(skipped_legacy)
		)
	if wastage_rows:
		log.append(f"{len(wastage_rows)} wastage line(s) included.")

	if not plan["stock"] and not plan["non_stock"]:
		return {"status": "Nothing to Consume", "log": log}

	_check_batch_settings(plan["stock"], items)
	posting_date, posting_time = _posting_datetime(shift)

	stock_entry_name = existing.get("Stock Entry")
	if plan["stock"] and not stock_entry_name:
		stock_entry_name = _make_stock_entry(shift, profile, plan["stock"], posting_date, posting_time).name
	elif stock_entry_name:
		log.append(f"Stock Entry {stock_entry_name} already existed; not created again.")

	journal_entry_name = existing.get("Journal Entry")
	if plan["non_stock"] and not journal_entry_name:
		journal_entry, note = _make_journal_entry(shift, profile, plan["non_stock"], posting_date)
		journal_entry_name = journal_entry.name if journal_entry else None
		if note:
			log.append(note)

	return {
		"status": "Processed",
		"stock_entry": stock_entry_name,
		"journal_entry": journal_entry_name,
		"log": log,
	}


# --------------------------------------------------------------------------------------
# Pure logic (no database access: unit-tested without a site)
# --------------------------------------------------------------------------------------


def aggregate_consumption(sold_lines, boms, items):
	"""
	sold_lines: [{doctype, invoice, item_code, item_name, stock_qty}]
	boms:       {drink_item_code: {"name", "quantity", "rows": [{item_code, stock_qty, rate, conversion_factor}]}}
	items:      {ingredient_item_code: {item_name, stock_uom, is_stock_item, valuation_rate}}
	Returns {"stock": {...}, "non_stock": {...}, "no_bom": set()} where each ingredient entry is
	{item_code, item_name, stock_uom, qty, amount, lines: {(doctype, invoice, drink): {...}}}.
	Quantities are in the ingredient's STOCK UOM.
	"""
	stock, non_stock, no_bom = OrderedDict(), OrderedDict(), set()

	for line in sold_lines:
		bom = boms.get(line["item_code"])
		if not bom or not bom["rows"]:
			no_bom.add(line["item_code"])
			continue

		factor = flt(line["stock_qty"]) / (flt(bom["quantity"]) or 1)
		for row in bom["rows"]:
			used = flt(row["stock_qty"]) * factor
			if not used:
				continue

			info = items[row["item_code"]]
			is_stock = cint(info["is_stock_item"])
			target = stock if is_stock else non_stock
			entry = target.setdefault(
				row["item_code"],
				{
					"item_code": row["item_code"],
					"item_name": info.get("item_name") or row["item_code"],
					"stock_uom": info["stock_uom"],
					"qty": 0.0,
					"amount": 0.0,
					"lines": OrderedDict(),
				},
			)
			entry["qty"] += used

			if not is_stock:
				# configured fixed rate: Item valuation rate (per stock UOM), else the BOM line rate
				unit_cost = flt(info.get("valuation_rate")) or (
					flt(row.get("rate")) / (flt(row.get("conversion_factor")) or 1)
				)
				entry["amount"] += used * unit_cost

			key = (line["doctype"], line["invoice"], line["item_code"])
			detail = entry["lines"].setdefault(
				key,
				{
					"kind": "sale",
					"doctype": line["doctype"],
					"invoice": line["invoice"],
					"drink": line.get("item_name") or line["item_code"],
					"sold": 0.0,
					"used": 0.0,
				},
			)
			detail["sold"] += flt(line["stock_qty"])
			detail["used"] += used

	return {"stock": stock, "non_stock": non_stock, "no_bom": no_bom}


def add_wastage(plan, wastage_rows, items):
	"""
	Add the closing popup's wastage rows ({item_code, qty (stock UOM), reason}) to plan["stock"].
	If the item is also a recipe ingredient its row simply gains wastage lines; otherwise a new row.
	"""
	for idx, w in enumerate(wastage_rows):
		qty = flt(w["qty"])
		info = items.get(w["item_code"])
		if qty <= 0 or not info or not cint(info["is_stock_item"]):
			continue
		entry = plan["stock"].setdefault(
			w["item_code"],
			{
				"item_code": w["item_code"],
				"item_name": info.get("item_name") or w["item_code"],
				"stock_uom": info["stock_uom"],
				"qty": 0.0,
				"amount": 0.0,
				"lines": OrderedDict(),
			},
		)
		entry["qty"] += qty
		entry["lines"][("Wastage", idx, w["item_code"])] = {
			"kind": "wastage",
			"reason": w.get("reason") or "",
			"sold": 0.0,
			"used": qty,
		}


def render_breakdown(entry, qty_precision=3):
	"""HTML table stored on each Stock Entry row: which invoice, which drink, how much was used."""
	uom = escape_html(entry["stock_uom"])
	body = []
	for d in entry["lines"].values():
		used = f"{flt(d['used'], 6):.{qty_precision + 1}f}"
		if d.get("kind") == "wastage":
			body.append(
				"<tr><td><b>{src}</b></td><td>{reason}</td><td></td>"
				"<td style='text-align:right'>{used}</td></tr>".format(
					src=escape_html(_("Wastage")), reason=escape_html(d.get("reason") or ""), used=used
				)
			)
			continue
		route = "/app/" + frappe.scrub(d["doctype"]).replace("_", "-") + "/" + d["invoice"]
		body.append(
			"<tr><td><a href='{route}'>{inv}</a></td><td>{drink}</td>"
			"<td style='text-align:right'>{sold:g}</td><td style='text-align:right'>{used}</td></tr>".format(
				route=escape_html(route),
				inv=escape_html(d["invoice"]),
				drink=escape_html(d["drink"]),
				sold=flt(d["sold"], 3),
				used=used,
			)
		)
	total = f"{flt(entry['qty'], qty_precision + 1):.{qty_precision + 1}f}"
	return (
		"<table class='table table-bordered table-sm'>"
		f"<thead><tr><th>Invoice / Wastage</th><th>Drink sold / Reason</th><th>Qty sold</th><th>Qty used ({uom})</th></tr></thead>"
		f"<tbody>{''.join(body)}</tbody>"
		f"<tfoot><tr><td colspan='3'><b>Total issued ({len(body)} lines)</b></td>"
		f"<td style='text-align:right'><b>{total}</b></td></tr></tfoot></table>"
	)


# --------------------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------------------


def _item_group_tree(group):
	lft, rgt = frappe.db.get_value("Item Group", group, ["lft", "rgt"])
	return set(frappe.get_all("Item Group", filters={"lft": [">=", lft], "rgt": ["<=", rgt]}, pluck="name"))


def _invoice_refs(shift):
	refs = []
	for row in shift.get("pos_transactions") or []:
		if row.get("sales_invoice"):
			refs.append(("Sales Invoice", row.sales_invoice))
		elif row.get("pos_invoice"):
			refs.append(("POS Invoice", row.pos_invoice))

	if not refs and shift.pos_opening_shift and frappe.db.has_column("Sales Invoice", "posa_pos_opening_shift"):
		for name in frappe.get_all(
			"Sales Invoice",
			filters={"docstatus": 1, "posa_pos_opening_shift": shift.pos_opening_shift},
			pluck="name",
			order_by="creation",
		):
			refs.append(("Sales Invoice", name))

	return list(OrderedDict.fromkeys(refs))


def _collect_sold_lines(refs, groups):
	"""Submitted, non-return invoice lines whose item is in the consumable item group."""
	legacy = frappe.db.has_column("Stock Entry", "custom_sales_invoice")
	lines, skipped_legacy = [], []

	for doctype in ("Sales Invoice", "POS Invoice"):
		names = [n for d, n in refs if d == doctype]
		if not names:
			continue

		headers = {
			r.name: r
			for r in frappe.get_all(
				doctype, filters={"name": ["in", names]}, fields=["name", "docstatus", "is_return"]
			)
		}
		eligible = []
		for n in names:
			h = headers.get(n)
			# returns never put ingredients back: the drink was already made
			if not h or h.docstatus != 1 or cint(h.is_return):
				continue
			if (
				legacy
				and doctype == "Sales Invoice"
				and frappe.db.exists("Stock Entry", {"custom_sales_invoice": n, "docstatus": 1})
			):
				skipped_legacy.append(n)
				continue
			eligible.append(n)
		if not eligible:
			continue

		rows = frappe.get_all(
			f"{doctype} Item",
			parent_doctype=doctype,
			filters={"parent": ["in", eligible]},
			fields=["parent", "item_code", "item_name", "qty", "stock_qty", "conversion_factor"],
			order_by="parent, idx",
		)
		item_group = {
			r.name: r.item_group
			for r in frappe.get_all(
				"Item",
				filters={"name": ["in", list({r.item_code for r in rows})]},
				fields=["name", "item_group"],
			)
		}
		for r in rows:
			if item_group.get(r.item_code) not in groups:
				continue
			lines.append(
				{
					"doctype": doctype,
					"invoice": r.parent,
					"item_code": r.item_code,
					"item_name": r.item_name,
					"stock_qty": flt(r.stock_qty) or flt(r.qty) * (flt(r.conversion_factor) or 1),
				}
			)

	return lines, skipped_legacy


def _load_boms_and_items(sold_lines, extra_item_codes=()):
	drink_codes = list({line["item_code"] for line in sold_lines})
	if not drink_codes and not extra_item_codes:
		return {}, {}

	default_bom = {
		r.name: r.default_bom
		for r in frappe.get_all(
			"Item", filters={"name": ["in", drink_codes or [""]]}, fields=["name", "default_bom"]
		)
	}
	bom_names = [b for b in default_bom.values() if b]
	headers = {
		b.name: b
		for b in frappe.get_all(
			"BOM",
			filters={"name": ["in", bom_names or [""]], "docstatus": 1, "is_active": 1},
			fields=["name", "quantity"],
		)
	}
	rows_by_bom = {}
	for r in frappe.get_all(
		"BOM Item",
		parent_doctype="BOM",
		filters={"parent": ["in", list(headers) or [""]]},
		fields=["parent", "item_code", "qty", "stock_qty", "conversion_factor", "rate"],
		order_by="parent, idx",
	):
		rows_by_bom.setdefault(r.parent, []).append(
			{
				"item_code": r.item_code,
				"stock_qty": flt(r.stock_qty) or flt(r.qty) * (flt(r.conversion_factor) or 1),
				"rate": flt(r.rate),
				"conversion_factor": flt(r.conversion_factor) or 1,
			}
		)

	boms = {}
	for code in drink_codes:
		name = default_bom.get(code)
		if name in headers:
			boms[code] = {
				"name": name,
				"quantity": flt(headers[name].quantity) or 1,
				"rows": rows_by_bom.get(name, []),
			}

	ingredient_codes = {r["item_code"] for rows in rows_by_bom.values() for r in rows} | set(extra_item_codes)
	items = {
		i.name: i
		for i in frappe.get_all(
			"Item",
			filters={"name": ["in", list(ingredient_codes) or [""]]},
			fields=[
				"name",
				"item_name",
				"stock_uom",
				"is_stock_item",
				"valuation_rate",
				"has_batch_no",
				"has_serial_no",
			],
		)
	}
	return boms, items


def _check_batch_settings(stock_plan, items):
	"""Batch/serial rows are auto-picked by ERPNext at submit; make sure it is allowed."""
	needs_pick = [c for c in stock_plan if items[c].has_batch_no or items[c].has_serial_no]
	if needs_pick and not frappe.db.get_single_value(
		"Stock Settings", "auto_create_serial_and_batch_bundle_for_outward"
	):
		frappe.throw(
			_(
				"Batch/serial tracked ingredients ({0}) need 'Auto Create Serial and Batch Bundle For Outward' "
				"enabled in Stock Settings, so ERPNext can pick batches for the consolidated issue."
			).format(", ".join(needs_pick))
		)


def _posting_datetime(shift):
	today = getdate(nowdate())
	posting = getdate(shift.posting_date) if shift.get("posting_date") else today
	if posting >= today:
		return today, nowtime()
	return posting, "23:59:59"  # a late-processed older shift is dated to its own day


# --------------------------------------------------------------------------------------
# Document creation
# --------------------------------------------------------------------------------------


def _make_stock_entry(shift, profile, stock_plan, posting_date, posting_time):
	ensure_stock_entry_type()
	qty_precision = frappe.get_precision("Stock Entry Detail", "qty") or 3

	se = frappe.new_doc("Stock Entry")
	se.stock_entry_type = STOCK_ENTRY_TYPE
	se.purpose = "Material Issue"
	se.company = shift.company
	se.set_posting_time = 1
	se.posting_date = posting_date
	se.posting_time = posting_time
	se.custom_pos_closing_shift = shift.name
	se.remarks = _(
		"Recipe consumption for POS Closing Shift {0} (POS Profile {1}). One entry for the whole shift."
	).format(shift.name, shift.pos_profile)

	for entry in stock_plan.values():
		qty = flt(entry["qty"], qty_precision)
		if not qty:
			continue
		row = {
			"item_code": entry["item_code"],
			"qty": qty,
			"uom": entry["stock_uom"],
			"stock_uom": entry["stock_uom"],
			"conversion_factor": 1,
			"s_warehouse": profile.custom_consumables_warehouse,
			"cost_center": profile.cost_center,
			"custom_consumption_breakdown": render_breakdown(entry, qty_precision),
		}
		if profile.custom_consumption_expense_account:
			row["expense_account"] = profile.custom_consumption_expense_account
		se.append("items", row)

	if not se.items:
		frappe.throw(_("Consumed quantities round to zero; nothing to issue."))

	se.flags.ignore_permissions = True
	se.insert()
	se.submit()
	return se


def _make_journal_entry(shift, profile, non_stock_plan, posting_date):
	"""Cost of non-stock ingredients (water ...) at fixed rates. Posted only if accounts are set."""
	amount_precision = frappe.get_precision("Journal Entry Account", "debit_in_account_currency") or 2
	total = flt(sum(e["amount"] for e in non_stock_plan.values()), amount_precision)
	summary = "; ".join(
		f"{e['item_code']}: {flt(e['qty'], 3)} {e['stock_uom']} = {flt(e['amount'], amount_precision)}"
		for e in non_stock_plan.values()
	)
	if not total:
		return None, "Non-stock ingredients have no rate (set Valuation Rate on the item): " + summary

	debit = profile.custom_nonstock_cost_debit_account
	credit = profile.custom_nonstock_cost_credit_account
	if not (debit and credit):
		return None, (
			f"Non-stock cost {total} NOT posted (set the two non-stock cost accounts on POS Profile "
			f"{shift.pos_profile}): {summary}"
		)

	je = frappe.new_doc("Journal Entry")
	je.voucher_type = "Journal Entry"
	je.company = shift.company
	je.posting_date = posting_date
	je.custom_pos_closing_shift = shift.name
	je.user_remark = f"Non-stock recipe cost for POS Closing Shift {shift.name}. {summary}"
	je.append(
		"accounts",
		{"account": debit, "debit_in_account_currency": total, "cost_center": profile.cost_center},
	)
	je.append(
		"accounts",
		{"account": credit, "credit_in_account_currency": total, "cost_center": profile.cost_center},
	)
	je.flags.ignore_permissions = True
	je.insert()
	je.submit()
	return je, None


def ensure_stock_entry_type():
	"""Self-healing: (re)create the Stock Entry Type if it was ever deleted. Autoname is 'Prompt'."""
	if frappe.db.exists("Stock Entry Type", STOCK_ENTRY_TYPE):
		return
	doc = frappe.new_doc("Stock Entry Type")
	doc.name = STOCK_ENTRY_TYPE
	doc.purpose = "Material Issue"
	doc.insert(ignore_permissions=True)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _error_text(e):
	text = strip_html_tags(str(e) or "").strip()
	if not text and getattr(frappe.local, "message_log", None):
		text = strip_html_tags(str(frappe.local.message_log[-1])).strip()
	frappe.clear_messages()
	return (text or e.__class__.__name__)[:1000]


def _notify_failure(shift, text):
	try:
		users = frappe.get_all("Has Role", filters={"role": "Stock Manager", "parenttype": "User"}, pluck="parent")
		recipients = [
			u for u in set(users) if u not in ("Administrator", "Guest") and frappe.db.get_value("User", u, "enabled")
		]
		if recipients:
			frappe.sendmail(
				recipients=recipients,
				subject=_("Recipe consumption failed: {0}").format(shift.name),
				message=_("POS Closing Shift {0} was closed but its ingredient consumption failed:<br><br>{1}<br><br>"
				"Fix the cause, then press 'Process Recipe Consumption' on the closing shift.").format(
					get_link_to_form("POS Closing Shift", shift.name), escape_html(text)
				),
			)
	except Exception:
		frappe.log_error(title="Recipe consumption: could not send failure email", message=frappe.get_traceback())