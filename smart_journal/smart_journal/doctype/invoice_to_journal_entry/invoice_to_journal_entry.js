// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt

frappe.ui.form.on("Invoice to Journal Entry", {
	setup(frm) {
		const company = () => frm.doc.company;

		// Expense account: any non-group account of the company.
		frm.set_query("expense_account", () => account_query(company(), null));
		// Per-line override account (Line Items table).
		frm.set_query("account", "items", () => account_query(company(), null));
		frm.set_query("vat_account", () => account_query(company(), "Tax"));
		frm.set_query("supplier_creditor_account", () => account_query(company(), "Payable"));
		frm.set_query("bank_account", () => ({
			filters: { company: company(), is_group: 0, account_type: ["in", ["Bank", "Cash"]] },
		}));
		frm.set_query("cost_center", () => ({
			filters: { company: company(), is_group: 0 },
		}));
		frm.set_query("project", () => ({
			filters: { company: company() },
		}));
	},

	refresh(frm) {
		frm.disable_save = false;

		// Extract button — runs QR decode + AI vision and fills the fields.
		if (!frm.is_new() && frm.doc.invoice_file && frm.doc.status !== "JE Created") {
			frm.add_custom_button(__("Extract from Invoice"), () => {
				frappe.dom.freeze(__("Reading invoice (QR + AI vision)…"));
				frappe
					.call({
						method: "smart_journal.api.extraction.extract_invoice",
						args: { docname: frm.doc.name },
					})
					.then((r) => {
						frappe.dom.unfreeze();
						if (r.message) {
							const m = r.message;
							const dir = m.direction === "Seller (Sale)"
								? `<span class="text-danger">${m.direction} — this tool books purchases only</span>`
								: `<span class="text-success">${m.direction}</span>`;
							frappe.msgprint({
								title: __("Extracted"),
								indicator: m.qr_found ? "green" : "orange",
								message: `Supplier: <b>${frappe.utils.escape_html(m.supplier_name || "?")}</b><br>
									Invoice: <b>${frappe.utils.escape_html(m.invoice_no || "?")}</b><br>
									Net ${format_currency(m.net)} + VAT ${format_currency(m.vat)} = Total <b>${format_currency(m.total)}</b><br>
									Direction: ${dir}<br>
									QR: ${m.qr_found ? "found ✓" : "not found"} · Confidence: ${frappe.utils.escape_html(m.confidence || "")}`,
							});
							frm.reload_doc();
						}
					})
					.catch(() => frappe.dom.unfreeze());
			}).addClass("btn-primary");
		}

		// Create Journal Entry button.
		if (!frm.is_new() && !frm.doc.journal_entry && frm.doc.total_amount) {
			frm.add_custom_button(__("Create Journal Entry"), () => {
				frappe.confirm(
					__("Create a draft Journal Entry for total {0}?", [format_currency(frm.doc.total_amount)]),
					() => {
						frm.call("create_journal_entry").then((r) => {
							if (r.message) {
								frappe.show_alert({ message: __("Journal Entry {0} created", [r.message]), indicator: "green" });
								frm.reload_doc();
							}
						});
					},
				);
			}).addClass("btn-primary");
		}

		// Open the created Journal Entry.
		if (frm.doc.journal_entry) {
			frm.add_custom_button(__("Open Journal Entry"), () => {
				frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
			});
		}
	},

	pay_mode(frm) {
		frm.refresh_fields(["bank_account", "supplier", "supplier_creditor_account"]);
	},

	net_amount(frm) {
		recompute_total(frm);
	},
	vat_amount(frm) {
		recompute_total(frm);
	},
});

function account_query(company, account_type) {
	const filters = { company: company, is_group: 0 };
	if (account_type) filters.account_type = account_type;
	return { filters };
}

function recompute_total(frm) {
	// Convenience: if total is empty, suggest net + VAT.
	if (!frm.doc.total_amount && (frm.doc.net_amount || frm.doc.vat_amount)) {
		frm.set_value("total_amount", flt(frm.doc.net_amount) + flt(frm.doc.vat_amount));
	}
}
