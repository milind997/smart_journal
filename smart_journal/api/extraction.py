# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt
"""
Invoice extraction backend for Smart Journal.

Pipeline:
  1. Read the attached invoice file (PDF or image).
  2. Render PDF pages to images (PyMuPDF).
  3. Decode the ZATCA QR (pyzbar) -> reliable seller VAT / total / VAT amount.
  4. Run an LLM vision pass (provider from AI Settings) -> full structured data.
  5. Merge (QR wins on the fields it carries), detect buyer/seller direction,
     reconcile amounts, and write everything back onto the document.
"""

import base64
import io
import json
import re

import frappe
from frappe import _
from frappe.utils import flt

ROUNDING_TOLERANCE = 0.05

# ----------------------------------------------------------------------------- #
#  Public entry point
# ----------------------------------------------------------------------------- #


@frappe.whitelist()
def extract_invoice(docname):
	"""Extract data from the attached invoice and fill the document fields."""
	doc = frappe.get_doc("Invoice to Journal Entry", docname)
	doc.check_permission("write")

	if not doc.invoice_file:
		frappe.throw(_("Attach an invoice file first."))

	settings = frappe.get_single("AI Settings")
	if not settings.enabled:
		frappe.throw(_("AI Settings is disabled. Enable it and set an API key first."))

	content, filename = _read_file(doc.invoice_file)
	images = _file_to_images(content, filename)
	if not images:
		frappe.throw(_("Could not read any page/image from the attached file."))

	# 1) ZATCA QR (best source for seller VAT, total, VAT amount)
	raw_qr, qr = _decode_qr(images)

	# 2) LLM vision pass (full structured extraction)
	llm = _call_llm(images, settings)

	# 3) Merge — QR wins where it has a value
	supplier_name = qr.get("seller_name") or llm.get("supplier_name")
	supplier_vat = _digits(qr.get("seller_vat")) or _digits(llm.get("supplier_vat"))
	customer_vat = _digits(llm.get("customer_vat"))
	total = flt(qr.get("total") or llm.get("total_amount"))
	vat = flt(qr.get("vat") or llm.get("vat_amount"))
	net = flt(llm.get("net_amount"))
	if not net and total:
		net = flt(total - vat)

	company_vat = _digits(frappe.db.get_value("Company", doc.company, "tax_id"))
	direction = _detect_direction(supplier_vat, customer_vat, company_vat)

	# 4) Write back
	doc.supplier_name = supplier_name
	doc.supplier_vat = supplier_vat
	doc.customer_vat = customer_vat
	doc.invoice_no = llm.get("invoice_no")
	doc.invoice_date = _to_date(llm.get("invoice_date"))
	doc.direction = direction
	doc.net_amount = net
	doc.vat_amount = vat
	doc.total_amount = total
	doc.raw_qr = raw_qr or ""
	doc.extraction_confidence = _confidence(qr, net, vat, total)

	rules = _load_routing_rules()
	doc.set("items", [])
	for it in (llm.get("line_items") or []):
		item_code = (it.get("item_code") or "")[:140]
		description = it.get("description") or ""
		doc.append("items", {
			"item_code": item_code,
			"description": description,
			"qty": flt(it.get("qty")),
			"rate": flt(it.get("rate")),
			"amount": flt(it.get("amount")),
			"account": _route_account(f"{item_code} {description}", rules),
		})

	doc.status = "Extracted"
	doc.save()
	frappe.db.commit()

	return {
		"supplier_name": supplier_name,
		"invoice_no": doc.invoice_no,
		"net": net, "vat": vat, "total": total,
		"direction": direction,
		"qr_found": bool(raw_qr),
		"confidence": doc.extraction_confidence,
	}


@frappe.whitelist()
def extract_from_file(file_url, company=None):
	"""Extract an invoice from an uploaded file (no saved document needed).

	Used by the "Extract from Invoice" dialog on Journal Entry. Returns the
	header amounts plus a list of suggested debit lines (one per routed
	expense account) so the user can add Journal Entry rows directly.
	"""
	if not file_url:
		frappe.throw(_("Attach an invoice file first."))

	settings = frappe.get_single("AI Settings")
	if not settings.enabled:
		frappe.throw(_("AI Settings is disabled. Enable it and set an API key first."))

	content, filename = _read_file(file_url)
	images = _file_to_images(content, filename)
	if not images:
		frappe.throw(_("Could not read any page/image from the attached file."))

	raw_qr, qr = _decode_qr(images)
	llm = _call_llm(images, settings)

	supplier_name = qr.get("seller_name") or llm.get("supplier_name")
	supplier_vat = _digits(qr.get("seller_vat")) or _digits(llm.get("supplier_vat"))
	customer_vat = _digits(llm.get("customer_vat"))
	total = flt(qr.get("total") or llm.get("total_amount"))
	vat = flt(qr.get("vat") or llm.get("vat_amount"))
	net = flt(llm.get("net_amount"))
	if not net and total:
		net = flt(total - vat)

	company_vat = _digits(frappe.db.get_value("Company", company, "tax_id")) if company else ""
	direction = _detect_direction(supplier_vat, customer_vat, company_vat)

	# Group routed line items into suggested debit rows.
	rules = _load_routing_rules()
	groups = {}
	for it in (llm.get("line_items") or []):
		text = f"{it.get('item_code') or ''} {it.get('description') or ''}"
		account = _route_account(text, rules)
		if account:
			groups[account] = flt(groups.get(account, 0)) + flt(it.get("amount"))

	suggestions = [
		{"account": account, "amount": amount, "dr_cr": "Debit"}
		for account, amount in groups.items()
	]
	# If nothing routed, suggest the whole net as one debit line (no account).
	if not suggestions and net:
		suggestions.append({"account": None, "amount": net, "dr_cr": "Debit"})

	return {
		"supplier_name": supplier_name,
		"supplier_vat": supplier_vat,
		"invoice_no": llm.get("invoice_no"),
		"net": net, "vat": vat, "total": total,
		"direction": direction,
		"qr_found": bool(raw_qr),
		"suggestions": suggestions,
	}


# ----------------------------------------------------------------------------- #
#  File handling
# ----------------------------------------------------------------------------- #


def _read_file(file_url):
	import os
	site_path = frappe.utils.get_site_path()
	filename = file_url.split("/")[-1]

	# Strategy 1 — standard Frappe resolution
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		content = file_doc.get_content()
		if content:
			return content, (file_doc.file_name or filename)
	except Exception:
		pass

	# Strategy 2 — fallback to private/files/ by filename
	private_path = os.path.join(site_path, "private", "files", filename)
	if os.path.isfile(private_path):
		with open(private_path, "rb") as fh:
			return fh.read(), filename

	# Strategy 3 — fallback to public/files/ by filename
	public_path = os.path.join(site_path, "public", "files", filename)
	if os.path.isfile(public_path):
		with open(public_path, "rb") as fh:
			return fh.read(), filename

	raise FileNotFoundError(
		f"File not found on disk for URL '{file_url}'. "
		f"Checked: {private_path} and {public_path}"
	)


def _file_to_images(content, filename):
	"""Return a list of PNG byte-strings, one per page (or the image itself)."""
	images = []
	if str(filename).lower().endswith(".pdf"):
		import fitz  # PyMuPDF
		pdf = fitz.open(stream=content, filetype="pdf")
		for page in pdf:
			pix = page.get_pixmap(dpi=200)
			images.append(pix.tobytes("png"))
		pdf.close()
	else:
		images.append(content)
	return images


# ----------------------------------------------------------------------------- #
#  ZATCA QR
# ----------------------------------------------------------------------------- #

_TLV_TAGS = {1: "seller_name", 2: "seller_vat", 3: "timestamp", 4: "total", 5: "vat"}


def _decode_qr(images):
	"""Return (raw_base64, parsed_dict). Empty dict if no ZATCA QR found."""
	try:
		from pyzbar.pyzbar import decode as zbar_decode
		from PIL import Image
	except Exception:
		return None, {}

	for img_bytes in images:
		try:
			im = Image.open(io.BytesIO(img_bytes))
		except Exception:
			continue
		for result in zbar_decode(im):
			data = result.data
			b64 = data.decode("utf-8", "ignore") if isinstance(data, bytes) else data
			parsed = _parse_zatca_tlv(b64)
			if parsed.get("seller_vat") or parsed.get("total"):
				return b64, parsed
	return None, {}


def _parse_zatca_tlv(b64str):
	"""Parse base64 TLV (ZATCA Phase-1 QR) into a dict."""
	out = {}
	try:
		raw = base64.b64decode(b64str)
	except Exception:
		return out
	i, n = 0, len(raw)
	while i + 2 <= n:
		tag = raw[i]
		length = raw[i + 1]
		val = raw[i + 2:i + 2 + length]
		if tag in _TLV_TAGS:
			try:
				out[_TLV_TAGS[tag]] = val.decode("utf-8")
			except Exception:
				out[_TLV_TAGS[tag]] = val.hex()
		i += 2 + length
	return out


# ----------------------------------------------------------------------------- #
#  LLM vision
# ----------------------------------------------------------------------------- #

_PROMPT = """You are reading a Saudi (KSA) tax invoice that mixes Arabic and English.
Return ONLY a JSON object (no markdown, no commentary) with exactly these keys:

{
  "supplier_name": string,        // the SELLER / vendor name
  "supplier_vat": string,         // SELLER VAT registration number (digits)
  "customer_vat": string,         // BUYER / customer VAT number (digits), or ""
  "invoice_no": string,
  "invoice_date": string,         // GREGORIAN date as YYYY-MM-DD (ignore Hijri)
  "net_amount": number,           // total BEFORE VAT
  "vat_amount": number,           // VAT (15%) amount
  "total_amount": number,         // grand total INCLUDING VAT
  "line_items": [
    {"item_code": string, "description": string, "qty": number, "rate": number, "amount": number}
  ]
}

Rules:
- Numbers must be plain numbers (no currency symbol, no thousands separator).
- If a value is missing, use "" for strings and 0 for numbers.
- supplier = the company issuing the invoice; customer = the one being billed."""


def _call_llm(images, settings):
	provider = settings.provider
	api_key = settings.get_api_key()
	model = settings.get_default_model()
	max_tokens = settings.max_tokens or 2000
	pages = images[:2]  # first two pages are enough for an invoice

	if provider == "Anthropic":
		import anthropic
		client = anthropic.Anthropic(api_key=api_key, base_url=settings.base_url or None)
		content = [{"type": "text", "text": _PROMPT}]
		for img in pages:
			content.append({
				"type": "image",
				"source": {"type": "base64", "media_type": "image/png",
				           "data": base64.b64encode(img).decode()},
			})
		msg = client.messages.create(
			model=model, max_tokens=max_tokens,
			messages=[{"role": "user", "content": content}],
		)
		text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

	elif provider == "OpenAI":
		from openai import OpenAI

		from smart_journal.smart_journal.doctype.ai_settings.ai_settings import openai_chat_create
		client = OpenAI(api_key=api_key, base_url=settings.base_url or None)
		content = [{"type": "text", "text": _PROMPT}]
		for img in pages:
			b64 = base64.b64encode(img).decode()
			content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
		resp = openai_chat_create(
			client,
			model=model, max_tokens=max_tokens,
			messages=[{"role": "user", "content": content}],
		)
		text = resp.choices[0].message.content
	else:
		frappe.throw(_("Unknown AI provider: {0}").format(provider))

	return _parse_json(text)


def _parse_json(text):
	if not text:
		return {}
	text = text.strip()
	text = re.sub(r"^```(?:json)?", "", text).strip()
	text = re.sub(r"```$", "", text).strip()
	try:
		return json.loads(text)
	except Exception:
		m = re.search(r"\{.*\}", text, re.DOTALL)
		if m:
			try:
				return json.loads(m.group(0))
			except Exception:
				pass
	frappe.log_error(text[:2000], "smart_journal: LLM JSON parse failed")
	return {}


# ----------------------------------------------------------------------------- #
#  Helpers
# ----------------------------------------------------------------------------- #


def _load_routing_rules():
	"""Return [(keyword_lower, account), ...] from AI Settings, in order."""
	settings = frappe.get_single("AI Settings")
	rules = []
	for row in (settings.get("expense_routing") or []):
		if row.keyword and row.account:
			rules.append((row.keyword.strip().lower(), row.account))
	return rules


def _route_account(text, rules):
	"""First keyword found in the line text wins. Returns None if no match."""
	text = (text or "").lower()
	for keyword, account in rules:
		if keyword and keyword in text:
			return account
	return None


def _digits(val):
	return re.sub(r"\D", "", str(val)) if val else ""


def _detect_direction(seller_vat, customer_vat, company_vat):
	"""You are the BUYER if your VAT is the customer; SELLER if it's the seller."""
	if company_vat:
		if customer_vat and _vat_match(customer_vat, company_vat):
			return "Buyer (Purchase)"
		if seller_vat and _vat_match(seller_vat, company_vat):
			return "Seller (Sale)"
	# Default: a supplier invoice you received -> purchase.
	return "Buyer (Purchase)"


def _vat_match(a, b):
	"""Tolerant VAT compare (handles OCR digit drift / leading zeros)."""
	a, b = _digits(a), _digits(b)
	if not a or not b:
		return False
	return a == b or a[-10:] == b[-10:] or a in b or b in a


def _to_date(val):
	if not val:
		return None
	m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", str(val))
	if m:
		return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
	return None


def _confidence(qr, net, vat, total):
	math_ok = total and abs(flt(total) - (flt(net) + flt(vat))) <= ROUNDING_TOLERANCE
	if qr.get("seller_vat") and math_ok:
		return "High (QR + totals matched)"
	if math_ok:
		return "Medium (AI only, totals matched)"
	return "Low (review amounts)"
