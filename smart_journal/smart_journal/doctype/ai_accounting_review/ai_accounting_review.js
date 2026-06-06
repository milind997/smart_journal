// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Accounting Review", {
	onload(frm) {
		if (frm.is_new() && !frm.doc.posting_date) {
			frm.set_value("posting_date", frappe.datetime.get_today());
		}
	},

	refresh(frm) {
		_render_status_badge(frm);
		_setup_field_filters(frm);
		_add_buttons(frm);
	},

	company(frm) {
		// Clear company-specific accounts when company changes.
		frm.set_value("bank_account", "");
		frm.set_value("cost_center", "");
		_setup_field_filters(frm);
	},

	purchase_request(frm) {
		if (!frm.doc.purchase_request) return;
		// Auto-fill company from the linked PR.
		frappe.db
			.get_value("Purchase Request", frm.doc.purchase_request, "company")
			.then((r) => {
				if (r && r.message && r.message.company && !frm.doc.company) {
					frm.set_value("company", r.message.company);
				}
			});
	},
});

// ---------------------------------------------------------------------------
// Status badge — coloured indicator at the top of the form
// ---------------------------------------------------------------------------

function _render_status_badge(frm) {
	const colours = {
		Draft: "gray",
		"AI Suggested": "blue",
		"Under Review": "orange",
		Approved: "green",
		Rejected: "red",
		"Journal Entry Created": "green",
		Cancelled: "gray",
	};
	const colour = colours[frm.doc.status] || "gray";

	// Validation warning strip
	if (frm.doc.validation_status === "Warning") {
		frm.dashboard.add_comment(
			__("⚠️ Some accounts could not be matched automatically. Please review the Accounting Rows."),
			"orange",
			true,
		);
	}
	if (frm.doc.validation_status === "Failed") {
		frm.dashboard.add_comment(
			__("❌ AI processing failed. Check AI Remarks for details."),
			"red",
			true,
		);
	}
	if (frm.doc.status === "AI Suggested") {
		frm.dashboard.add_comment(
			__(
				"✅ AI has suggested account entries. Review the Accounting Rows below, " +
				"select the Bank Account, then click Create Journal Entry.",
			),
			"blue",
			true,
		);
	}
}

// ---------------------------------------------------------------------------
// Field filters — scoped to the selected company
// ---------------------------------------------------------------------------

function _setup_field_filters(frm) {
	const co = () => frm.doc.company;

	frm.set_query("bank_account", () => ({
		filters: { company: co(), is_group: 0, account_type: ["in", ["Bank", "Cash"]] },
	}));
	frm.set_query("cost_center", () => ({
		filters: { company: co(), is_group: 0 },
	}));
	frm.set_query("project", () => ({
		filters: { company: co() },
	}));

	// Filters for the Accounting Rows child table
	frm.set_query("suggested_account", "accounting_rows", () => ({
		filters: { company: co(), is_group: 0 },
	}));
	frm.set_query("accountant_account", "accounting_rows", () => ({
		filters: { company: co(), is_group: 0 },
	}));
	frm.set_query("final_account", "accounting_rows", () => ({
		filters: { company: co(), is_group: 0 },
	}));
}

// ---------------------------------------------------------------------------
// Buttons
// ---------------------------------------------------------------------------

function _add_buttons(frm) {
	if (frm.is_new()) return;

	const status = frm.doc.status;

	// ---- Open linked JE ----
	if (frm.doc.journal_entry) {
		frm.add_custom_button(__("📄 Open Journal Entry"), () => {
			frappe.set_route("Form", "Journal Entry", frm.doc.journal_entry);
		});
	}

	// ---- Open source PR ----
	if (frm.doc.purchase_request) {
		frm.add_custom_button(__("🔗 Open Purchase Request"), () => {
			frappe.set_route("Form", "Purchase Request", frm.doc.purchase_request);
		});
	}

	// ---- Create Journal Entry ---- (main action)
	const canCreate =
		!frm.doc.journal_entry &&
		["AI Suggested", "Under Review", "Approved"].includes(status);

	if (canCreate) {
		frm.add_custom_button(__("✅ Create Journal Entry"), () => {
			_confirm_and_create_je(frm);
		}).addClass("btn-primary");
	}

	// ---- Mark as Under Review ----
	if (status === "AI Suggested") {
		frm.add_custom_button(__("👁 Mark Under Review"), () => {
			frm.set_value("status", "Under Review");
			frm.save();
		});
	}
}

// ---------------------------------------------------------------------------
// Create Journal Entry flow
// ---------------------------------------------------------------------------

function _confirm_and_create_je(frm) {
	if (!frm.doc.bank_account) {
		frappe.msgprint({
			title: __("Bank Account Required"),
			message: __(
				"Please select the <b>Paid From (Bank / Cash)</b> account " +
				"in the Posting Details section before creating the Journal Entry.",
			),
			indicator: "orange",
		});
		frm.scroll_to_field("bank_account");
		return;
	}

	// Warn if any row has no final account
	const missing = (frm.doc.accounting_rows || []).filter(
		(r) => !r.final_account && !r.accountant_account && !r.suggested_account,
	);
	if (missing.length) {
		frappe.msgprint({
			title: __("Missing Accounts"),
			message: __(
				"<b>{0}</b> row(s) have no account assigned. " +
				"Please fill in the account for each row in the Accounting Rows table.",
				[missing.length],
			),
			indicator: "red",
		});
		return;
	}

	const total = flt(frm.doc.total_suggested_credit);
	frappe.confirm(
		__(
			"Create a draft Journal Entry for <b>SAR {0}</b> credited from <b>{1}</b>?",
			[format_currency(total), frm.doc.bank_account],
		),
		() => {
			frappe.dom.freeze(__("Creating Journal Entry…"));
			frm
				.call("create_journal_entry")
				.then((r) => {
					frappe.dom.unfreeze();
					if (r.message) {
						frappe.show_alert(
							{
								message:
									__("Journal Entry created: ") +
									`<a href='/app/journal-entry/${r.message}'><b>${r.message}</b></a>`,
								indicator: "green",
							},
							10,
						);
						frm.reload_doc();
					}
				})
				.catch(() => frappe.dom.unfreeze());
		},
	);
}
