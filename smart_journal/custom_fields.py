# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt
"""
Custom fields injected onto the custom "Purchase Request" doctype.

The "Invoices & Expense Breakdown" section (invoice attachments + expense
breakdown tables and their helper fields) has been removed. No custom fields
are currently added; create() is kept as a no-op so the after_install /
after_migrate hooks stay valid.
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

CUSTOM_FIELDS = {}


def create():
	"""Idempotent — safe to call on every install / migrate."""
	if not frappe.db.exists("DocType", "Purchase Request"):
		# PR doctype not created yet on this site — skip silently.
		return
	if CUSTOM_FIELDS:
		create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
