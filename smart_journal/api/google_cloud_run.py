import base64

import frappe
from frappe import _

from smart_journal.api.google_document_ai import EXPENSE_KIND, INVOICE_KIND
from smart_journal.smart_journal.doctype.ai_settings.ai_settings import GOOGLE_VERTEX_PROVIDER


def cloud_run_enabled(settings):
	return (
		settings.provider == GOOGLE_VERTEX_PROVIDER
		and bool(getattr(settings, "google_cloud_run_enabled", 0))
		and bool((getattr(settings, "google_cloud_run_url", "") or "").strip())
	)


def process_cloud_run(content, filename, settings, kind, expense_types=None):
	"""Send a document to the Google Cloud Run processor and return final JSON."""
	if kind not in {INVOICE_KIND, EXPENSE_KIND}:
		frappe.throw(_("Unknown Cloud Run processing kind: {0}").format(kind))

	url = _process_url(settings)
	payload = {
		"kind": kind,
		"file_name": filename or "document",
		"mime_type": _mime_type(filename, content),
		"content_base64": base64.b64encode(content).decode(),
	}
	if kind == EXPENSE_KIND and expense_types:
		payload["expense_types"] = expense_types

	response = _requests().post(
		url,
		json=payload,
		headers=_headers(settings),
		timeout=int(getattr(settings, "timeout", 120) or 120),
	)
	return _read_response(response)


def test_cloud_run(settings):
	url = _base_url(settings)
	response = _requests().get(
		f"{url}/health",
		headers=_headers(settings),
		timeout=min(int(getattr(settings, "timeout", 120) or 120), 30),
	)
	data = _read_response(response)
	model = data.get("model") or settings.get_default_model()
	return f"Cloud Run processor reachable with model {model}."


@frappe.whitelist()
def test_process_file(file_url: str, kind: str = INVOICE_KIND):
	settings = frappe.get_single("AI Settings")
	if not cloud_run_enabled(settings):
		frappe.throw(_("Cloud Run processor is not enabled in AI Settings."))

	from smart_journal.api.extraction import _read_file

	content, filename = _read_file(file_url)
	expense_types = None
	if kind == EXPENSE_KIND:
		from smart_journal.api.pr_automation import _expense_type_list

		expense_types = _expense_type_list()
	return process_cloud_run(content, filename, settings, kind, expense_types=expense_types)


def _process_url(settings):
	return f"{_base_url(settings)}/process"


def _base_url(settings):
	url = (getattr(settings, "google_cloud_run_url", "") or "").strip().rstrip("/")
	if not url:
		frappe.throw(_("Cloud Run URL is required when Cloud Run Processor is enabled."))
	return url


def _headers(settings):
	headers = {"Content-Type": "application/json"}
	secret = _get_secret(settings)
	if secret:
		headers["X-Smart-Journal-Secret"] = secret
	return headers


def _get_secret(settings):
	try:
		secret = settings.get_password("google_cloud_run_secret", raise_exception=False)
	except Exception:
		secret = None
	return secret or (getattr(settings, "google_cloud_run_secret", "") or "").strip()


def _read_response(response):
	try:
		payload = response.json()
	except Exception:
		payload = {}
	if response.status_code >= 400:
		error = payload.get("error") or response.text[:500]
		frappe.throw(_("Cloud Run processor failed: {0}").format(error))
	if payload.get("ok") is False:
		frappe.throw(_("Cloud Run processor failed: {0}").format(payload.get("error") or payload))
	return payload.get("data") or payload


def _requests():
	try:
		import requests
	except ImportError:
		frappe.throw(_("Python package 'requests' is not installed."))
	return requests


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
