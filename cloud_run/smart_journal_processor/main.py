import base64
import json
import os
import re
from copy import deepcopy

from flask import Flask, jsonify, request
from google import genai
from google.cloud import documentai_v1 as documentai
from google.genai import types

app = Flask(__name__)

INVOICE_KIND = "invoice"
EXPENSE_KIND = "expense"

_INVOICE_PROMPT = """You are reading a Saudi (KSA) tax invoice that mixes Arabic and English.
Return ONLY a JSON object (no markdown, no commentary) with exactly these keys:

{
  "supplier_name": string,
  "supplier_vat": string,
  "customer_vat": string,
  "invoice_no": string,
  "invoice_date": string,
  "net_amount": number,
  "vat_amount": number,
  "total_amount": number,
  "line_items": [
    {"item_code": string, "description": string, "qty": number, "rate": number, "amount": number}
  ]
}

Rules:
- Numbers must be plain numbers with no currency symbols or commas.
- If a value is missing, use "" for strings and 0 for numbers.
- supplier = the company issuing the invoice; customer = the one being billed.
- invoice_date must be Gregorian YYYY-MM-DD.
"""

_RECEIPT_PROMPT = """You are reading an expense receipt, invoice, or bank bill-payment confirmation
for a Saudi company (amounts in SAR). The document may be in Arabic, English, or both.

Return ONLY a JSON object, no markdown:
{
  "document_type": "Invoice" | "Receipt" | "Bank Slip" | "Other",
  "legible": true,
  "vendor": "<overall payer/seller name or empty string>",
  "vendor_vat": "<seller VAT registration number, digits only, or empty>",
  "invoice_number": "<string or empty>",
  "invoice_date": "<YYYY-MM-DD or empty>",
  "reference_number": "<bank / SADAD reference number if shown, else empty>",
  "bank_charge": <number>,
  "bank_charge_vat": <number>,
  "line_items": [
    {
      "expense_type": "<one of the categories below>",
      "description": "<short English description>",
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
- Create one line_items entry per distinct bill / expense / row in the document.
- Bank commission/fee and its VAT go in bank_charge / bank_charge_vat, never inside line_items.
- Numbers are plain numbers with no currency symbol or commas.
- If a value is missing use "" for strings and 0 for numbers.
"""

_DEFAULT_EXPENSE_TYPES = (
	"Fuel | Food/Meal | Hospitality | Electricity | Water | Phone/Internet | "
	"Car Maintenance | Government Fee | Social Insurance | Salary | Overtime | "
	"Airline Ticket | Hotel | Shipping | Customs | Office Supplies | Other"
)


class ProcessingError(Exception):
	pass


@app.get("/health")
def health():
	auth_error = _check_secret()
	if auth_error:
		return auth_error

	return jsonify({
		"ok": True,
		"service": "smart-journal-processor",
		"project": _env("GOOGLE_PROJECT_ID", required=False),
		"vertex_location": _env("GOOGLE_LOCATION", "us-central1", required=False),
		"document_ai_location": _env("DOCUMENT_AI_LOCATION", "us", required=False),
		"model": _env("GEMINI_MODEL", "gemini-2.5-flash", required=False),
		"invoice_processor_configured": bool(_env("DOCUMENT_AI_INVOICE_PROCESSOR_ID", required=False)),
		"expense_processor_configured": bool(_env("DOCUMENT_AI_EXPENSE_PROCESSOR_ID", required=False)),
	})


@app.post("/process")
def process():
	auth_error = _check_secret()
	if auth_error:
		return auth_error

	try:
		payload = request.get_json(silent=True) or {}
		kind = (payload.get("kind") or "").strip().lower()
		if kind not in {INVOICE_KIND, EXPENSE_KIND}:
			raise ProcessingError("kind must be 'invoice' or 'expense'.")

		filename = (payload.get("file_name") or "document").strip()
		content_b64 = payload.get("content_base64") or ""
		if not content_b64:
			raise ProcessingError("content_base64 is required.")

		content = base64.b64decode(content_b64, validate=True)
		mime_type = payload.get("mime_type") or _mime_type(filename, content)
		if not content:
			raise ProcessingError("document content is empty.")

		doc_ai = _process_document_ai(content, filename, mime_type, kind)
		if kind == INVOICE_KIND:
			enriched = _enrich_invoice(doc_ai)
			data = _merge_invoice(doc_ai, enriched)
			if not _has_invoice_signal(data) or not _passes_confidence(data):
				data = _merge_invoice(data, _direct_invoice(content, mime_type))
		else:
			expense_types = payload.get("expense_types") or _DEFAULT_EXPENSE_TYPES
			enriched = _enrich_receipt(doc_ai, expense_types)
			data = _merge_receipt(doc_ai, enriched)
			if not _has_receipt_signal(data) or not _passes_confidence(data):
				data = _merge_receipt(data, _direct_receipt(content, mime_type, expense_types))

		data["_processing_backend"] = "google_cloud_run"
		return jsonify({"ok": True, "data": data})
	except Exception as exc:
		status = 400 if isinstance(exc, ProcessingError) else 500
		return jsonify({"ok": False, "error": str(exc)}), status


def _check_secret():
	expected = _env("SMART_JOURNAL_PROCESSOR_SECRET", required=False)
	if not expected:
		return None
	provided = request.headers.get("X-Smart-Journal-Secret") or ""
	if provided != expected:
		return jsonify({"ok": False, "error": "unauthorized"}), 401
	return None


def _env(name, default=None, required=True):
	value = os.environ.get(name)
	if value in (None, ""):
		if required:
			raise ProcessingError(f"Missing required environment variable: {name}")
		return default
	return value


def _process_document_ai(content, filename, mime_type, kind):
	project = _env("GOOGLE_PROJECT_ID")
	location = _env("DOCUMENT_AI_LOCATION", "us")
	processor_id = _env(
		"DOCUMENT_AI_INVOICE_PROCESSOR_ID" if kind == INVOICE_KIND else "DOCUMENT_AI_EXPENSE_PROCESSOR_ID"
	)
	client = documentai.DocumentProcessorServiceClient(
		client_options={"api_endpoint": f"{location}-documentai.googleapis.com"}
	)
	name = client.processor_path(project, location, processor_id)
	result = client.process_document(
		request={
			"name": name,
			"raw_document": documentai.RawDocument(content=content, mime_type=mime_type),
		}
	)
	if kind == INVOICE_KIND:
		return _normalize_invoice(result.document)
	return _normalize_expense(result.document)


def _vertex_text(prompt, content=None, mime_type=None, max_tokens=None):
	project = _env("GOOGLE_PROJECT_ID")
	location = _env("GOOGLE_LOCATION", "us-central1")
	model = _env("GEMINI_MODEL", "gemini-2.5-flash")
	client = genai.Client(
		vertexai=True,
		project=project,
		location=location,
		http_options=types.HttpOptions(api_version="v1"),
	)
	parts = [prompt]
	if content:
		parts.append(types.Part.from_bytes(data=content, mime_type=mime_type))
	config_kwargs = {
		"automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
	}
	if max_tokens:
		config_kwargs["max_output_tokens"] = int(max_tokens)
	response = client.models.generate_content(
		model=model,
		contents=parts,
		config=types.GenerateContentConfig(**config_kwargs),
	)
	return getattr(response, "text", "") or ""


def _enrich_invoice(doc_ai):
	source = deepcopy(doc_ai or {})
	source_text = source.pop("document_ai_text", "")
	prompt = f"""{_INVOICE_PROMPT}

Use this Document AI extraction and OCR text as the source. Correct OCR issues,
keep amounts consistent, and return ONLY the JSON object.

Document AI JSON:
{json.dumps(source, ensure_ascii=False)}

OCR text:
{source_text[:4000]}
"""
	return _parse_json(_vertex_text(prompt, max_tokens=_max_tokens(2000)))


def _direct_invoice(content, mime_type):
	prompt = _INVOICE_PROMPT + "\nRead the attached document and return ONLY the JSON object."
	return _parse_json(_vertex_text(prompt, content=content, mime_type=mime_type, max_tokens=_max_tokens(2000)))


def _enrich_receipt(doc_ai, expense_types):
	source = deepcopy(doc_ai or {})
	source_text = source.pop("document_ai_text", "")
	prompt = _RECEIPT_PROMPT.replace("__EXPENSE_TYPES__", expense_types)
	prompt += f"""

Use this Document AI extraction and OCR text as the source. Correct OCR issues,
classify each line into one of the allowed expense_type values, and return ONLY
the JSON object.

Document AI JSON:
{json.dumps(source, ensure_ascii=False)}

OCR text:
{source_text[:4000]}
"""
	return _parse_json(_vertex_text(prompt, max_tokens=_max_tokens(1200)))


def _direct_receipt(content, mime_type, expense_types):
	prompt = _RECEIPT_PROMPT.replace("__EXPENSE_TYPES__", expense_types)
	prompt += "\nRead the attached document and return ONLY the JSON object."
	return _parse_json(_vertex_text(prompt, content=content, mime_type=mime_type, max_tokens=_max_tokens(1200)))


def _normalize_invoice(document):
	groups = _entity_groups(document)
	net = _amount(groups, ["net_amount", "subtotal", "total_before_tax", "amount_due_before_tax"])
	vat = _amount(groups, ["total_tax_amount", "vat_amount", "tax_amount"])
	total = _amount(groups, ["total_amount", "amount_due", "invoice_total", "total"])
	if not net and total:
		net = round(total - vat, 2)
	line_items = _line_items(groups)
	if not line_items and net:
		line_items = [{"item_code": "", "description": "", "qty": 1, "rate": net, "amount": net}]
	return {
		"supplier_name": _text(groups, ["supplier_name", "vendor_name", "seller_name"]),
		"supplier_vat": _digits(_text(groups, ["supplier_tax_id", "supplier_vat", "vendor_tax_id"])),
		"customer_vat": _digits(_text(groups, ["receiver_tax_id", "customer_tax_id", "customer_vat"])),
		"invoice_no": _text(groups, ["invoice_id", "invoice_number", "invoice_no"]),
		"invoice_date": _date_text(groups, ["invoice_date"]),
		"net_amount": net,
		"vat_amount": vat,
		"total_amount": total,
		"line_items": line_items,
		"document_ai_confidence": _avg_confidence(document),
		"document_ai_text": document.text or "",
	}


def _normalize_expense(document):
	groups = _entity_groups(document)
	bank_charge = _amount(groups, ["bank_charge", "service_charge", "fee", "commission"])
	bank_charge_vat = _amount(groups, ["bank_charge_vat", "fee_tax", "commission_tax"])
	return {
		"document_type": "Receipt",
		"legible": True,
		"vendor": _text(groups, ["supplier_name", "merchant_name", "vendor_name", "seller_name"]),
		"vendor_vat": _digits(_text(groups, ["supplier_tax_id", "merchant_tax_id", "vendor_tax_id"])),
		"invoice_number": _text(groups, ["receipt_id", "invoice_id", "invoice_number", "transaction_id"]),
		"invoice_date": _date_text(groups, ["receipt_date", "invoice_date", "purchase_date"]),
		"reference_number": _text(groups, ["transaction_id", "reference_number", "payment_reference"]),
		"bank_charge": bank_charge,
		"bank_charge_vat": bank_charge_vat,
		"line_items": _expense_line_items(groups),
		"document_ai_confidence": _avg_confidence(document),
		"document_ai_text": document.text or "",
	}


def _entity_groups(document):
	groups = {}
	for entity in document.entities or []:
		groups.setdefault(entity.type_, []).append(entity)
	return groups


def _line_items(groups):
	items = []
	for entity in groups.get("line_item", []):
		props = _properties(entity)
		description = _text(props, ["line_item/description", "description", "product_name", "item_name"])
		qty = _amount(props, ["line_item/quantity", "quantity"], default=1)
		rate = _amount(props, ["line_item/unit_price", "unit_price", "price"])
		amount = _amount(props, ["line_item/amount", "amount", "line_item/total_amount", "total_amount"])
		if not amount and rate and qty:
			amount = rate * qty
		if description or amount:
			items.append({
				"item_code": "",
				"description": description,
				"qty": qty or 1,
				"rate": rate or amount,
				"amount": amount,
			})
	return items


def _expense_line_items(groups):
	items = []
	for entity in groups.get("line_item", []):
		props = _properties(entity)
		description = _text(props, ["line_item/description", "description", "product_name", "item_name"])
		total = _amount(props, ["line_item/amount", "amount", "line_item/total_amount", "total_amount"])
		vat = _amount(props, ["line_item/tax", "tax", "vat_amount"])
		net = _amount(props, ["line_item/net_amount", "net_amount", "subtotal"])
		if not net and total:
			net = round(total - vat, 2)
		if description or net or vat or total:
			items.append({
				"expense_type": "Other",
				"description": description,
				"vendor": "",
				"amount_before_vat": net,
				"vat_amount": vat,
				"total_amount": total or round(net + vat, 2),
			})
	return items


def _properties(entity):
	props = {}
	for prop in entity.properties or []:
		props.setdefault(prop.type_, []).append(prop)
	return props


def _text(groups, aliases):
	entity = _best_entity(groups, aliases)
	if not entity:
		return ""
	return _entity_text(entity)


def _date_text(groups, aliases):
	entity = _best_entity(groups, aliases)
	if not entity:
		return ""
	normalized = getattr(entity, "normalized_value", None)
	if normalized and getattr(normalized, "date_value", None):
		date = normalized.date_value
		return f"{date.year:04d}-{date.month:02d}-{date.day:02d}"
	return _entity_text(entity)


def _amount(groups, aliases, default=0):
	entity = _best_entity(groups, aliases)
	if not entity:
		return default
	normalized = getattr(entity, "normalized_value", None)
	if normalized and getattr(normalized, "money_value", None):
		money = normalized.money_value
		return float(money.units or 0) + float(money.nanos or 0) / 1_000_000_000
	return _parse_amount(_entity_text(entity), default=default)


def _best_entity(groups, aliases):
	for alias in aliases:
		candidates = groups.get(alias) or []
		if candidates:
			return max(candidates, key=lambda item: item.confidence or 0)
	return None


def _entity_text(entity):
	return (entity.mention_text or "").strip()


def _parse_amount(text, default=0):
	if text in (None, ""):
		return default
	cleaned = re.sub(r"[^\d.,-]", "", str(text))
	if "," in cleaned and "." in cleaned:
		cleaned = cleaned.replace(",", "")
	elif "," in cleaned:
		cleaned = cleaned.replace(",", ".")
	try:
		return float(cleaned)
	except Exception:
		return default


def _merge_invoice(base, enriched):
	out = dict(enriched or {})
	base = base or {}
	for key in [
		"supplier_name", "supplier_vat", "customer_vat", "invoice_no", "invoice_date",
		"net_amount", "vat_amount", "total_amount", "line_items", "document_ai_confidence",
	]:
		if not _value_present(out.get(key)) and _value_present(base.get(key)):
			out[key] = base.get(key)
	return out


def _merge_receipt(base, enriched):
	out = dict(enriched or {})
	base = base or {}
	for key in [
		"document_type", "legible", "vendor", "vendor_vat", "invoice_number",
		"invoice_date", "reference_number", "bank_charge", "bank_charge_vat",
		"line_items", "document_ai_confidence",
	]:
		if not _value_present(out.get(key)) and _value_present(base.get(key)):
			out[key] = base.get(key)
	return out


def _has_invoice_signal(data):
	if not data:
		return False
	return bool(
		_float(data.get("total_amount"))
		or _float(data.get("net_amount"))
		or _float(data.get("vat_amount"))
		or data.get("invoice_no")
		or data.get("line_items")
	)


def _has_receipt_signal(data):
	if not data:
		return False
	for item in data.get("line_items") or []:
		if (
			_float(item.get("amount_before_vat"))
			or _float(item.get("vat_amount"))
			or _float(item.get("total_amount"))
		):
			return True
	return bool(_float(data.get("bank_charge")) or _float(data.get("bank_charge_vat")))


def _passes_confidence(data):
	threshold = _float(_env("DOCUMENT_AI_CONFIDENCE_THRESHOLD", "0", required=False))
	if not threshold:
		return True
	confidence = data.get("document_ai_confidence")
	if confidence in (None, ""):
		return True
	return _float(confidence) >= threshold


def _value_present(value):
	if isinstance(value, list):
		return bool(value)
	if isinstance(value, (int, float)):
		return bool(_float(value))
	return bool(value)


def _parse_json(text):
	if not text:
		return {}
	text = text.strip()
	text = re.sub(r"^```(?:json)?", "", text).strip()
	text = re.sub(r"```$", "", text).strip()
	try:
		return json.loads(text)
	except Exception:
		match = re.search(r"\{.*\}", text, re.DOTALL)
		if match:
			return json.loads(match.group(0))
	return {}


def _avg_confidence(document):
	values = [entity.confidence for entity in document.entities or [] if entity.confidence is not None]
	return sum(values) / len(values) if values else 0


def _mime_type(filename, content):
	lower = (filename or "").lower()
	if lower.endswith(".pdf"):
		return "application/pdf"
	if content[:3] == b"\xff\xd8\xff" or lower.endswith((".jpg", ".jpeg")):
		return "image/jpeg"
	if content[:8] == b"\x89PNG\r\n\x1a\n" or lower.endswith(".png"):
		return "image/png"
	if lower.endswith(".webp"):
		return "image/webp"
	if lower.endswith(".gif"):
		return "image/gif"
	return "application/octet-stream"


def _digits(value):
	return re.sub(r"\D+", "", str(value or ""))


def _float(value):
	try:
		return float(value or 0)
	except Exception:
		return 0.0


def _max_tokens(default):
	return int(_env("MAX_OUTPUT_TOKENS", str(default), required=False) or default)
