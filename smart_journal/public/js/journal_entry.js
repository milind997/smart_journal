// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt
//
// Adds an "Extract from Invoice" button to Journal Entry. It opens a dialog
// where you attach an invoice, run extraction (QR + AI vision), then add
// account rows (Debit/Credit) — one click per row — straight into the entry.

frappe.ui.form.on("Journal Entry", {
	refresh(frm) {
		frm.add_custom_button(__("Extract from Invoice"), () => open_extract_dialog(frm));
	},
});

function open_extract_dialog(frm) {
	let suggestions = [];

	const d = new frappe.ui.Dialog({
		title: __("Extract Invoice → Add Row"),
		size: "large",
		fields: [
			{ fieldname: "invoice_file", fieldtype: "Attach", label: __("Invoice File (PDF / Image)") },
			{ fieldname: "extract_btn", fieldtype: "Button", label: __("Extract") },
			{ fieldname: "summary", fieldtype: "HTML" },
			{ fieldname: "row_section", fieldtype: "Section Break", label: __("Add Account Row") },
			{ fieldname: "suggestion", fieldtype: "Select", label: __("Matched Account (suggestion)") },
			{
				fieldname: "account",
				fieldtype: "Link",
				label: __("Account"),
				options: "Account",
				get_query: () => ({ filters: { company: frm.doc.company, is_group: 0 } }),
			},
			{ fieldname: "col_break", fieldtype: "Column Break" },
			{ fieldname: "dr_cr", fieldtype: "Select", label: __("Type"), options: "Debit\nCredit", default: "Debit" },
			{ fieldname: "amount", fieldtype: "Currency", label: __("Amount") },
		],
		primary_action_label: __("Add Row"),
		primary_action(values) {
			if (!values.account) {
				frappe.msgprint(__("Select an account first."));
				return;
			}
			const amt = flt(values.amount);
			if (!amt) {
				frappe.msgprint(__("Enter an amount."));
				return;
			}
			const row = frm.add_child("accounts", { account: values.account });
			if (values.dr_cr === "Credit") {
				row.credit_in_account_currency = amt;
			} else {
				row.debit_in_account_currency = amt;
			}
			frm.refresh_field("accounts");
			frappe.show_alert({ message: __("Row added: {0}", [values.account]), indicator: "green" });
			// Keep the dialog open so more rows (e.g. VAT, the credit side) can be added.
			d.set_value("account", "");
			d.set_value("amount", 0);
			d.set_value("suggestion", "");
		},
	});

	d.fields_dict.extract_btn.$input.on("click", () => {
		const file_url = d.get_value("invoice_file");
		if (!file_url) {
			frappe.msgprint(__("Attach an invoice file first."));
			return;
		}
		frappe.dom.freeze(__("Reading invoice (QR + AI vision)…"));
		frappe
			.call({
				method: "smart_journal.api.extraction.extract_from_file",
				args: { file_url, company: frm.doc.company },
			})
			.then((r) => {
				frappe.dom.unfreeze();
				if (!r.message) return;
				const m = r.message;
				suggestions = m.suggestions || [];
				render_summary(d, m);
				populate_suggestions(d, suggestions);
				if (suggestions.length) apply_suggestion(d, suggestions, 0);
			})
			.catch(() => frappe.dom.unfreeze());
	});

	d.fields_dict.suggestion.$input.on("change", function () {
		const idx = parseInt((this.value || "").split(":")[0], 10);
		if (!isNaN(idx)) apply_suggestion(d, suggestions, idx);
	});

	d.show();
}

function render_summary(d, m) {
	const dir =
		m.direction === "Seller (Sale)"
			? `<span class="text-danger">${m.direction}</span>`
			: `<span class="text-success">${m.direction || "?"}</span>`;
	d.fields_dict.summary.$wrapper.html(`
		<div class="text-muted" style="padding:4px 0;">
			Supplier: <b>${frappe.utils.escape_html(m.supplier_name || "?")}</b> ·
			Invoice: <b>${frappe.utils.escape_html(m.invoice_no || "?")}</b><br>
			Net <b>${format_currency(m.net)}</b> + VAT <b>${format_currency(m.vat)}</b>
			= Total <b>${format_currency(m.total)}</b> ·
			${dir} · QR: ${m.qr_found ? "found ✓" : "not found"}
		</div>
	`);
}

function populate_suggestions(d, suggestions) {
	const opts = [""].concat(
		suggestions.map((s, i) => `${i}: ${s.account || "(no match)"} — ${format_currency(s.amount)} (${s.dr_cr})`),
	);
	d.set_df_property("suggestion", "options", opts.join("\n"));
	d.refresh();
}

function apply_suggestion(d, suggestions, i) {
	const s = suggestions[i];
	if (!s) return;
	if (s.account) d.set_value("account", s.account);
	d.set_value("amount", s.amount);
	d.set_value("dr_cr", s.dr_cr);
}
