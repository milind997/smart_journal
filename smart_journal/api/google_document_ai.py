# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt
"""Google Document AI integration for Smart Journal.

Document AI does the first-pass OCR/entity extraction. The rest of the app keeps
using the existing normalized invoice/receipt dictionaries.
"""

import re

import frappe
from frappe.utils import flt


INVOICE_KIND = "invoice"
EXPENSE_KIND = "expense"


def document_ai_enabled(settings):
	return bool(getattr(settings, "document_ai_enabled", 0)) and settings.provider == "Google Vertex AI"


def get_document_ai_processor(settings, kind):
	"""Return the full processor resource name for an invoice/expense processor."""
	project = (settings.google_project_id or "").strip()
	location = (settings.document_ai_location or "us").strip()
	if not project:
		frappe.throw("Google Cloud Project ID is required for Document AI.")
	if not location:
		frappe.throw("Document AI Location is required.")

	if kind == INVOICE_KIND:
		processor_id = (settings.document_ai_invoice_processor_id or "").strip()
		label = "Invoice Processor ID"
	elif kind == EXPENSE_KIND:
		processor_id = (settings.document_ai_expense_processor_id or "").strip()
		label = "Expense Processor ID"
	else:
		frappe.throw(f"Unknown Document AI processor kind: {kind}")

	if not processor_id:
		frappe.throw(f"{label} is required when Document AI is enabled.")

	try:
		from google.cloud import documentai_v1 as documentai
	except ImportError:
		frappe.throw("Python package 'google-cloud-documentai' is not installed. Run: bench pip install google-cloud-documentai")

	client = documentai.DocumentProcessorServiceClient(
		client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
	)
	return client.processor_path(project, location, processor_id)


def process_document_ai(content, filename, settings, kind):
	"""Process original file bytes with Document AI and return normalized data."""
	try:
		from google.cloud import documentai_v1 as documentai
	except ImportError:
		frappe.throw("Python package 'google-cloud-documentai' is not installed. Run: bench pip install google-cloud-documentai")

	from smart_journal.smart_journal.doctype.ai_settings.ai_settings import configure_google_credentials

	configure_google_credentials(settings)
	location = (settings.document_ai_location or "us").strip()
	name = get_document_ai_processor(settings, kind)
	client = documentai.DocumentProcessorServiceClient(
		client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
	)
	request = documentai.ProcessRequest(
		name=name,
		raw_document=documentai.RawDocument(
			content=content,
			mime_type=_mime_type(filename),
		),
	)
	result = client.process_document(request=request)
	document = result.document
	if kind == INVOICE_KIND:
		return normalize_invoice(document)
	return normalize_expense(document)


def describe_processor(settings, kind):
	"""Fetch a processor from Google to verify credentials, location, and ID."""
	try:
		from google.cloud import documentai_v1 as documentai
	except ImportError:
		frappe.throw("Python package 'google-cloud-documentai' is not installed. Run: bench pip install google-cloud-documentai")

	from smart_journal.smart_journal.doctype.ai_settings.ai_settings import configure_google_credentials

	configure_google_credentials(settings)
	location = (settings.document_ai_location or "us").strip()
	client = documentai.DocumentProcessorServiceClient(
		client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
	)
	return client.get_processor(name=get_document_ai_processor(settings, kind))


def normalize_invoice(document):
	entities = _entity_groups(document)
	line_items = _line_items(entities)
	data = {
		"supplier_name": _text(entities, ["supplier_name", "supplier", "vendor_name", "remittance_supplier_name"]),
		"supplier_vat": _digits(_text(entities, ["supplier_tax_id", "supplier_vat", "vendor_tax_id", "tax_id"])),
		"customer_vat": _digits(_text(entities, ["receiver_tax_id", "customer_tax_id", "buyer_tax_id"])),
		"invoice_no": _text(entities, ["invoice_id", "invoice_number", "invoice_no"]),
		"invoice_date": _date_text(entities, ["invoice_date"]),
		"net_amount": _amount(entities, ["net_amount", "subtotal_amount", "subtotal"]),
		"vat_amount": _amount(entities, ["total_tax_amount", "tax_amount", "vat_amount"]),
		"total_amount": _amount(entities, ["total_amount", "amount_due", "total"]),
		"line_items": line_items,
		"document_ai_confidence": _avg_confidence(document),
		"document_ai_text": (getattr(document, "text", "") or "")[:4000],
	}
	if not data["net_amount"] and data["total_amount"]:
		data["net_amount"] = flt(data["total_amount"] - data["vat_amount"])
	if not data["line_items"] and data["net_amount"]:
		data["line_items"] = [{
			"item_code": "",
			"description": data["supplier_name"] or "Invoice",
			"qty": 1,
			"rate": data["net_amount"],
			"amount": data["net_amount"],
		}]
	return data


def normalize_expense(document):
	entities = _entity_groups(document)
	total = _amount(entities, ["total_amount", "amount", "purchase_amount", "amount_due"])
	vat = _amount(entities, ["tax_amount", "total_tax_amount", "vat_amount"])
	net = _amount(entities, ["net_amount", "subtotal_amount", "subtotal"])
	if not net and total:
		net = flt(total - vat)

	line_items = _expense_line_items(entities)
	if not line_items and (net or vat or total):
		line_items = [{
			"expense_type": "Other",
			"description": _text(entities, ["description", "merchant_name", "supplier_name", "vendor_name"]) or "Receipt",
			"vendor": _text(entities, ["merchant_name", "supplier_name", "vendor_name"]),
			"amount_before_vat": net,
			"vat_amount": vat,
			"total_amount": total or (net + vat),
		}]

	return {
		"document_type": "Receipt",
		"legible": _avg_confidence(document) >= 0.35,
		"vendor": _text(entities, ["merchant_name", "supplier_name", "vendor_name", "supplier"]),
		"vendor_vat": _digits(_text(entities, ["supplier_tax_id", "vendor_tax_id", "tax_id"])),
		"invoice_number": _text(entities, ["receipt_id", "invoice_id", "invoice_number", "reference_number"]),
		"invoice_date": _date_text(entities, ["receipt_date", "invoice_date", "date"]),
		"reference_number": _text(entities, ["reference_number", "transaction_id"]),
		"bank_charge": _amount(entities, ["bank_charge", "fee_amount", "service_charge"]),
		"bank_charge_vat": _amount(entities, ["bank_charge_tax", "fee_tax_amount"]),
		"line_items": line_items,
		"document_ai_confidence": _avg_confidence(document),
		"document_ai_text": (getattr(document, "text", "") or "")[:4000],
	}


def _entity_groups(document):
	groups = {}
	for entity in getattr(document, "entities", []) or []:
		key = (getattr(entity, "type_", "") or "").lower()
		if key:
			groups.setdefault(key, []).append(entity)
	return groups


def _line_items(entities):
	items = []
	for entity in entities.get("line_item", []):
		props = _properties(entity)
		description = _text(props, ["description", "line_item/description", "item_description", "product_name"])
		amount = _amount(props, ["amount", "line_item/amount", "total_amount"])
		qty = flt(_text(props, ["quantity", "line_item/quantity"]) or 0)
		rate = _amount(props, ["unit_price", "line_item/unit_price"])
		if not amount and rate and qty:
			amount = flt(rate * qty)
		if not description and not amount:
			continue
		items.append({
			"item_code": _text(props, ["product_code", "item_code", "line_item/product_code"])[:140],
			"description": description or "Invoice line",
			"qty": qty or 1,
			"rate": rate or amount,
			"amount": amount,
		})
	return items


def _expense_line_items(entities):
	items = []
	for entity in entities.get("line_item", []):
		props = _properties(entity)
		total = _amount(props, ["amount", "line_item/amount", "total_amount"])
		vat = _amount(props, ["tax_amount", "vat_amount"])
		net = _amount(props, ["net_amount", "subtotal_amount"])
		if not net and total:
			net = flt(total - vat)
		description = _text(props, ["description", "line_item/description", "item_description", "product_name"])
		if not description and not total and not net:
			continue
		items.append({
			"expense_type": "Other",
			"description": description or "Receipt line",
			"vendor": "",
			"amount_before_vat": net,
			"vat_amount": vat,
			"total_amount": total or (net + vat),
		})
	return items


def _properties(entity):
	groups = {}
	for prop in getattr(entity, "properties", []) or []:
		key = (getattr(prop, "type_", "") or "").lower()
		if key:
			groups.setdefault(key, []).append(prop)
	return groups


def _text(groups, aliases):
	entity = _best_entity(groups, aliases)
	return _entity_text(entity) if entity else ""


def _date_text(groups, aliases):
	entity = _best_entity(groups, aliases)
	if not entity:
		return ""
	value = getattr(entity, "normalized_value", None)
	date_value = getattr(value, "date_value", None) if value else None
	if date_value and getattr(date_value, "year", 0):
		return f"{date_value.year:04d}-{date_value.month:02d}-{date_value.day:02d}"
	return _entity_text(entity)


def _amount(groups, aliases):
	entity = _best_entity(groups, aliases)
	if not entity:
		return 0.0
	value = getattr(entity, "normalized_value", None)
	money = getattr(value, "money_value", None) if value else None
	if money:
		return flt(getattr(money, "units", 0)) + flt(getattr(money, "nanos", 0)) / 1_000_000_000
	float_value = getattr(value, "float_value", None) if value else None
	if float_value:
		return flt(float_value)
	integer_value = getattr(value, "integer_value", None) if value else None
	if integer_value:
		return flt(integer_value)
	return _parse_amount(_entity_text(entity))


def _best_entity(groups, aliases):
	best = None
	for alias in aliases:
		for entity in groups.get(alias.lower(), []) or []:
			if best is None or flt(getattr(entity, "confidence", 0)) > flt(getattr(best, "confidence", 0)):
				best = entity
	return best


def _entity_text(entity):
	if not entity:
		return ""
	value = getattr(entity, "normalized_value", None)
	text = getattr(value, "text", None) if value else None
	return (text or getattr(entity, "mention_text", "") or "").strip()


def _parse_amount(text):
	if not text:
		return 0.0
	match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", str(text))
	return flt(match.group(0).replace(",", "")) if match else 0.0


def _digits(val):
	return re.sub(r"\D", "", str(val)) if val else ""


def _avg_confidence(document):
	values = [flt(getattr(entity, "confidence", 0)) for entity in getattr(document, "entities", []) or []]
	values = [v for v in values if v > 0]
	return flt(sum(values) / len(values)) if values else 0.0


def _mime_type(filename):
	ext = (filename or "").rsplit(".", 1)[-1].lower()
	return {
		"pdf": "application/pdf",
		"jpg": "image/jpeg",
		"jpeg": "image/jpeg",
		"png": "image/png",
		"gif": "image/gif",
		"webp": "image/webp",
		"tif": "image/tiff",
		"tiff": "image/tiff",
	}.get(ext, "application/pdf")
