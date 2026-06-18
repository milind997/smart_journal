# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt
"""
Dashboard "Connections" links injected onto the custom "Purchase Request" doctype.

"Purchase Request" is a user-created (custom) doctype that lives in the database,
not in code — so its connections are DocType Link rows rather than a shipped .json.
Here we ensure the "AI Accounting Review" card appears in the Connections section,
linked back via the review's `purchase_request` field. Run from the
after_install / after_migrate hooks, idempotent.
"""

import frappe

# group label -> (link_doctype, link_fieldname)
DOCTYPE_LINKS = {
	"Reviews": ("AI Accounting Review", "purchase_request"),
}


def create():
	"""Idempotent — safe to call on every install / migrate."""
	if not frappe.db.exists("DocType", "Purchase Request"):
		# PR doctype not created yet on this site — skip silently.
		return
	if not frappe.db.exists("DocType", "AI Accounting Review"):
		return

	doc = frappe.get_doc("DocType", "Purchase Request")
	existing = {l.link_doctype for l in doc.links}
	changed = False
	for group, (link_doctype, link_fieldname) in DOCTYPE_LINKS.items():
		if link_doctype in existing:
			continue
		doc.append(
			"links",
			{
				"group": group,
				"link_doctype": link_doctype,
				"link_fieldname": link_fieldname,
			},
		)
		changed = True

	if changed:
		doc.save(ignore_permissions=True)
