# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt

import os
import shlex

import frappe
from frappe.model.document import Document

GOOGLE_VERTEX_PROVIDER = "Google Vertex AI"

DEFAULT_MODELS = {
	"Anthropic": "claude-sonnet-4-6",
	"OpenAI": "gpt-4o",
	GOOGLE_VERTEX_PROVIDER: "gemini-2.5-flash",
}


class AISettings(Document):
	def get_api_key(self):
		"""Return the decrypted API key, or raise if not configured."""
		key = self.get_password("api_key", raise_exception=False)
		if not key:
			frappe.throw("API Key is not set in AI Settings.")
		return key

	def get_default_model(self):
		"""Sensible vision model per provider if none is set on the doc."""
		if self.model:
			return self.model
		return DEFAULT_MODELS.get(self.provider, "gpt-4o")


def openai_chat_create(client, **kwargs):
	"""Call OpenAI chat completions, tolerating the max_tokens rename.

	Newer models (gpt-5.x, o1/o3, …) dropped ``max_tokens`` in favour of
	``max_completion_tokens``. Older models / proxy gateways only know the old
	name. Try the new parameter first and fall back on the rename error.
	"""
	max_tokens = kwargs.pop("max_tokens", None)
	if max_tokens is None:
		return client.chat.completions.create(**kwargs)
	try:
		return client.chat.completions.create(max_completion_tokens=max_tokens, **kwargs)
	except Exception as e:
		if "max_completion_tokens" in str(e) or "max_tokens" in str(e):
			return client.chat.completions.create(max_tokens=max_tokens, **kwargs)
		raise


def get_google_vertex_client(settings):
	"""Create a Google Gen AI client configured for Vertex AI."""
	try:
		from google import genai
		from google.genai import types
	except ImportError:
		frappe.throw("Python package 'google-genai' is not installed. Run: bench pip install google-genai")

	project = (settings.google_project_id or "").strip()
	location = (settings.google_location or "us-central1").strip()
	if not project:
		frappe.throw("Google Cloud Project ID is required for Google Vertex AI.")
	if not location:
		frappe.throw("Vertex AI Location is required for Google Vertex AI.")

	configure_google_credentials(settings)

	http_options = types.HttpOptions(api_version="v1")
	client_kwargs = {
		"project": project,
		"location": location,
		"http_options": http_options,
	}

	try:
		return genai.Client(vertexai=True, **client_kwargs)
	except TypeError as vertex_error:
		try:
			return genai.Client(enterprise=True, **client_kwargs)
		except TypeError:
			raise vertex_error


def configure_google_credentials(settings):
	"""Apply an optional credentials file path for Google client libraries."""
	credentials_file = (settings.google_credentials_file or "").strip()
	if not credentials_file:
		return

	credentials_file = os.path.expanduser(credentials_file)
	if not os.path.exists(credentials_file):
		frappe.throw(f"Google credentials file was not found: {credentials_file}")
	if _is_google_oauth_client_secret(credentials_file):
		frappe.throw(
			"Credentials File Path cannot be an OAuth client secret file. "
			"Move it to OAuth Client Secret File, run the ADC setup command, then leave Credentials File Path blank."
		)
	os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file


def google_vertex_generate_content(settings, model, contents, max_tokens=None):
	"""Generate content through Gemini on Vertex AI and return response text."""
	from google.genai import types

	client = get_google_vertex_client(settings)
	config_kwargs = {
		"automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
	}
	if max_tokens:
		config_kwargs["max_output_tokens"] = int(max_tokens)
	config = types.GenerateContentConfig(**config_kwargs)

	kwargs = {
		"model": model,
		"contents": contents,
		"config": config,
	}

	response = client.models.generate_content(**kwargs)
	return getattr(response, "text", "") or ""


def _is_google_oauth_client_secret(path):
	try:
		import json

		with open(path) as f:
			data = json.load(f)
	except Exception:
		return False
	return bool(data.get("web") or data.get("installed"))


def _get_google_adc_setup_command(settings):
	oauth_file = (settings.google_oauth_client_secret_file or "").strip()
	if not oauth_file:
		return ""

	oauth_file = os.path.expanduser(oauth_file)
	if not os.path.exists(oauth_file):
		return ""
	if not _is_google_oauth_client_secret(oauth_file):
		return ""

	return (
		"gcloud auth application-default login "
		f"--client-id-file={shlex.quote(oauth_file)} "
		"--scopes=https://www.googleapis.com/auth/cloud-platform"
	)


@frappe.whitelist()
def test_connection():
	"""Make a tiny call to the configured provider to verify the connection works."""
	settings = frappe.get_single("AI Settings")
	if not settings.enabled:
		return {"ok": False, "message": "AI Settings is disabled. Tick 'Enabled' and save first."}

	model = settings.get_default_model()
	provider = settings.provider

	try:
		if provider == "Anthropic":
			api_key = settings.get_api_key()
			try:
				import anthropic
			except ImportError:
				return {
					"ok": False,
					"message": "Python package 'anthropic' is not installed. Run: bench pip install anthropic",
				}
			client = anthropic.Anthropic(
				api_key=api_key,
				base_url=settings.base_url or None,
			)
			client.messages.create(
				model=model,
				max_tokens=8,
				messages=[{"role": "user", "content": "ping"}],
			)
			return {"ok": True, "message": f"Anthropic reachable with model {model}."}

		elif provider == "OpenAI":
			api_key = settings.get_api_key()
			try:
				from openai import OpenAI
			except ImportError:
				return {
					"ok": False,
					"message": "Python package 'openai' is not installed. Run: bench pip install openai",
				}
			client = OpenAI(
				api_key=api_key,
				base_url=settings.base_url or None,
			)
			openai_chat_create(
				client,
				model=model,
				max_tokens=8,
				messages=[{"role": "user", "content": "ping"}],
			)
			return {"ok": True, "message": f"OpenAI reachable with model {model}."}

		elif provider == GOOGLE_VERTEX_PROVIDER:
			try:
				cloud_run_status = _test_cloud_run(settings)
				if cloud_run_status:
					return {
						"ok": True,
						"message": cloud_run_status,
					}
				google_vertex_generate_content(settings, model, "ping", max_tokens=8)
				document_ai_status = _test_document_ai(settings)
			except Exception as e:
				command = _get_google_adc_setup_command(settings)
				if command:
					return {
						"ok": False,
						"message": "Google ADC is not ready. Run the ADC setup command, then test again.",
						"command": command,
						"details": str(e),
					}
				raise
			return {
				"ok": True,
				"message": (
					f"Google Vertex AI reachable with model {model} in {settings.google_location or 'us-central1'}. "
					f"{document_ai_status}"
				),
			}

		return {"ok": False, "message": f"Unknown provider: {provider}"}

	except Exception as e:
		return {"ok": False, "message": f"Connection failed: {e}"}


def _test_document_ai(settings):
	if not getattr(settings, "document_ai_enabled", 0):
		return "Document AI is disabled."

	try:
		from smart_journal.api.google_document_ai import describe_processor
	except ImportError:
		return "Document AI package is not installed."

	missing = []
	if not (settings.document_ai_invoice_processor_id or "").strip():
		missing.append("invoice processor")
	if not (settings.document_ai_expense_processor_id or "").strip():
		missing.append("expense processor")
	if missing:
		return "Document AI configured partially; missing " + " and ".join(missing) + "."

	describe_processor(settings, "invoice")
	describe_processor(settings, "expense")
	return "Document AI processors reachable."


def _test_cloud_run(settings):
	if not getattr(settings, "google_cloud_run_enabled", 0):
		return ""
	if not (getattr(settings, "google_cloud_run_url", "") or "").strip():
		frappe.throw("Cloud Run processor is enabled, but Cloud Run URL is missing.")

	from smart_journal.api.google_cloud_run import test_cloud_run

	return test_cloud_run(settings)
