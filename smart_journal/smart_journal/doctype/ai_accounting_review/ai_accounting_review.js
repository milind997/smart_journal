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
		_render_balance_indicator(frm);
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

	validate(frm) {
		_recompute_totals(frm);
	},
});

// ---------------------------------------------------------------------------
// Accounting Rows — keep Debit / Credit / Difference live like a Journal Entry
// ---------------------------------------------------------------------------

frappe.ui.form.on("AI Accounting Review Account", {
	debit(frm, cdt, cdn) {
		_clear_other_side(cdt, cdn, "debit");
		_recompute_totals(frm);
	},
	credit(frm, cdt, cdn) {
		_clear_other_side(cdt, cdn, "credit");
		_recompute_totals(frm);
	},
	accounting_rows_remove(frm) {
		_recompute_totals(frm);
	},
});

function _clear_other_side(cdt, cdn, side) {
	// A Journal Entry line is either a debit or a credit, never both.
	const row = locals[cdt][cdn];
	const other = side === "debit" ? "credit" : "debit";
	if (flt(row[side]) && flt(row[other])) {
		frappe.model.set_value(cdt, cdn, other, 0);
	}
}

function _recompute_totals(frm) {
	let dr = 0;
	let cr = 0;
	(frm.doc.accounting_rows || []).forEach((r) => {
		dr += flt(r.debit);
		cr += flt(r.credit);
	});
	frm.set_value("total_suggested_debit", dr);
	frm.set_value("total_suggested_credit", cr);
	frm.set_value("difference_amount", flt(dr - cr));
	_render_balance_indicator(frm);
}

// ---------------------------------------------------------------------------
// Balance indicator — live "Balanced / Off by X" headline so the accountant
// knows whether the entry is ready before clicking Create Journal Entry.
// ---------------------------------------------------------------------------

function _render_balance_indicator(frm) {
	const rows = frm.doc.accounting_rows || [];
	if (!rows.length) {
		frm.dashboard.set_headline("");
		return;
	}

	const dr = flt(frm.doc.total_suggested_debit);
	const cr = flt(frm.doc.total_suggested_credit);
	const diff = flt(frm.doc.difference_amount);
	const balanced = Math.abs(diff) <= 0.01;

	const colour = balanced ? "#28a745" : "#d9534f";
	const text = balanced
		? __("Balanced — Debit {0} = Credit {1}. Ready to create the Journal Entry.", [
				format_currency(dr),
				format_currency(cr),
			])
		: __("Not balanced — Debit {0} vs Credit {1}, off by {2}. Adjust the rows first.", [
				format_currency(dr),
				format_currency(cr),
				format_currency(Math.abs(diff)),
			]);

	frm.dashboard.set_headline(
		`<span style="font-weight:600;color:${colour};">${balanced ? "✅" : "⚠️"} ${text}</span>`,
	);
}

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
	frm.set_query("against_account", "accounting_rows", () => ({
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

	// ---- Review Again ---- (re-run AI extraction on the source PR)
	// Always available when a PR is linked. The backend refuses the re-run if a
	// Journal Entry already exists (re-running would diverge from the posted JE).
	if (frm.doc.purchase_request) {
		frm.add_custom_button(__("🔄 Review Again"), () => {
			_confirm_and_rerun(frm);
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
	const rows = frm.doc.accounting_rows || [];
	const has_credit_rows = rows.some((r) => flt(r.credit) > 0);

	// Bank account is only needed for legacy debit-only rows (credit synthesised).
	if (!has_credit_rows && !frm.doc.bank_account) {
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

	// Warn if any row with an amount has no account
	const missing = rows.filter(
		(r) =>
			(flt(r.debit) > 0 || flt(r.credit) > 0) &&
			!r.final_account &&
			!r.accountant_account &&
			!r.suggested_account,
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

	// Enforce balance when the rows already carry both sides.
	if (has_credit_rows && Math.abs(flt(frm.doc.difference_amount)) > 0.01) {
		frappe.msgprint({
			title: __("Entry Not Balanced"),
			message: __(
				"Total Debit (<b>{0}</b>) and Total Credit (<b>{1}</b>) must be equal. " +
				"Current difference: <b>{2}</b>.",
				[
					format_currency(flt(frm.doc.total_suggested_debit)),
					format_currency(flt(frm.doc.total_suggested_credit)),
					format_currency(flt(frm.doc.difference_amount)),
				],
			),
			indicator: "red",
		});
		return;
	}

	const total = flt(frm.doc.total_suggested_debit);
	const credit_from = has_credit_rows ? __("the bank credit rows") : frm.doc.bank_account;
	frappe.confirm(
		__(
			"Create a draft Journal Entry for <b>SAR {0}</b> credited from <b>{1}</b>?",
			[format_currency(total), credit_from],
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

// ---------------------------------------------------------------------------
// Review Again flow — re-run the AI extraction on the source Purchase Request.
// The background job rebuilds the Accounting Rows from scratch, so any manual
// edits to the rows are discarded — warn before re-running.
// ---------------------------------------------------------------------------

function _confirm_and_rerun(frm) {
	let message = __(
		"Re-run the AI review on <b>{0}</b>? This rebuilds the Accounting Rows " +
			"from the Purchase Request attachments and <b>discards any manual edits</b> " +
			"to the rows.",
		[frm.doc.purchase_request],
	);

	// If a Journal Entry was already created, re-running unlinks it (the JE
	// itself is kept) so a fresh suggestion can be built — make that explicit.
	if (frm.doc.journal_entry) {
		message +=
			"<br><br>" +
			__(
				"⚠️ Journal Entry <b>{0}</b> is already linked. It will be <b>unlinked</b> " +
					"(the Journal Entry itself is kept) so a new entry can be created from the " +
					"refreshed rows. Review or delete the old Journal Entry afterwards.",
				[frm.doc.journal_entry],
			);
	}

	frappe.confirm(
		message + "<br><br>" + __("Continue?"),
		() => {
			frappe.dom.freeze(__("Sending to AI… please wait"));
			frm
				.call("rerun_review", { review_name: frm.doc.name })
				.then((r) => {
					frappe.dom.unfreeze();
					if (!r || !r.message) return;
					frappe.show_alert(
						{
							message: __(
								"🔄 AI is re-reading the attachments in the background. " +
									"This page will refresh when it is done.",
							),
							indicator: "blue",
						},
						15,
					);
					_poll_rerun_until_done(frm);
				})
				.catch(() => frappe.dom.unfreeze());
		},
	);
}

function _poll_rerun_until_done(frm) {
	let attempts = 0;
	const MAX = 75; // ~5 minutes at 4s intervals

	const interval = setInterval(() => {
		if (++attempts > MAX) {
			clearInterval(interval);
			frappe.show_alert(
				{
					message: __("AI is taking longer than expected. Refresh the page to check."),
					indicator: "orange",
				},
				10,
			);
			return;
		}

		frappe.db.get_value("AI Accounting Review", frm.doc.name, "status").then((res) => {
			const status = res && res.message && res.message.status;
			if (!status || status === "Draft") return; // still processing
			clearInterval(interval);
			frappe.show_alert(
				{ message: __("✅ AI review updated. Reloading…"), indicator: "green" },
				6,
			);
			frm.reload_doc();
		});
	}, 4000);
}
