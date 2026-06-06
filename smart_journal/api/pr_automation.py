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
    _file_to_images,
    _parse_json,
    _to_date,
)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_RECEIPT_PROMPT = """\
You are reading an expense receipt or invoice for a Saudi company (amounts in SAR).
The document may be in Arabic, English, or both.

Return ONLY a JSON object — no markdown, no commentary:
{
  "document_type": "Invoice" | "Receipt" | "Bank Slip" | "Other",
  "vendor": "<seller name or empty string>",
  "expense_type": "<one of the categories below>",
  "description": "<short English description of what was purchased>",
  "amount_before_vat": <number>,
  "vat_amount": <number>,
  "total_amount": <number>,
  "invoice_number": "<string or empty>",
  "invoice_date": "<YYYY-MM-DD or empty>"
}

expense_type must be exactly one of:
  Fuel | Food/Meal | Hospitality | Electricity | Water | Phone/Internet |
  Car Maintenance | Government Fee | Airline Ticket | Hotel | Shipping |
  Salary/Overtime | Customs | Office Supplies | Other

Rules:
- Numbers are plain numbers (no currency symbol, no commas).
- If a value is missing use "" for strings and 0 for numbers.
- amount_before_vat + vat_amount should equal total_amount (within rounding).
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
  Fuel | Food/Meal | Hospitality | Electricity | Water | Phone/Internet |
  Car Maintenance | Government Fee | Airline Ticket | Hotel | Shipping |
  Salary/Overtime | Customs | Office Supplies | Other

If no amounts are found return {{"line_items": []}}.
"""

_PROCESSABLE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp", "pdf"}


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

	return {"review_name": review.name, "already_exists": False}


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
	try:
		settings = frappe.get_single("AI Settings")
		if not settings.enabled:
			_fail(review_name, "AI Settings is disabled. Please enable it and add an API key.")
			return

		pr = frappe.get_doc("Purchase Request", pr_name)
		rules = _load_routing_rules_v2()
		company_defaults = _load_company_defaults()

		# ------------------------------------------------------------------ #
		# Step 1 — process every attachment
		# ------------------------------------------------------------------ #
		attachments = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Purchase Request",
				"attached_to_name": pr_name,
			},
			fields=["name", "file_name", "file_url", "is_private"],
		)

		extracted_docs = []   # rows for extracted_documents child table
		# Two buckets: real expense docs (Invoice/Receipt) vs. payment proofs (Bank Slip).
		# We prefer expense docs for GL amounts; bank slips are kept only as fallback
		# so a PR with only a bank slip still posts correctly.
		expense_line_items = []
		bank_slip_line_items = []
		processed_count = 0

		for att in attachments:
			if not _is_processable(att.file_name):
				continue
			processed_count += 1
			try:
				content = _read_file(att.file_url)
				images = _file_to_images(content, att.file_name)
				if not images:
					continue

				data = _extract_receipt(images, settings)
				amount = flt(data.get("amount_before_vat") or 0)
				vat = flt(data.get("vat_amount") or 0)
				total = flt(data.get("total_amount") or 0)
				# If only total is known, treat it as net and skip VAT to avoid double-counting
				if amount == 0 and total > 0:
					amount = total
					vat = 0

				doc_type = data.get("document_type", "Receipt")
				expense_type = data.get("expense_type", "Other")
				description = data.get("description") or att.file_name
				account = _route_by_type(expense_type, pr.company, rules)

				extracted_docs.append({
					"attachment": att.file_url,
					"document_type": doc_type,
					"vendor": data.get("vendor", ""),
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
					"source": att.file_name,
					"file_url": att.file_url,
					"account": account,
				}

				# Bank slips are payment proofs — keep separate to avoid double-counting
				# when the same PR has both a bank slip and an invoice for the same expense.
				if doc_type == "Bank Slip":
					bank_slip_line_items.append(line_item)
				else:
					expense_line_items.append(line_item)

			except Exception:
				frappe.log_error(frappe.get_traceback(), f"PR Review: {att.file_name}")

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
			# Bank slip is the ground truth for amounts
			bank_slip_total = sum(flt(i["amount"]) for i in bank_slip_line_items)
			bank_slip_vat = sum(flt(i["vat"]) for i in bank_slip_line_items)

			expense_total = sum(flt(i["amount"]) for i in expense_line_items)
			if expense_total > 0 and bank_slip_total > 0:
				# Scale expense line amounts to match the bank slip total
				scale = bank_slip_total / expense_total
				for item in expense_line_items:
					item["amount"] = flt(item["amount"] * scale)
				# Replace VAT with bank slip VAT (distributed to first item)
				for idx, item in enumerate(expense_line_items):
					item["vat"] = bank_slip_vat if idx == 0 else 0
			elif bank_slip_total > 0:
				# Expense items have zero amount — use bank slip amounts directly
				for item in expense_line_items:
					item["amount"] = bank_slip_total / len(expense_line_items)
					item["vat"] = bank_slip_vat if expense_line_items.index(item) == 0 else 0

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
				account = _route_by_type(etype, pr.company, rules)
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

		# Guard against double-counting when multiple attachments represent the
		# same payment (e.g. a bank slip + an approval sheet for the same amount).
		# If the extracted total exceeds the PR requested_amount by more than 50%,
		# scale all expense debit rows down proportionally so they sum to
		# requested_amount.
		pr_requested = flt(pr.requested_amount)
		total_debit = sum(r["debit"] for r in accounting_rows if not r.get("is_vat"))
		if pr_requested > 0 and total_debit > pr_requested * 1.5:
			scale = pr_requested / total_debit
			for r in accounting_rows:
				if not r.get("is_vat"):
					r["debit"] = flt(r["debit"] * scale)
			# Recalculate after scaling
			total_debit = sum(r["debit"] for r in accounting_rows if not r.get("is_vat"))

		total_vat = sum(r["debit"] for r in accounting_rows if r.get("is_vat"))
		total_credit = total_debit + total_vat

		unmatched = [r for r in accounting_rows if not r.get("suggested_account") and not r.get("is_vat")]
		confidence = 90 if not unmatched else max(40, 90 - len(unmatched) * 15)

		remarks = _build_remarks(line_items, accounting_rows, processed_count)

		# ------------------------------------------------------------------ #
		# Step 4 — save everything back to the review doc
		# ------------------------------------------------------------------ #
		review = frappe.get_doc("AI Accounting Review", review_name)
		review.status = "AI Suggested"
		review.validation_status = "Passed" if not unmatched else "Warning"
		review.ai_confidence = confidence
		review.total_invoice_amount = total_debit
		review.bank_paid_amount = total_credit
		review.total_suggested_debit = total_credit
		review.total_suggested_credit = total_credit
		review.difference_amount = 0
		review.ai_remarks = remarks

		# Auto-fill cost center and bank account from company defaults
		co_def = company_defaults.get(pr.company, {})
		if not review.cost_center:
			review.cost_center = co_def.get("cost_center") or _get_default_cost_center(pr.company)
		if not review.bank_account:
			review.bank_account = co_def.get("credit_account") or _get_default_bank_account(pr.company)

		review.set("extracted_documents", [])
		for d in extracted_docs:
			review.append("extracted_documents", d)

		review.set("accounting_rows", [])
		for row in accounting_rows:
			row_data = {k: v for k, v in row.items() if k != "is_vat"}
			review.append("accounting_rows", row_data)

		review.save(ignore_permissions=True)
		frappe.db.commit()

	except Exception:
		frappe.log_error(frappe.get_traceback(), f"process_pr_review: {review_name}")
		_fail(review_name, frappe.get_traceback()[:500])


# ---------------------------------------------------------------------------
# AI extraction helpers
# ---------------------------------------------------------------------------

def _extract_receipt(images, settings):
	"""Call LLM vision on receipt images and return parsed dict."""
	provider = settings.provider
	api_key = settings.get_api_key()
	model = settings.get_default_model()
	max_tokens = int(settings.max_tokens or 800)
	pages = images[:2]

	if provider == "Anthropic":
		import anthropic
		client = anthropic.Anthropic(api_key=api_key, base_url=settings.base_url or None)
		content = [{"type": "text", "text": _RECEIPT_PROMPT}]
		for img in pages:
			content.append({
				"type": "image",
				"source": {
					"type": "base64",
					"media_type": "image/png",
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
		content = [{"type": "text", "text": _RECEIPT_PROMPT}]
		for img in pages:
			b64 = base64.b64encode(img).decode()
			content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
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
	)

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
		frappe.log_error(frappe.get_traceback(), "PR text extraction")
		return []


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


def _build_remarks(line_items, accounting_rows, attachment_count):
	"""Build human-readable markdown for the ai_remarks field."""
	lines = ["### AI Extraction Summary\n"]
	lines.append(f"**Attachments processed:** {attachment_count}")
	lines.append(f"**Expense lines found:** {len(line_items)}\n")

	expense_rows = [r for r in accounting_rows if not r.get("is_vat")]
	vat_rows = [r for r in accounting_rows if r.get("is_vat")]

	if expense_rows:
		lines.append("**Suggested Journal Entry (Debit side):**")
		for r in expense_rows:
			acct = r.get("suggested_account") or "⚠️ No account matched — please select manually"
			lines.append(f"- {r['description'][:70]}  →  **{acct}**  |  SAR {r['debit']:.2f}")

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


def _route_by_type(expense_type, company, rules):
	"""Pick GL account by: exact company+type → type only → keyword fallback."""
	# 1. Exact: company + expense_type
	for r in rules:
		if r["company"] == company and r["expense_type"] == expense_type:
			return r["account"]
	# 2. Same type, no company restriction
	for r in rules:
		if not r["company"] and r["expense_type"] == expense_type:
			return r["account"]
	# 3. Keyword fallback in expense_type string
	search = expense_type.lower()
	for r in rules:
		if r["keyword"] and r["keyword"] in search:
			return r["account"]
	# 4. Company "Other" catch-all
	for r in rules:
		if r["company"] == company and r["expense_type"] == "Other":
			return r["account"]
	return None


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


def _fail(review_name, message):
	try:
		review = frappe.get_doc("AI Accounting Review", review_name)
		review.status = "Draft"
		review.validation_status = "Failed"
		review.ai_remarks = f"❌ Processing failed:\n\n{message[:1000]}"
		review.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		pass
