# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


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
		return "claude-sonnet-4-6" if self.provider == "Anthropic" else "gpt-4o"


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


@frappe.whitelist()
def test_connection():
	"""Make a tiny call to the configured provider to verify the API key works."""
	settings = frappe.get_single("AI Settings")
	if not settings.enabled:
		return {"ok": False, "message": "AI Settings is disabled. Tick 'Enabled' and save first."}

	api_key = settings.get_api_key()
	model = settings.get_default_model()
	provider = settings.provider

	try:
		if provider == "Anthropic":
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

		return {"ok": False, "message": f"Unknown provider: {provider}"}

	except Exception as e:
		return {"ok": False, "message": f"Connection failed: {e}"}
