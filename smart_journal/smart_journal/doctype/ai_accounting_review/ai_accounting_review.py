# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, now


class AIAccountingReview(Document):
	def validate(self):
		self._sync_final_accounts()
		self._compute_totals()
		self._check_mandatory()
		self._check_status_consistency()

	def before_submit(self):
		# A submitted review must be a balanced entry, exactly like a Journal Entry.
		if abs(flt(self.difference_amount)) > 0.01:
			frappe.throw(
				_("Total Debit ({0}) and Total Credit ({1}) must be equal before submitting.").format(
					flt(self.total_suggested_debit), flt(self.total_suggested_credit)
				)
			)

	# ------------------------------------------------------------------
	# Validation helpers
	# ------------------------------------------------------------------

	def _sync_final_accounts(self):
		"""Use accountant_account override when set, else fall back to AI suggestion."""
		for row in self.accounting_rows or []:
			row.final_account = row.accountant_account or row.suggested_account

	def _compute_totals(self):
		"""Roll up Debit / Credit / Difference from the rows, like a Journal Entry."""
		dr = sum(flt(r.debit) for r in (self.accounting_rows or []))
		cr = sum(flt(r.credit) for r in (self.accounting_rows or []))
		self.total_suggested_debit = dr
		self.total_suggested_credit = cr
		self.difference_amount = flt(dr - cr)

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
	# Re-run AI extraction
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def rerun_review(self):
		"""Re-run the AI extraction on the linked Purchase Request.

		Re-reads the PR attachments / breakdown and rebuilds the accounting rows
		from scratch (process_pr_review clears extracted_documents + accounting_rows
		first), so any manual edits to the rows are discarded.

		If a Journal Entry was already created from this review, the link is
		cleared so the rebuild can produce fresh suggestions — the previously
		created (draft) Journal Entry is left untouched and must be reviewed or
		deleted manually. The caller warns the user about both effects.

		Returns: {"review_name": str, "unlinked_journal_entry": str | None}
		"""
		if not self.purchase_request:
			frappe.throw(_("This review has no linked Purchase Request to re-process."))

		unlinked_je = self.journal_entry or None

		# Clear the JE link first so the status reset below doesn't trip
		# _check_status_consistency (which requires status == 'Journal Entry
		# Created' whenever journal_entry is set).
		if unlinked_je:
			self.db_set("journal_entry", None)
		self.db_set("reviewed_by", None)
		self.db_set("reviewed_on", None)

		# Reset to Draft so get_review_status() reports "still processing" while
		# the background job runs (the form polls on the status field).
		self.db_set("status", "Draft")
		self.db_set("validation_status", "Not Validated")
		self.db_set(
			"ai_remarks",
			"⏳ AI is re-reading your attachments in the background. Refresh this page in a moment.",
		)
		frappe.db.commit()

		frappe.enqueue(
			"smart_journal.api.pr_automation.process_pr_review",
			queue="long",
			timeout=600,
			review_name=self.name,
			pr_name=self.purchase_request,
		)

		return {"review_name": self.name, "unlinked_journal_entry": unlinked_je}

	# ------------------------------------------------------------------
	# Journal Entry creation
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def create_journal_entry(self):
		"""Build a draft Journal Entry that mirrors the accounting rows 1:1.

		The rows are a balanced Journal Entry already (each expense line is
		paired with a bank-credit line, like JVS-2210-006). We therefore copy
		every row verbatim — both its debit and its credit side.

		Legacy fallback: older reviews stored debit-only rows with the credit
		held in `bank_account`. If the rows carry no credit at all, we
		synthesise a single bank credit line so those still post correctly.
		"""
		if self.journal_entry:
			frappe.throw(
				_("Journal Entry {0} is already linked to this review.").format(self.journal_entry)
			)
		if not self.accounting_rows:
			frappe.throw(_("No accounting rows found. Run AI extraction first."))

		# Copy every row verbatim — use final_account (accountant override wins).
		lines = []
		for row in self.accounting_rows:
			debit = flt(row.debit)
			credit = flt(row.credit)
			if debit <= 0 and credit <= 0:
				continue
			account = row.final_account or row.accountant_account or row.suggested_account
			if not account:
				frappe.throw(
					_("Row '{0}' has no account assigned. Please select an account before creating the Journal Entry.").format(
						row.description or row.idx
					)
				)
			lines.append({
				"account": account,
				"debit": debit,
				"credit": credit,
				# .get() so this degrades gracefully if the party_type/party fields
				# haven't been migrated into the DB yet.
				"party_type": row.get("party_type") or None,
				"party": row.get("party") or None,
			})

		if not lines:
			frappe.throw(_("All accounting rows have zero amount."))

		total_debit = sum(l["debit"] for l in lines)
		total_credit = sum(l["credit"] for l in lines)

		# Legacy fallback — debit-only rows: synthesise the single bank credit.
		if total_credit == 0:
			if not self.bank_account:
				frappe.throw(
					_("Please select the 'Paid From (Bank / Cash)' account before creating the Journal Entry.")
				)
			lines.append({"account": self.bank_account, "debit": 0, "credit": total_debit})
			total_credit = total_debit

		if abs(total_debit - total_credit) > 0.01:
			frappe.throw(
				_("Rows are not balanced — Total Debit {0} ≠ Total Credit {1}.").format(
					total_debit, total_credit
				)
			)

		remark = (
			self.user_remark
			or f"({frappe.db.get_value('Purchase Request', self.purchase_request, 'title') or self.purchase_request})"
		)

		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = self.posting_date
		je.user_remark = remark

		# Record who was paid in the header (mirrors the reference JE's
		# pay_to_recd_from = "تكافل الراجحي").
		if getattr(self, "supplier", None):
			je.pay_to_recd_from = (
				frappe.db.get_value("Supplier", self.supplier, "supplier_name") or self.supplier
			)

		# Party stays on the line that carries it (the Supplier Payable rows) —
		# the bank line is deliberately left without a party, exactly like the
		# reference Journal Entry.
		for line in lines:
			je.append("accounts", {
				"account": line["account"],
				"debit_in_account_currency": line["debit"],
				"credit_in_account_currency": line["credit"],
				"party_type": line.get("party_type") or None,
				"party": line.get("party") or None,
				"cost_center": self.cost_center or None,
				"project": self.project or None,
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
