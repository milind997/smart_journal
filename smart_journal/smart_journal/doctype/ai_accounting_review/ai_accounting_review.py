# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now


class AIAccountingReview(Document):
	def validate(self):
		self._sync_final_accounts()
		self._check_mandatory()
		self._check_status_consistency()

	# ------------------------------------------------------------------
	# Validation helpers
	# ------------------------------------------------------------------

	def _sync_final_accounts(self):
		"""Use accountant_account override when set, else fall back to AI suggestion."""
		for row in self.accounting_rows or []:
			row.final_account = row.accountant_account or row.suggested_account

	def _check_mandatory(self):
		if not self.company:
			frappe.throw(_("Company is mandatory."))
		if not self.purchase_request:
			frappe.throw(_("Purchase Request is mandatory."))

	def _check_status_consistency(self):
		if self.status == "Approved" and self.validation_status == "Failed":
			frappe.throw(
				_("Validation Status is 'Failed'. Resolve issues before setting status to 'Approved'.")
			)
		if self.journal_entry and self.status != "Journal Entry Created":
			frappe.throw(
				_("Journal Entry {0} is linked — Status must be 'Journal Entry Created'.").format(
					self.journal_entry
				)
			)

	# ------------------------------------------------------------------
	# Journal Entry creation
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def create_journal_entry(self):
		"""Build a draft Journal Entry from the reviewed accounting rows."""
		if self.journal_entry:
			frappe.throw(
				_("Journal Entry {0} is already linked to this review.").format(self.journal_entry)
			)
		if not self.bank_account:
			frappe.throw(
				_("Please select the 'Paid From (Bank / Cash)' account before creating the Journal Entry.")
			)
		if not self.accounting_rows:
			frappe.throw(_("No accounting rows found. Run AI extraction first."))

		# Collect debit rows — use final_account (accountant override wins over AI suggestion)
		debit_rows = []
		for row in self.accounting_rows:
			account = row.final_account or row.accountant_account or row.suggested_account
			if not account:
				frappe.throw(
					_("Row '{0}' has no account assigned. Please select an account before creating the Journal Entry.").format(
						row.description or row.idx
					)
				)
			if flt(row.debit) <= 0:
				continue
			debit_rows.append({"account": account, "amount": flt(row.debit)})

		if not debit_rows:
			frappe.throw(_("All accounting rows have zero debit amount."))

		total_debit = sum(r["amount"] for r in debit_rows)

		remark = (
			self.user_remark
			or f"({frappe.db.get_value('Purchase Request', self.purchase_request, 'title') or self.purchase_request})"
		)

		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = self.posting_date
		je.user_remark = remark

		# Debit rows — one per accounting row
		for row in debit_rows:
			je.append("accounts", {
				"account": row["account"],
				"debit_in_account_currency": row["amount"],
				"cost_center": self.cost_center or None,
				"project": self.project or None,
				"user_remark": remark,
			})

		# Credit row — single line to the bank / cash account
		je.append("accounts", {
			"account": self.bank_account,
			"credit_in_account_currency": total_debit,
			"cost_center": self.cost_center or None,
			"user_remark": remark,
		})

		# Link JE directly to the Purchase Request using the standard field
		# (Journal Entry has a purchase_request link field in this system).
		try:
			je.purchase_request = self.purchase_request
		except Exception:
			pass

		je.insert()  # saved as DRAFT (docstatus=0)

		# Stamp review as done
		self.db_set("journal_entry", je.name)
		self.db_set("status", "Journal Entry Created")
		self.db_set("reviewed_by", frappe.session.user)
		self.db_set("reviewed_on", now())

		frappe.db.commit()
		return je.name
