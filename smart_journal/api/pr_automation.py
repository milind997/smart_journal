# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt
"""
Purchase Request → AI Accounting Review automation.

Flow:
  1. Accountant clicks "Create AI Review" on a Purchase Request.
  2. create_ai_review()  →  creates the AI Accounting Review doc (status=Draft)
                        →  queues process_pr_review() as a background job.
  3. process_pr_review() (background):
       - Reads every image/PDF attachment on the PR via AI vision.
       - Extracts: expense type, amount, VAT, vendor per receipt.
       - Falls back to PR description text when no attachments exist.
       - Groups receipts by GL account (using routing rules from AI Settings).
       - Populates extracted_documents + accounting_rows child tables.
       - Sets status → "AI Suggested".
  4. Accountant opens the review, checks suggested accounts, adjusts if needed.
  5. Accountant clicks "Create Journal Entry"  →  draft JE created and linked.
"""

import base64
import json
import re
from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, today

from smart_journal.api.extraction import (
    _digits,
    _file_to_images,
    _img_media_type,
    _parse_json,
    _to_date,
)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_RECEIPT_PROMPT = """\
You are reading an expense receipt, invoice, or bank bill-payment confirmation
for a Saudi company (amounts in SAR). The document may be in Arabic, English, or
both, and it MAY contain MULTIPLE bills/expenses — e.g. a SADAD batch payment
that pays several bills at once, or a salary transfer that covers basic salary
plus overtime. Split every distinct expense into its own line.

Return ONLY a JSON object — no markdown, no commentary:
{
  "document_type": "Invoice" | "Receipt" | "Bank Slip" | "Other",
  "legible": <true if the document is clearly readable; false if it is blurry, dark, skewed, cut off, or you had to GUESS the amounts>,
  "vendor": "<overall payer/seller name or empty string>",
  "vendor_vat": "<seller VAT registration number, digits only, or empty>",
  "invoice_number": "<string or empty>",
  "invoice_date": "<YYYY-MM-DD or empty>",
  "reference_number": "<bank / SADAD reference number if shown, else empty>",
  "bank_charge": <number: bank commission / transfer fee, else 0>,
  "bank_charge_vat": <number: VAT charged on the bank fee, else 0>,
  "line_items": [
    {
      "expense_type": "<one of the categories below>",
      "description": "<short English description of THIS bill/line>",
      "vendor": "<biller/seller for this line, or empty>",
      "amount_before_vat": <number>,
      "vat_amount": <number>,
      "total_amount": <number>
    }
  ]
}

expense_type must be exactly one of:
  __EXPENSE_TYPES__

Rules:
- Create ONE line_items entry per distinct bill / expense / row in the document.
  A SADAD batch with 4 bills must return 4 line_items. A salary + overtime
  transfer should return separate "Salary" and "Overtime" lines if both appear.
- "Social Insurance" = GOSI / التأمينات الاجتماعية (not a generic Government Fee).
- "Zakat" = ZATCA zakat / الزكاة / هيئة الزكاة والضريبة والجمارك (company zakat or a
  reassessment / إعادة ربط), NOT a generic Government Fee.
- The bank's own commission/fee and its VAT go in "bank_charge" / "bank_charge_vat",
  NEVER inside line_items.
- Numbers are plain numbers (no currency symbol, no commas).
- If a value is missing use "" for strings and 0 for numbers.
- Per line: amount_before_vat + vat_amount should equal total_amount (within rounding).
- Set "legible" to false whenever the scan quality forced you to guess any amount.
"""

_TEXT_PROMPT = """\
You are an accounting assistant. Extract every expense line item from the
Purchase Request description below. The text may be in Arabic, English, or both.

Title: {title}
Description:
{text}

Return ONLY a JSON object — no markdown:
{{
  "line_items": [
    {{"expense_type": "<category>", "description": "<English description>",
      "amount": <number>, "vat_amount": <number>}}
  ]
}}

expense_type must be exactly one of:
  __EXPENSE_TYPES__

If no amounts are found return {{"line_items": []}}.
"""

_PROCESSABLE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "pdf"}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# Everything the PR automation does is written to a dedicated rotating log:
#     frappe-bench/sites/<site>/logs/smart_journal.log
# Tail it live with:
#     tail -f sites/<site>/logs/smart_journal.log
# Every line is prefixed with [<review_name>] so a single run can be grepped:
#     grep "AI-REVIEW-0001" sites/<site>/logs/smart_journal.log
#
# Failures are ALSO recorded in the Error Log doctype (Desk → Error Log, or
# /app/error-log) with the full traceback, so non-technical users can see them.

def _logger():
	"""Dedicated rotating logger → sites/<site>/logs/smart_journal.log.

	Frappe's logger defaults to WARNING, which would hide our INFO progress
	lines, so we force INFO — we want the 'working' lines, not just failures.
	"""
	import logging

	logger = frappe.logger("smart_journal", allow_site=True, file_count=10)
	logger.setLevel(logging.INFO)
	return logger


def _log(msg, level="info"):
	"""Write a progress / diagnostic line to the smart_journal log file."""
	try:
		getattr(_logger(), level)(msg)
	except Exception:
		# Logging must never break the actual job.
		pass


def _error_log(title, message=None, reference_doctype=None, reference_name=None):
	"""Record a failure in the core **Error Log** doctype (Desk → Error Log).

	Uses keyword args so Frappe's title/message swap-heuristic can't mis-file the
	entry, and links the row to its source document via reference_doctype /
	reference_name so it is clickable and filterable in the Desk UI:
	    /app/error-log?reference_doctype=AI Accounting Review

	Returns the created Error Log name (or None on failure).
	"""
	try:
		el = frappe.log_error(
			title=(title or "Smart Journal")[:140],
			message=message or frappe.get_traceback(with_context=True),
			reference_doctype=reference_doctype,
			reference_name=reference_name,
		)
		return getattr(el, "name", None)
	except Exception:
		# Never let error-logging raise from inside an except block.
		_log(f"could not write to Error Log: {title}", "error")
		return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@frappe.whitelist()
def create_ai_review(pr_name):
	"""Create an AI Accounting Review doc for *pr_name* and queue AI processing.

	Returns:
	  {"review_name": str, "already_exists": bool}
	"""
	pr = frappe.get_doc("Purchase Request", pr_name)

	# Block duplicate active reviews for the same PR.
	existing = frappe.db.get_value(
		"AI Accounting Review",
		{"purchase_request": pr_name, "status": ["not in", ["Cancelled"]]},
		"name",
	)
	if existing:
		return {"review_name": existing, "already_exists": True}

	# Create the stub review document immediately so the user has a link.
	review = frappe.new_doc("AI Accounting Review")
	review.purchase_request = pr_name
	review.company = pr.company
	review.posting_date = today()
	review.currency = getattr(pr, "currency", None) or "SAR"
	review.requested_amount = flt(pr.requested_amount)
	review.recommended_purchase_amount = flt(getattr(pr, "recommended_purchase_amount", 0))
	review.status = "Draft"
	review.validation_status = "Not Validated"
	review.ai_remarks = "⏳ AI is reading your attachments in the background. Refresh this page in a moment."
	review.insert(ignore_permissions=True)
	frappe.db.commit()

	# Queue the heavy lifting in the background.
	frappe.enqueue(
		"smart_journal.api.pr_automation.process_pr_review",
		queue="long",
		timeout=600,
		review_name=review.name,
		pr_name=pr_name,
	)
	_log(f"[{review.name}] QUEUED background processing for PR {pr_name} (company={pr.company})")

	return {"review_name": review.name, "already_exists": False}


@frappe.whitelist()
def extract_to_pr(pr_name):
	"""Read the PR's attached invoices and fill its 'Expense Breakdown' table.

	This runs synchronously (the user clicked the button and is waiting). It
	splits every attachment into per-expense-type lines so the user can correct
	the amounts on the Purchase Request itself before sending to accounting.

	Returns {"line_count", "breakdown_total", "requested_amount", "mismatch"}.
	"""
	settings = frappe.get_single("AI Settings")
	if not settings.enabled:
		frappe.throw(_("AI Settings is disabled. Enable it and set an API key first."))

	pr = frappe.get_doc("Purchase Request", pr_name)
	rules = _load_routing_rules_v2()
	collected = _collect_attachment_lines(pr, settings, rules)

	# Merge invoice + bank-slip lines: prefer invoice/receipt lines; only use
	# bank-slip lines when no invoice line exists (avoids double counting).
	lines = collected["expense_line_items"] or collected["bank_slip_line_items"]

	pr.set("expense_breakdown", [])
	for item in lines:
		total = flt(item["amount"]) + flt(item["vat"])
		pr.append("expense_breakdown", {
			"expense_type": item["expense_type"],
			"description": item["description"],
			"amount": flt(item["amount"]),
			"vat_amount": flt(item["vat"]),
			"total_amount": total,
			"attachment": item.get("file_url"),
			"source": item.get("source") or "AI",
			"confidence": 80,
		})

	breakdown_total = sum(flt(r.total_amount) for r in pr.expense_breakdown)
	requested = flt(pr.requested_amount)
	mismatch = flt(requested - breakdown_total)

	_set_if_exists(pr, "breakdown_total", breakdown_total)
	_set_if_exists(pr, "breakdown_status", _breakdown_status_text(requested, breakdown_total))

	pr.save(ignore_permissions=True)
	frappe.db.commit()

	return {
		"line_count": len(lines),
		"breakdown_total": breakdown_total,
		"requested_amount": requested,
		"mismatch": mismatch,
	}


@frappe.whitelist()
def get_review_status(review_name):
	"""Return lightweight status info for polling from the browser."""
	doc = frappe.get_doc("AI Accounting Review", review_name)
	return {
		"status": doc.status,
		"validation_status": doc.validation_status,
		"ai_confidence": doc.ai_confidence,
		"row_count": len(doc.accounting_rows or []),
	}


# ---------------------------------------------------------------------------
# Background job
# ---------------------------------------------------------------------------

def process_pr_review(review_name, pr_name):
	"""Background: extract data from PR attachments and populate the review doc."""
	_log(f"[{review_name}] START — processing PR {pr_name}")
	try:
		settings = frappe.get_single("AI Settings")
		if not settings.enabled:
			_log(f"[{review_name}] ABORT — AI Settings is disabled", "error")
			_fail(review_name, "AI Settings is disabled. Please enable it and add an API key.")
			return

		# Fail fast with a clear message if the API key is missing — otherwise the
		# job crashes with a raw traceback deep inside extraction (get_api_key()
		# raises). Read the raw field so this check never throws.
		if not settings.get_password("api_key", raise_exception=False):
			_log(f"[{review_name}] ABORT — AI Settings has no API Key", "error")
			_fail(
				review_name,
				"AI Settings has no API Key.\n\n"
				"Open AI Settings, paste your Anthropic API key into the 'API Key' "
				"field, save, then create the review again.",
			)
			return

		pr = frappe.get_doc("Purchase Request", pr_name)
		rules = _load_routing_rules_v2()
		company_defaults = _load_company_defaults()
		_log(
			f"[{review_name}] config: provider={settings.provider} model={settings.get_default_model()} "
			f"routing_rules={len(rules)} company_defaults={len(company_defaults)}"
		)

		# ------------------------------------------------------------------ #
		# Step 1 — collect expense lines.
		#
		# Preferred source: the human-corrected "Expense Breakdown" table on the
		# Purchase Request (filled via 'Extract & Divide' and edited by the user).
		# If that table is empty we fall back to extracting the attachments here.
		# ------------------------------------------------------------------ #
		pr_lines = _lines_from_pr_breakdown(pr, rules)
		if pr_lines["expense_line_items"]:
			extracted_docs = pr_lines["extracted_docs"]
			expense_line_items = pr_lines["expense_line_items"]
			bank_slip_line_items = []
			bank_charge_total = pr_lines["bank_charge_total"]
			bank_charge_vat_total = pr_lines["bank_charge_vat_total"]
			processed_count = pr_lines["processed_count"]
			vendor_name = pr_lines.get("vendor_name") or ""
			vendor_vat = pr_lines.get("vendor_vat") or ""
			unclear_files = pr_lines.get("unclear_files") or []
		else:
			collected = _collect_attachment_lines(pr, settings, rules)
			extracted_docs = collected["extracted_docs"]
			expense_line_items = collected["expense_line_items"]
			bank_slip_line_items = collected["bank_slip_line_items"]
			bank_charge_total = collected["bank_charge_total"]
			bank_charge_vat_total = collected["bank_charge_vat_total"]
			processed_count = collected["processed_count"]
			vendor_name = collected.get("vendor_name") or ""
			vendor_vat = collected.get("vendor_vat") or ""
			unclear_files = collected.get("unclear_files") or []

		# Resolve (or auto-create) the Supplier for the extracted vendor so it can
		# be set as the Party on the bank-credit line of the Journal Entry.
		supplier = _resolve_supplier(vendor_name, vendor_vat)
		_log(
			f"[{review_name}] vendor='{vendor_name or '-'}' vat='{vendor_vat or '-'}' "
			f"→ supplier={supplier or '(none)'}"
		)

		_src = "PR breakdown table" if pr_lines["expense_line_items"] else "attachment extraction"
		_log(
			f"[{review_name}] extracted via {_src}: files={processed_count} "
			f"invoice_lines={len(expense_line_items)} bank_slip_lines={len(bank_slip_line_items)} "
			f"bank_charge={bank_charge_total} bank_charge_vat={bank_charge_vat_total}"
		)

		# Determine final line_items for GL posting:
		#
		# Case A — invoice/receipt only (no bank slip):
		#   Use invoice amounts as-is.
		#
		# Case B — bank slip only (no invoice/receipt):
		#   Use bank slip amounts (common when only payment proof is attached).
		#
		# Case C — both bank slip AND invoice/receipt present:
		#   Use invoice for account routing (expense_type/description),
		#   but override amounts with the bank slip total and VAT.
		#   This handles handwritten invoices where OCR misreads the amount —
		#   the bank slip tells us exactly what was paid.
		if expense_line_items and bank_slip_line_items:
			# The bank slip is the ground truth for the TOTAL CASH PAID, and that
			# total is VAT-INCLUSIVE (gross). The invoice gives us the net/VAT
			# structure. Reconcile GROSS-to-GROSS so the expense + VAT debits sum to
			# what was actually paid, while PRESERVING the invoice's net:VAT split.
			#
			# (The previous version scaled the net up to the gross and then zeroed
			# the VAT — which overstated the expense and discarded recoverable input
			# VAT. e.g. a 170 SAR hospitality invoice was booked as 170 expense / 0
			# VAT instead of 147.82 expense / 22.18 VAT.)
			bank_gross = sum(flt(i["amount"]) + flt(i["vat"]) for i in bank_slip_line_items)
			bank_vat = sum(flt(i["vat"]) for i in bank_slip_line_items)
			invoice_gross = sum(flt(i["amount"]) + flt(i["vat"]) for i in expense_line_items)

			if invoice_gross > 0 and bank_gross > 0:
				# Scale net AND VAT by the same factor → ratio preserved, gross matches.
				scale = bank_gross / invoice_gross
				for item in expense_line_items:
					item["amount"] = flt(item["amount"] * scale)
					item["vat"] = flt(item["vat"] * scale)
			elif bank_gross > 0:
				# Invoice amounts were unreadable (e.g. handwritten). Fall back to the
				# bank slip: split out VAT only if the slip itself carried a VAT figure.
				n = len(expense_line_items)
				net_each = flt((bank_gross - bank_vat) / n)
				for idx, item in enumerate(expense_line_items):
					item["amount"] = net_each
					item["vat"] = flt(bank_vat) if idx == 0 else 0.0

			line_items = expense_line_items
		else:
			# Case A or B
			line_items = expense_line_items if expense_line_items else bank_slip_line_items

		# ------------------------------------------------------------------ #
		# Step 2 — fall back to PR description text if no attachments worked
		# ------------------------------------------------------------------ #
		if not line_items:
			text_items = _extract_from_text(pr, settings)
			for item in text_items:
				etype = item.get("expense_type", "Other")
				desc = item.get("description", "")
				account = _route_by_type(etype, pr.company, rules, desc)
				line_items.append({
					"expense_type": etype,
					"description": desc,
					"amount": flt(item.get("amount", 0)),
					"vat": flt(item.get("vat_amount", 0)),
					"source": "PR Description",
					"account": account,
				})

		# ------------------------------------------------------------------ #
		# Step 3 — group by GL account → accounting rows
		# ------------------------------------------------------------------ #
		accounting_rows = _build_accounting_rows(line_items, pr.company, company_defaults)

		# Append bank charge + VAT-on-charge rows (kept out of the PR amount).
		accounting_rows += _make_bank_charge_rows(
			bank_charge_total, bank_charge_vat_total, pr.company, company_defaults
		)

		# Guard against double-counting when multiple attachments represent the
		# same payment (e.g. a bank slip + an approval sheet for the same amount).
		# If the extracted total exceeds the PR requested_amount by more than 50%,
		# scale only the PR-expense debit rows (not VAT / bank charges) down so
		# they sum to requested_amount.
		pr_requested = flt(pr.requested_amount)
		total_debit = sum(r["debit"] for r in accounting_rows if r.get("include_in_pr_amount"))
		if pr_requested > 0 and total_debit > pr_requested * 1.5:
			scale = pr_requested / total_debit
			for r in accounting_rows:
				if r.get("include_in_pr_amount"):
					r["debit"] = flt(r["debit"] * scale)

		# Unmatched check is on the expense (debit) rows only — before we add credits.
		unmatched = [r for r in accounting_rows if not r.get("suggested_account") and r.get("include_in_pr_amount")]
		confidence = 90 if not unmatched else max(40, 90 - len(unmatched) * 15)

		# ------------------------------------------------------------------ #
		# Step 3b — add ONE consolidated bank-credit line that pays for all the
		# debits, mirroring how the accountant books these (DR expense, DR VAT,
		# … , CR bank total) — e.g. JVS-2210-018 / JVR-2210-085 each have a
		# single Al Enmaa credit line for the full amount paid.
		# ------------------------------------------------------------------ #
		co_def = company_defaults.get(pr.company, {})
		bank_account = co_def.get("credit_account") or _get_default_bank_account(pr.company)
		payable_account = _get_payable_account(pr.company, company_defaults) if supplier else None

		# Real figures, captured BEFORE the settlement/payable round-trip is added
		# (which would otherwise double the debit total). invoice_amount excludes
		# bank charges; cash_paid is everything that leaves the bank.
		invoice_amount = sum(flt(r["debit"]) for r in accounting_rows if not r.get("is_bank_charge"))
		cash_paid = sum(flt(r["debit"]) for r in accounting_rows)

		accounting_rows = _append_settlement_lines(
			accounting_rows, bank_account, pr.company,
			supplier=supplier, payable_account=payable_account,
		)

		# Full balanced totals (include the payable round-trip) — these match the
		# Journal Entry's total_debit / total_credit, e.g. 6056.98 in the reference.
		total_debit = sum(r["debit"] for r in accounting_rows)
		total_credit = sum(r["credit"] for r in accounting_rows)

		_log(
			f"[{review_name}] reconciled: rows={len(accounting_rows)} "
			f"debit={flt(total_debit):.2f} credit={flt(total_credit):.2f} "
			f"diff={flt(total_debit - total_credit):.2f} confidence={confidence} "
			f"unmatched_accounts={len(unmatched)}"
		)

		remarks = _build_remarks(line_items, accounting_rows, processed_count)

		# Prepend a prominent warning when any attachment was unclear, so the
		# accountant knows to upload a clearer copy before trusting the amounts.
		unclear_note = _unclear_warning(unclear_files)
		if unclear_note:
			remarks = unclear_note + "\n\n" + remarks

		# ------------------------------------------------------------------ #
		# Step 4 — save everything back to the review doc
		# ------------------------------------------------------------------ #
		review = frappe.get_doc("AI Accounting Review", review_name)
		review.status = "AI Suggested"
		review.validation_status = "Warning" if (unmatched or unclear_files) else "Passed"
		review.ai_confidence = confidence
		review.total_invoice_amount = invoice_amount
		review.bank_paid_amount = cash_paid
		review.bank_charges_amount = bank_charge_total
		review.vat_on_bank_charges = bank_charge_vat_total
		review.total_suggested_debit = total_debit
		review.total_suggested_credit = total_credit
		review.difference_amount = flt(total_debit - total_credit)
		review.ai_remarks = remarks

		# Supplier header — keep the raw extracted name even when no Supplier could
		# be resolved/created, so the accountant can pick one manually.
		review.supplier_name = (vendor_name or "")[:140]
		review.supplier_vat = vendor_vat or ""
		if supplier:
			review.supplier = supplier

		# Auto-fill cost center and bank account from company defaults
		if not review.cost_center:
			review.cost_center = co_def.get("cost_center") or _get_default_cost_center(pr.company)
		if not review.bank_account:
			review.bank_account = bank_account

		review.set("extracted_documents", [])
		for d in extracted_docs:
			review.append("extracted_documents", d)

		review.set("accounting_rows", [])
		for row in accounting_rows:
			row_data = {k: v for k, v in row.items() if k != "is_vat"}
			review.append("accounting_rows", row_data)

		review.save(ignore_permissions=True)
		frappe.db.commit()

		_log(
			f"[{review_name}] SUCCESS — status='{review.status}' validation='{review.validation_status}' "
			f"confidence={review.ai_confidence} debit={flt(total_debit):.2f} credit={flt(total_credit):.2f} "
			f"balanced={'yes' if abs(flt(total_debit - total_credit)) <= 0.01 else 'NO'}"
		)

	except Exception:
		tb = frappe.get_traceback(with_context=True)
		# 1) full traceback to the dedicated log file (greppable by review name)
		_log(f"[{review_name}] FAILED while processing PR {pr_name}\n{tb}", "error")
		# 2) core Error Log doctype, linked to the review so it's clickable in Desk
		el = _error_log(
			title=f"Smart Journal: PR Review failed — {review_name}",
			message=tb,
			reference_doctype="AI Accounting Review",
			reference_name=review_name,
		)
		# 3) surface a short message + the Error Log link on the review itself
		_fail(review_name, tb[:500], error_log=el)


# ---------------------------------------------------------------------------
# AI extraction helpers
# ---------------------------------------------------------------------------

def _expense_type_list():
	"""Pipe-separated list of enabled Expense Types for the AI prompt.

	Pulls from the Expense Type master so types added in the UI are recognised
	by extraction. Falls back to built-in defaults if the master is empty.
	"""
	names = []
	try:
		names = frappe.get_all(
			"Expense Type", filters={"disabled": 0}, pluck="name", order_by="creation"
		)
	except Exception:
		names = []
	if not names:
		names = [
			"Fuel", "Food/Meal", "Hospitality", "Electricity", "Water",
			"Phone/Internet", "Car Maintenance", "Government Fee", "Social Insurance",
			"Salary", "Overtime", "Airline Ticket", "Hotel", "Shipping", "Customs",
			"Office Supplies", "Other",
		]
	return " | ".join(names)


def _extract_receipt(images, settings):
	"""Call LLM vision on receipt images and return parsed dict."""
	provider = settings.provider
	api_key = settings.get_api_key()
	model = settings.get_default_model()
	max_tokens = int(settings.max_tokens or 800)
	pages = images[:2]
	prompt = _RECEIPT_PROMPT.replace("__EXPENSE_TYPES__", _expense_type_list())

	if provider == "Anthropic":
		import anthropic
		client = anthropic.Anthropic(api_key=api_key, base_url=settings.base_url or None)
		content = [{"type": "text", "text": prompt}]
		for img in pages:
			content.append({
				"type": "image",
				"source": {
					"type": "base64",
					"media_type": _img_media_type(img),
					"data": base64.b64encode(img).decode(),
				},
			})
		msg = client.messages.create(
			model=model,
			max_tokens=max_tokens,
			messages=[{"role": "user", "content": content}],
		)
		text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

	elif provider == "OpenAI":
		from openai import OpenAI
		from smart_journal.smart_journal.doctype.ai_settings.ai_settings import openai_chat_create
		client = OpenAI(api_key=api_key, base_url=settings.base_url or None)
		content = [{"type": "text", "text": prompt}]
		for img in pages:
			b64 = base64.b64encode(img).decode()
			content.append({"type": "image_url", "image_url": {"url": f"data:{_img_media_type(img)};base64,{b64}"}})
		resp = openai_chat_create(
			client, model=model, max_tokens=max_tokens,
			messages=[{"role": "user", "content": content}],
		)
		text = resp.choices[0].message.content
	else:
		frappe.throw(_("Unknown AI provider: {0}").format(provider))

	return _parse_json(text)


def _extract_from_text(pr, settings):
	"""Extract line items from PR description text when no attachments exist."""
	text = (
		getattr(pr, "purchase_request_after_rejection", None)
		or getattr(pr, "purchase_details", None)
		or getattr(pr, "purchase_reason", None)
		or ""
	)
	if not text or len(text.strip()) < 5:
		return []

	prompt = _TEXT_PROMPT.format(
		title=getattr(pr, "title", pr.name) or pr.name,
		text=text[:2000],
	).replace("__EXPENSE_TYPES__", _expense_type_list())

	provider = settings.provider
	api_key = settings.get_api_key()
	model = settings.get_default_model()

	try:
		if provider == "Anthropic":
			import anthropic
			client = anthropic.Anthropic(api_key=api_key, base_url=settings.base_url or None)
			msg = client.messages.create(
				model=model, max_tokens=800,
				messages=[{"role": "user", "content": prompt}],
			)
			text_resp = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
		elif provider == "OpenAI":
			from openai import OpenAI
			from smart_journal.smart_journal.doctype.ai_settings.ai_settings import openai_chat_create
			client = OpenAI(api_key=api_key, base_url=settings.base_url or None)
			resp = openai_chat_create(
				client, model=model, max_tokens=800,
				messages=[{"role": "user", "content": prompt}],
			)
			text_resp = resp.choices[0].message.content
		else:
			return []

		result = _parse_json(text_resp)
		return result.get("line_items", [])
	except Exception:
		_error_log(
			title="Smart Journal: PR text extraction failed",
			reference_doctype="Purchase Request",
			reference_name=pr.name,
		)
		return []


# ---------------------------------------------------------------------------
# Line collection (attachments → per-expense-type lines)
# ---------------------------------------------------------------------------

def _attachment_sources(pr):
	"""Normalised list of files to extract from.

	Prefers the 'Invoice Attachments' child table on the PR (one row per file,
	with an optional document_type hint). Falls back to files attached via the
	sidebar so older PRs still work.
	"""
	sources = []
	for row in (pr.get("invoice_attachments") or []):
		if row.attachment:
			sources.append({
				"file_url": row.attachment,
				"file_name": row.attachment.split("/")[-1],
				"doc_type_hint": (getattr(row, "document_type", None) or "").strip() or None,
				"row": row,
			})
	if not sources:
		for att in frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Purchase Request", "attached_to_name": pr.name},
			fields=["file_name", "file_url"],
		):
			sources.append({
				"file_url": att.file_url,
				"file_name": att.file_name,
				"doc_type_hint": None,
				"row": None,
			})
	return sources


def _collect_attachment_lines(pr, settings, rules):
	"""Extract every processable attachment into per-expense-type line items.

	Returns a dict with: extracted_docs, expense_line_items, bank_slip_line_items,
	bank_charge_total, bank_charge_vat_total, processed_count.
	"""
	company = pr.company
	sources = _attachment_sources(pr)

	extracted_docs = []
	expense_line_items = []
	bank_slip_line_items = []
	bank_charge_total = 0.0
	bank_charge_vat_total = 0.0
	processed_count = 0
	vendors = []  # (name, vat, weight) — used to pick the document's primary supplier
	unclear_files = []  # [(file_name, reason)] — surfaced as a "make it clearer" warning

	for src in sources:
		file_name = src["file_name"]
		file_url = src["file_url"]
		row = src["row"]
		if not _is_processable(file_name):
			if row:
				row.status = "Skipped (unsupported)"
				row.lines_found = 0
			continue
		processed_count += 1
		lines_this_file = 0
		try:
			content = _read_file(file_url)
			images = _file_to_images(content, file_name)
			if not images:
				if row:
					row.status = "Unreadable"
				unclear_files.append((file_name, "could not be opened/converted"))
				continue

			data = _extract_receipt(images, settings)
			# An explicit row hint overrides the AI's guess at document type.
			doc_type = src["doc_type_hint"] or data.get("document_type", "Receipt")

			# Normalise to per-expense-type lines; fall back to the legacy
			# single-object shape so older models / extractions still work.
			raw_lines = data.get("line_items") or []
			if not raw_lines:
				raw_lines = [{
					"expense_type": data.get("expense_type", "Other"),
					"description": data.get("description"),
					"amount_before_vat": data.get("amount_before_vat"),
					"vat_amount": data.get("vat_amount"),
					"total_amount": data.get("total_amount"),
				}]

			for li in raw_lines:
				amount = flt(li.get("amount_before_vat") or 0)
				vat = flt(li.get("vat_amount") or 0)
				total = flt(li.get("total_amount") or 0)
				# Derive the net amount from the gross total when the model only gave
				# us a total. Back the VAT out of the gross if a VAT figure was
				# provided, so a VAT-inclusive total isn't booked entirely as expense
				# with zero recoverable input VAT.
				if amount == 0 and total > 0:
					amount = flt(total - vat)
				if amount == 0 and vat == 0 and total == 0:
					continue

				expense_type = li.get("expense_type") or "Other"
				description = li.get("description") or data.get("description") or file_name
				account = _route_by_type(expense_type, company, rules, description)

				extracted_docs.append({
					"attachment": file_url,
					"document_type": doc_type,
					"vendor": li.get("vendor") or data.get("vendor", ""),
					"invoice_number": data.get("invoice_number", ""),
					"invoice_date": _to_date(data.get("invoice_date")),
					"description": description,
					"amount_before_vat": amount,
					"vat_amount": vat,
					"total_amount": total or (amount + vat),
					"extracted_text": expense_type,
					"confidence": 80,
				})

				line_item = {
					"expense_type": expense_type,
					"description": description,
					"amount": amount,
					"vat": vat,
					"source": file_name,
					"file_url": file_url,
					"account": account,
				}
				if doc_type == "Bank Slip":
					bank_slip_line_items.append(line_item)
				else:
					expense_line_items.append(line_item)
				lines_this_file += 1

			bank_charge_total += flt(data.get("bank_charge") or 0)
			bank_charge_vat_total += flt(data.get("bank_charge_vat") or 0)

			# Remember this file's seller so we can resolve/create a Supplier later.
			# Weight by total amount so the largest invoice wins when a PR mixes
			# several vendors (bank slips, which name the payer, weigh nothing).
			doc_vendor = (data.get("vendor") or "").strip()
			if doc_vendor and doc_type != "Bank Slip":
				weight = sum(
					flt(li.get("total_amount") or 0) or flt(li.get("amount_before_vat") or 0)
					for li in raw_lines
				)
				vendors.append((doc_vendor, _digits(data.get("vendor_vat")), weight or 1.0))

			# Flag poor-quality scans so the accountant uploads a clearer copy:
			# either the model said it was illegible, or we got no readable amounts.
			if data.get("legible") is False:
				unclear_files.append((file_name, "image is blurry / hard to read"))
			elif lines_this_file == 0:
				unclear_files.append((file_name, "no readable amounts found"))

			if row:
				row.lines_found = lines_this_file
				row.status = "Extracted" if lines_this_file else "No lines found"
			_log(f"    file '{file_name}' (type={doc_type}) → {lines_this_file} line(s)")

		except Exception:
			_error_log(
				title=f"Smart Journal: attachment extraction failed — {file_name}",
				reference_doctype="Purchase Request",
				reference_name=pr.name,
			)
			_log(f"    file '{file_name}' → EXTRACTION FAILED: {frappe.get_traceback().splitlines()[-1]}", "error")
			if row:
				row.status = "Failed"
			unclear_files.append((file_name, "could not be read"))

	vendor_name, vendor_vat = _pick_primary_vendor(vendors)
	return {
		"extracted_docs": extracted_docs,
		"expense_line_items": expense_line_items,
		"bank_slip_line_items": bank_slip_line_items,
		"bank_charge_total": bank_charge_total,
		"bank_charge_vat_total": bank_charge_vat_total,
		"processed_count": processed_count,
		"vendor_name": vendor_name,
		"vendor_vat": vendor_vat,
		"unclear_files": unclear_files,
	}


def _lines_from_pr_breakdown(pr, rules):
	"""Build line items from the user-corrected PR 'Expense Breakdown' table.

	Returns the same shape as _collect_attachment_lines (minus bank_slip_line_items)
	so process_pr_review can consume either source. Empty expense_line_items means
	the table is unused — caller should fall back to attachment extraction.
	"""
	rows = pr.get("expense_breakdown") or []
	expense_line_items = []
	extracted_docs = []
	for r in rows:
		amount = flt(r.amount)
		vat = flt(r.vat_amount)
		if amount == 0 and vat == 0 and flt(r.total_amount) == 0:
			continue
		etype = r.expense_type or "Other"
		desc = r.description or etype
		account = _route_by_type(etype, pr.company, rules, desc)
		expense_line_items.append({
			"expense_type": etype,
			"description": desc,
			"amount": amount,
			"vat": vat,
			"source": r.source or "PR Breakdown",
			"file_url": r.attachment,
			"account": account,
		})
		extracted_docs.append({
			"attachment": r.attachment,
			"document_type": "Invoice",
			"vendor": "",
			"description": desc,
			"amount_before_vat": amount,
			"vat_amount": vat,
			"total_amount": flt(r.total_amount) or (amount + vat),
			"extracted_text": etype,
			"confidence": 90,
		})

	return {
		"extracted_docs": extracted_docs,
		"expense_line_items": expense_line_items,
		"bank_charge_total": 0.0,
		"bank_charge_vat_total": 0.0,
		"processed_count": len(rows),
		"vendor_name": "",
		"vendor_vat": "",
		"unclear_files": [],
	}


def _breakdown_status_text(requested, breakdown_total):
	"""Human-readable mismatch flag between requested_amount and the breakdown."""
	diff = flt(requested) - flt(breakdown_total)
	if abs(diff) <= 0.01:
		return f"✅ Matches requested amount (SAR {flt(breakdown_total):.2f})."
	if diff > 0:
		return (
			f"⚠️ Breakdown SAR {flt(breakdown_total):.2f} is SAR {diff:.2f} LESS than "
			f"requested SAR {flt(requested):.2f} — some expenses may be missing."
		)
	return (
		f"⚠️ Breakdown SAR {flt(breakdown_total):.2f} is SAR {abs(diff):.2f} MORE than "
		f"requested SAR {flt(requested):.2f} — check for double-counting."
	)


def _set_if_exists(doc, fieldname, value):
	"""Set a custom field only if it exists on the doctype (avoids hard failures)."""
	if doc.meta.has_field(fieldname):
		doc.set(fieldname, value)


# ---------------------------------------------------------------------------
# Grouping & formatting helpers
# ---------------------------------------------------------------------------

def _build_accounting_rows(line_items, company, company_defaults=None):
	"""Group line items by GL account into debit rows + one VAT row."""
	groups = defaultdict(lambda: {"amount": 0.0, "descriptions": [], "sources": [], "file_urls": []})

	for item in line_items:
		key = item.get("account") or f"__UNMATCHED__{item['expense_type']}"
		groups[key]["amount"] += flt(item["amount"])
		groups[key]["descriptions"].append(item["description"])
		groups[key]["sources"].append(item["source"])
		if item.get("file_url"):
			groups[key]["file_urls"].append(item["file_url"])
		if not item.get("account"):
			groups[key]["expense_type"] = item["expense_type"]

	rows = []
	for key, data in groups.items():
		is_unmatched = key.startswith("__UNMATCHED__")
		account = None if is_unmatched else key
		desc = "; ".join(dict.fromkeys(data["descriptions"]))[:200]
		# Use the first attachment URL so the accountant can preview the invoice
		first_attachment = next((u for u in data["file_urls"] if u), None)
		rows.append({
			"source_type": "Invoice",
			"description": desc,
			"attachment": first_attachment,
			"suggested_account": account,
			"accountant_account": None,
			"final_account": account,
			"account_company": company,
			"debit": flt(data["amount"]),
			"credit": 0,
			"confidence": 80 if account else 40,
			"ai_reason": "Matched from: " + ", ".join(dict.fromkeys(data["sources"])),
			"accountant_approved": 0,
			"include_in_pr_amount": 1,
			"is_vat": False,
		})

	# Aggregate VAT across all items
	total_vat = sum(flt(i.get("vat", 0)) for i in line_items)
	if total_vat > 0:
		vat_account = _get_vat_account(company, company_defaults)
		rows.append({
			"source_type": "Tax",
			"description": "VAT Input 15%",
			"suggested_account": vat_account,
			"accountant_account": None,
			"final_account": vat_account,
			"account_company": company,
			"debit": total_vat,
			"credit": 0,
			"confidence": 90,
			"ai_reason": "VAT extracted from receipts",
			"accountant_approved": 0,
			"include_in_pr_amount": 0,
			"is_vat": True,
		})

	return rows


def _make_bank_charge_rows(charge, charge_vat, company, company_defaults=None):
	"""Build debit rows for the bank commission and its VAT.

	These are real costs paid from the bank but are NOT part of the Purchase
	Request amount, so include_in_pr_amount = 0 (matching the accountant's JVR
	salary entry: separate 'bank fees' and 'VAT Input' lines).
	"""
	rows = []
	charge = flt(charge)
	charge_vat = flt(charge_vat)
	if charge > 0:
		acct = _get_bank_charge_account(company)
		rows.append({
			"source_type": "Bank Charge",
			"description": "Bank charges / عمولة بنكية",
			"suggested_account": acct,
			"accountant_account": None,
			"final_account": acct,
			"account_company": company,
			"debit": charge,
			"credit": 0,
			"is_bank_charge": 1,
			"include_in_pr_amount": 0,
			"confidence": 85 if acct else 40,
			"ai_reason": "Bank commission extracted from the payment slip",
			"accountant_approved": 0,
			"is_vat": False,
		})
	if charge_vat > 0:
		vat_acct = _get_vat_account(company, company_defaults)
		rows.append({
			"source_type": "Tax",
			"description": "VAT on bank charges 15%",
			"suggested_account": vat_acct,
			"accountant_account": None,
			"final_account": vat_acct,
			"account_company": company,
			"debit": charge_vat,
			"credit": 0,
			"is_bank_charge": 1,
			"include_in_pr_amount": 0,
			"confidence": 90 if vat_acct else 40,
			"ai_reason": "VAT on the bank commission",
			"accountant_approved": 0,
			"is_vat": True,
		})
	return rows


def _settlement_row(account, company, *, debit=0, credit=0, source_type="Bank Payment",
                    description="", reason="", party_type=None, party=None):
	"""Build a credit/settlement accounting row (bank or supplier-payable line)."""
	return {
		"source_type": source_type,
		"description": description,
		"attachment": None,
		"suggested_account": account,
		"accountant_account": None,
		"final_account": account,
		"account_company": company,
		"against_account": None,  # ERPNext computes the comma-joined contra on save
		"party_type": party_type,
		"party": party,
		"debit": flt(debit),
		"credit": flt(credit),
		"confidence": 95,
		"ai_reason": reason,
		"accountant_approved": 0,
		"include_in_pr_amount": 0,
		"is_vat": False,
	}


def _append_settlement_lines(debit_rows, bank_account, company, supplier=None, payable_account=None):
	"""Append the credit lines that settle the expense / VAT debits.

	Two shapes, mirroring how the accountant actually books these:

	* No supplier (or no payable account) → ONE consolidated bank credit for the
	  full amount (DR expense, DR VAT, … , CR bank total). When a supplier IS
	  known it is still tagged as the Party on that bank credit so the entry
	  records who was paid.

	* Supplier + payable account → the supplier payable round-trips exactly like
	  the reference JE (RTC-JV-2026-1025):
	      DR Expense / DR VAT                 (no party)
	      CR Supplier Payable = invoice total (party=Supplier)  ← bill created
	      DR Supplier Payable = invoice total (party=Supplier)  ← bill settled
	      CR Bank             = total paid     (no party)        ← cash out
	  Bank charges are paid by the bank but are NOT owed to the supplier, so they
	  stay out of the payable and only widen the bank credit.

	If *bank_account* is unknown we return the debit rows unchanged (the credit
	side is then synthesised at JE-creation time as a single line — legacy path).
	"""
	if not bank_account:
		return debit_rows

	total = sum(flt(r["debit"]) for r in debit_rows)
	if total <= 0:
		return debit_rows

	# Each debit's contra account is the bank (cosmetic on the review; the JE
	# recomputes against_account on save).
	for r in debit_rows:
		if flt(r["debit"]) > 0:
			r["against_account"] = bank_account

	# Simple path — no supplier payable: single consolidated bank credit, with
	# the supplier tagged as party when we have one but no payable account.
	if not (supplier and payable_account):
		return debit_rows + [_settlement_row(
			bank_account, company, credit=total,
			description="Total paid from bank",
			reason="Consolidated bank credit for all expense / VAT lines above",
			party_type="Supplier" if supplier else None,
			party=supplier or None,
		)]

	# Supplier path — payable round-trip. Payable = invoice debits only
	# (exclude bank charges, which the supplier is not owed).
	payable_total = sum(flt(r["debit"]) for r in debit_rows if not r.get("is_bank_charge"))

	rows = list(debit_rows)
	rows.append(_settlement_row(
		payable_account, company, credit=payable_total, source_type="Other",
		description="Invoice payable to supplier",
		reason="Supplier payable created by the invoice (party=Supplier)",
		party_type="Supplier", party=supplier,
	))
	rows.append(_settlement_row(
		payable_account, company, debit=payable_total, source_type="Other",
		description="Settle supplier payable",
		reason="Supplier payable cleared by the bank payment (party=Supplier)",
		party_type="Supplier", party=supplier,
	))
	rows.append(_settlement_row(
		bank_account, company, credit=total,
		description="Total paid from bank",
		reason="Consolidated bank credit for the supplier payment + any bank charges",
	))
	return rows


# ---------------------------------------------------------------------------
# Supplier resolution
# ---------------------------------------------------------------------------

def _pick_primary_vendor(vendors):
	"""From [(name, vat, weight), ...] pick the dominant supplier (name, vat).

	The vendor that appears on the largest-value invoice wins. Returns ("", "")
	when nothing usable was extracted.
	"""
	best_name, best_vat, best_weight = "", "", -1.0
	for name, vat, weight in vendors:
		if flt(weight) > best_weight:
			best_name, best_vat, best_weight = name, vat or "", flt(weight)
	return best_name, best_vat


def _default_supplier_group():
	"""Supplier Group for auto-created suppliers — Buying Settings default first."""
	grp = frappe.db.get_single_value("Buying Settings", "supplier_group")
	if grp:
		return grp
	return (
		frappe.db.get_value("Supplier Group", {"is_group": 0}, "name")
		or "All Supplier Groups"
	)


def _resolve_supplier(vendor_name, vendor_vat=None):
	"""Find an existing Supplier by VAT or name; auto-create one if missing.

	Matching order:
	  1. By tax_id (VAT) — most reliable, survives Arabic/English name drift.
	  2. By exact docname or supplier_name.
	  3. Otherwise create a new Supplier (default group, tax_id from the invoice).

	Returns the Supplier docname, or None when no vendor was extracted or
	creation failed (failure is logged but never aborts the review).
	"""
	vendor_name = (vendor_name or "").strip()
	vat = _digits(vendor_vat)

	# 1) Match by VAT
	if vat:
		existing = frappe.db.get_value("Supplier", {"tax_id": vat}, "name")
		if existing:
			return existing

	if not vendor_name:
		return None

	# 2) Match by name (docname or supplier_name)
	existing = (
		frappe.db.get_value("Supplier", {"name": vendor_name}, "name")
		or frappe.db.get_value("Supplier", {"supplier_name": vendor_name}, "name")
	)
	if existing:
		return existing

	# 3) Create
	try:
		supplier = frappe.new_doc("Supplier")
		supplier.supplier_name = vendor_name[:140]
		supplier.supplier_group = _default_supplier_group()
		if supplier.meta.has_field("supplier_type"):
			supplier.supplier_type = "Company"
		if vat and supplier.meta.has_field("tax_id"):
			supplier.tax_id = vat
		supplier.insert(ignore_permissions=True)
		_log(f"created Supplier '{supplier.name}' for vendor '{vendor_name}' (vat={vat or '-'})")
		return supplier.name
	except Exception:
		_error_log(
			title=f"Smart Journal: could not auto-create Supplier — {vendor_name[:80]}",
			reference_doctype="Supplier",
		)
		_log(f"could not auto-create Supplier for '{vendor_name}': "
		     f"{frappe.get_traceback().splitlines()[-1]}", "error")
		return None


def _unclear_warning(unclear_files):
	"""Markdown warning asking the user to re-upload unclear invoices, or ''.

	*unclear_files* is a list of (file_name, reason) tuples.
	"""
	if not unclear_files:
		return ""
	lines = [
		"⚠️ **Please make sure the invoice is clear.** "
		"Some attachments could not be read reliably — re-upload a clearer "
		"(sharp, well-lit, full-page) copy and run the review again:"
	]
	for file_name, reason in unclear_files:
		lines.append(f"- **{file_name}** — {reason}")
	lines.append("\n_The amounts below may be inaccurate until a clearer copy is provided._")
	return "\n".join(lines)


def _build_remarks(line_items, accounting_rows, attachment_count):
	"""Build human-readable markdown for the ai_remarks field."""
	lines = ["### AI Extraction Summary\n"]
	lines.append(f"**Attachments processed:** {attachment_count}")
	lines.append(f"**Expense lines found:** {len(line_items)}\n")

	# Debit side = expense rows (exclude VAT, the bank credit, and the supplier
	# payable settlement rows so the summary lists only real expenses).
	_settlement_types = {"Bank Payment", "Other"}
	expense_rows = [
		r for r in accounting_rows
		if not r.get("is_vat") and flt(r.get("debit")) > 0
		and r.get("source_type") not in _settlement_types
	]
	vat_rows = [r for r in accounting_rows if r.get("is_vat")]
	credit_rows = [
		r for r in accounting_rows
		if flt(r.get("credit")) > 0 and r.get("source_type") == "Bank Payment"
	]

	if expense_rows:
		lines.append("**Suggested Journal Entry (Debit side):**")
		for r in expense_rows:
			acct = r.get("suggested_account") or "⚠️ No account matched — please select manually"
			lines.append(f"- {r['description'][:70]}  →  **{acct}**  |  SAR {r['debit']:.2f}")

	if credit_rows:
		bank = credit_rows[0].get("final_account") or credit_rows[0].get("suggested_account") or "Bank"
		credit_total = sum(flt(r.get("credit")) for r in credit_rows)
		lines.append(
			f"\n**Credit side:** {len(credit_rows)} bank line(s) → **{bank}**  |  SAR {credit_total:.2f}"
		)

	if vat_rows:
		vr = vat_rows[0]
		acct = vr.get("suggested_account") or "⚠️ No VAT account"
		lines.append(f"- VAT Input 15%  →  **{acct}**  |  SAR {vr['debit']:.2f}")

	unmatched = [r for r in expense_rows if not r.get("suggested_account")]
	if unmatched:
		lines.append(
			"\n⚠️ **Some items have no matched account.** "
			"Please assign accounts in the Accounting Rows table below, "
			"or add keywords in **AI Settings → Routing Rules**."
		)
	else:
		lines.append("\n✅ All items matched to accounts. Review the suggestions and click **Create Journal Entry**.")

	return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _load_routing_rules_v2():
	"""Return list of dicts with company, expense_type, account, keyword."""
	settings = frappe.get_single("AI Settings")
	rules = []
	for row in (settings.get("expense_routing") or []):
		if row.account:
			rules.append({
				"company": row.company or "",
				"expense_type": row.expense_type or "",
				"account": row.account,
				"keyword": (row.keyword or "").strip().lower(),
			})
	return rules


def _route_by_type(expense_type, company, rules, description=""):
	"""Pick the GL account for an expense line.

	Priority:
	  0. A rule whose keyword appears in the receipt DESCRIPTION (most specific —
	     e.g. 'زكاة' routes a ZATCA payment to the Zakat account even when the AI
	     mislabels its expense_type as 'Government Fee'). Company-specific first,
	     then any-company. This is the behaviour documented on the keyword field.
	  1. Exact company + expense_type
	  2. Same expense_type, no company restriction
	  3. Keyword appears in the expense_type string (legacy fallback)
	  4. Company 'Other' catch-all
	"""
	etype = expense_type or ""
	desc = (description or "").lower()

	# 0. Keyword present in the receipt description (strongest, most specific).
	if desc:
		for r in rules:
			if r["company"] == company and _kw_hit(r["keyword"], desc):
				return r["account"]
		for r in rules:
			if not r["company"] and _kw_hit(r["keyword"], desc):
				return r["account"]

	# 1. Exact: company + expense_type
	for r in rules:
		if r["company"] == company and r["expense_type"] == etype:
			return r["account"]
	# 2. Same type, no company restriction
	for r in rules:
		if not r["company"] and r["expense_type"] == etype:
			return r["account"]
	# 3. Keyword in the expense_type string (legacy fallback)
	search = etype.lower()
	for r in rules:
		if _kw_hit(r["keyword"], search):
			return r["account"]
	# 4. Company "Other" catch-all
	for r in rules:
		if r["company"] == company and r["expense_type"] == "Other":
			return r["account"]
	return None


def _kw_hit(keyword, text):
	"""True if any comma/pipe-separated keyword appears in text (case-insensitive).

	Lets one rule carry both Arabic and English triggers, e.g. 'زكاة,zakat,zatca',
	so it matches whether the AI wrote an Arabic or an English line description.
	"""
	if not keyword or not text:
		return False
	for kw in re.split(r"[,|]", keyword):
		kw = kw.strip().lower()
		if kw and kw in text:
			return True
	return False


def _load_company_defaults():
	"""Return dict: company → {credit_account, vat_account, cost_center}."""
	settings = frappe.get_single("AI Settings")
	result = {}
	for row in (settings.get("company_defaults") or []):
		if row.company:
			result[row.company] = {
				"credit_account": row.credit_account,
				"vat_account": row.vat_account,
				"cost_center": row.cost_center,
			}
	return result


def _get_vat_account(company, company_defaults=None):
	"""Return VAT input account — from company defaults first, then DB lookup."""
	if company_defaults and company in company_defaults:
		vat = company_defaults[company].get("vat_account")
		if vat:
			return vat
	return (
		frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Tax", "name": ["like", "%VAT%"]},
			"name",
		)
		or frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Tax", "name": ["like", "%ضريبة%"]},
			"name",
		)
	)


def _get_payable_account(company, company_defaults=None):
	"""Supplier payable (creditor) account for the company.

	Order: AI Settings company default → Company.default_payable_account →
	an Arabic/English creditor account → any Payable account. Matches the
	reference JE's '1002151 - موردين محليين - Supplier Creditor'.
	"""
	if company_defaults and company in company_defaults:
		pa = company_defaults[company].get("payable_account")
		if pa:
			return pa
	default = frappe.db.get_value("Company", company, "default_payable_account")
	if default:
		return default
	for pat in ["%موردين%", "%Supplier Creditor%", "%Creditors%", "%Payable%", "%دائن%"]:
		acc = frappe.db.get_value(
			"Account",
			{"company": company, "is_group": 0, "account_type": "Payable", "name": ["like", pat]},
			"name",
		)
		if acc:
			return acc
	return frappe.db.get_value(
		"Account", {"company": company, "is_group": 0, "account_type": "Payable"}, "name"
	)


def _get_bank_charge_account(company):
	"""Find a bank-charges / commission expense account for the company."""
	for pat in ["%Bank Charges%", "%Bank Fee%", "%bank fees%", "%رسوم بنكية%", "%عمولة%"]:
		acc = frappe.db.get_value(
			"Account", {"company": company, "is_group": 0, "name": ["like", pat]}, "name"
		)
		if acc:
			return acc
	return None


def _get_default_cost_center(company):
	"""Return the Main cost center for the given company."""
	abbr = frappe.db.get_value("Company", company, "abbr") or ""
	return (
		frappe.db.get_value("Cost Center", {"company": company, "cost_center_name": "Main - " + abbr}, "name")
		or frappe.db.get_value("Cost Center", {"company": company, "cost_center_name": "Main"}, "name")
		or frappe.db.get_value("Cost Center", {"company": company, "is_group": 0, "cost_center_name": ["like", "%Main%"]}, "name")
	)


def _get_default_bank_account(company):
	"""Return the Al Enmaa Bank account for the given company (most common credit account)."""
	return (
		frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "name": ["like", "%Enmaa%"]}, "name")
		or frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "name": ["like", "%إنماء%"]}, "name")
		or frappe.db.get_value("Account", {"company": company, "account_type": "Bank"}, "name")
	)


def _is_processable(filename):
	ext = (filename or "").rsplit(".", 1)[-1].lower()
	return ext in _PROCESSABLE_EXTS


def _read_file(file_url):
	"""Read file content, trying multiple resolution strategies.

	Frappe stores files with two URL schemes:
	  /files/<name>         → sites/<site>/public/files/<name>
	  /private/files/<name> → sites/<site>/private/files/<name>

	In practice many attachments are saved with is_private=0 (public URL)
	but the actual bytes are only in private/files/ on disk (e.g. when
	uploaded via the mobile app or ERPNext PWA). We therefore:
	  1. Try Frappe's standard get_content() (works for truly public files
	     and any file whose content Frappe can resolve).
	  2. If that fails, try reading directly from private/files/ by filename
	     as a fallback.
	  3. Raise a clear error so the caller can log and skip gracefully.
	"""
	import os

	site_path = frappe.utils.get_site_path()

	# Strategy 1 — standard Frappe resolution
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
		if content:
			return content
	except Exception:
		pass

	# Strategy 2 — try private/files/ by filename when public URL failed
	filename = file_url.split("/")[-1]
	private_path = os.path.join(site_path, "private", "files", filename)
	if os.path.isfile(private_path):
		with open(private_path, "rb") as fh:
			return fh.read()

	# Strategy 3 — try public/files/ directly
	public_path = os.path.join(site_path, "public", "files", filename)
	if os.path.isfile(public_path):
		with open(public_path, "rb") as fh:
			return fh.read()

	raise FileNotFoundError(
		f"File not found on disk for URL '{file_url}'. "
		f"Checked: {private_path} and {public_path}"
	)


def _fail(review_name, message, error_log=None):
	_log(f"[{review_name}] marking review as FAILED: {str(message).splitlines()[0][:200]}", "error")
	try:
		review = frappe.get_doc("AI Accounting Review", review_name)
		review.status = "Draft"
		review.validation_status = "Failed"
		remark = f"❌ Processing failed:\n\n{message[:1000]}"
		if error_log:
			# Deep-link to the full traceback in the core Error Log doctype.
			remark += f"\n\n🔎 Full details: [Error Log {error_log}](/app/error-log/{error_log})"
		review.ai_remarks = remark
		review.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		_log(f"[{review_name}] could not write failure status to review doc\n{frappe.get_traceback()}", "error")
