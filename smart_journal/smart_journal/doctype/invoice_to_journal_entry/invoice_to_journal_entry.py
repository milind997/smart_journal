# Copyright (c) 2026, Raissyon and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt

# Tolerance (in account currency) for net + VAT vs total rounding differences.
ROUNDING_TOLERANCE = 0.05


class InvoicetoJournalEntry(Document):
	def validate(self):
		self._check_math()
		self._check_duplicate()

	def _check_math(self):
		"""Warn if net + VAT does not reconcile to total beyond tolerance."""
		if not (self.net_amount or self.vat_amount or self.total_amount):
			return
		diff = flt(self.total_amount) - (flt(self.net_amount) + flt(self.vat_amount))
		if abs(diff) > ROUNDING_TOLERANCE:
			frappe.msgprint(
				_("Net ({0}) + VAT ({1}) = {2} does not match Total ({3}). Difference: {4}. Please review before creating the Journal Entry.").format(
					flt(self.net_amount), flt(self.vat_amount),
					flt(self.net_amount) + flt(self.vat_amount), flt(self.total_amount), diff
				),
				indicator="orange", alert=True,
			)

	def _check_duplicate(self):
		"""Block the same supplier invoice being booked twice."""
		if not (self.supplier_vat and self.invoice_no):
			return
		existing = frappe.db.get_value(
			"Invoice to Journal Entry",
			{
				"supplier_vat": self.supplier_vat,
				"invoice_no": self.invoice_no,
				"status": "JE Created",
				"name": ["!=", self.name],
			},
			["name", "journal_entry"],
		)
		if existing:
			frappe.throw(
				_("This invoice (Supplier VAT {0}, Invoice No {1}) was already booked in {2} → Journal Entry {3}.").format(
					self.supplier_vat, self.invoice_no, existing[0], existing[1]
				)
			)

	def _build_expense_lines(self, net):
		"""Split the net expense across accounts based on line-item routing.

		Returns a list of (account, amount) debit lines summing to ``net``.
		Line items carrying an ``account`` are grouped by it; the rest fall back
		to the invoice's Expense Account. If there are no line items, or their
		amounts do not reconcile to net within tolerance, post the whole net to
		the single chosen Expense Account.
		"""
		groups = {}
		for it in (self.items or []):
			account = it.account or self.expense_account
			groups[account] = flt(groups.get(account, 0)) + flt(it.amount)

		grouped_total = flt(sum(groups.values()))
		if not groups or abs(grouped_total - net) > ROUNDING_TOLERANCE:
			# No usable line items — keep the simple single-account entry.
			return [(self.expense_account, net)]

		# Absorb sub-tolerance rounding into the largest line so debits == net.
		diff = flt(net - grouped_total)
		if diff:
			largest = max(groups, key=lambda a: groups[a])
			groups[largest] = flt(groups[largest] + diff)
		return list(groups.items())

	@frappe.whitelist()
	def create_journal_entry(self):
		"""Build a draft Journal Entry from the reviewed invoice data."""
		if self.journal_entry:
			frappe.throw(_("Journal Entry {0} is already linked to this record.").format(self.journal_entry))

		if self.direction == "Seller (Sale)":
			frappe.throw(_("This document was detected as a SALE (you are the seller). This tool only books purchase invoices."))

		net = flt(self.net_amount)
		vat = flt(self.vat_amount)
		total = flt(self.total_amount)

		if not total:
			frappe.throw(_("Total Amount is required to create a Journal Entry."))
		if not self.cost_center:
			frappe.throw(_("Cost Center is required."))

		# Reconcile small rounding differences onto the expense line so the entry balances.
		debit_total = net + vat
		diff = total - debit_total
		if abs(diff) > ROUNDING_TOLERANCE:
			frappe.throw(
				_("Net + VAT ({0}) does not balance with Total ({1}). Fix the amounts before creating the entry.").format(debit_total, total)
			)
		net = flt(net + diff)  # absorb rounding into the expense amount

		remark = self.user_remark or _("شراء مواد - {0}").format(self.supplier_name or self.invoice_no or "")

		je = frappe.new_doc("Journal Entry")
		je.voucher_type = "Journal Entry"
		je.company = self.company
		je.posting_date = self.posting_date
		je.cheque_no = self.invoice_no
		je.cheque_date = self.invoice_date
		je.user_remark = remark

		# Line 1 — Expense (DEBIT) = net, routed per line item across accounts.
		for account, amount in self._build_expense_lines(net):
			je.append("accounts", {
				"account": account,
				"debit_in_account_currency": amount,
				"cost_center": self.cost_center,
				"project": self.project,
				"user_remark": remark,
			})

		# Line 2 — VAT Input (DEBIT) = vat
		if vat:
			je.append("accounts", {
				"account": self.vat_account,
				"debit_in_account_currency": vat,
				"cost_center": self.cost_center,
			})

		# Line 3 — Paying side (CREDIT) = total
		if self.pay_mode == "Bank":
			if not self.bank_account:
				frappe.throw(_("Select the Bank/Cash account you paid from."))
			je.append("accounts", {
				"account": self.bank_account,
				"credit_in_account_currency": total,
				"cost_center": self.cost_center,
			})
		else:  # On Credit -> Supplier payable
			if not (self.supplier and self.supplier_creditor_account):
				frappe.throw(_("Select the Supplier and the Supplier Payable account for a credit purchase."))
			je.append("accounts", {
				"account": self.supplier_creditor_account,
				"party_type": "Supplier",
				"party": self.supplier,
				"credit_in_account_currency": total,
				"cost_center": self.cost_center,
			})

		je.insert()  # stays as a DRAFT (docstatus 0)

		# Attach a copy of the original invoice to the Journal Entry for audit.
		if self.invoice_file:
			try:
				frappe.get_doc({
					"doctype": "File",
					"file_url": self.invoice_file,
					"attached_to_doctype": "Journal Entry",
					"attached_to_name": je.name,
				}).insert(ignore_permissions=True)
			except Exception:
				frappe.log_error(frappe.get_traceback(), "smart_journal: attach invoice to JE failed")

		self.db_set("journal_entry", je.name)
		self.db_set("status", "JE Created")

		return je.name
