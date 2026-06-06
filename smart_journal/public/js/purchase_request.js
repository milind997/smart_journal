// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt
//
// Adds "Create AI Review" button to Purchase Request form.

frappe.ui.form.on("Purchase Request", {
	refresh(frm) {
		if (frm.is_new()) return;

		// wf_status is the status field on this custom PR doctype.
		const wf = (
			frm.doc.wf_status ||
			frm.doc.workflow_state ||
			frm.doc.status ||
			""
		).toLowerCase();

		// Hide button only on truly terminal / blocked states.
		if (wf === "draft" || wf.includes("reject") || wf.includes("cancel")) return;

		// Always render the button immediately — no async pre-check.
		// The server returns already_exists=true if a review already exists.
		frm
			.add_custom_button(__("🤖 Create AI Review"), () => _on_click(frm))
			.addClass("btn-primary");
	},
});

function _on_click(frm) {
	frappe.confirm(
		__(
			"AI will read all attachments on this Purchase Request and suggest " +
				"a Journal Entry split by expense type. Continue?",
		),
		() => _start_review(frm),
	);
}

function _start_review(frm) {
	frappe.dom.freeze(__("Sending to AI… please wait"));

	frappe.call({
		method: "smart_journal.api.pr_automation.create_ai_review",
		args: { pr_name: frm.doc.name },
		callback(r) {
			frappe.dom.unfreeze();
			if (!r || !r.message) return;

			const { review_name, already_exists } = r.message;

			if (already_exists) {
				frappe.msgprint({
					title: __("Review Already Exists"),
					message:
						__("An AI Review already exists for this Purchase Request. ") +
						`<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open it →")}</b></a>`,
					indicator: "orange",
				});
				return;
			}

			frappe.show_alert(
				{
					message:
						__("✅ Review created! AI is reading attachments in the background. ") +
						`<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review →")}</b></a>`,
					indicator: "blue",
				},
				15,
			);

			_poll_until_done(review_name);
		},
		error() {
			frappe.dom.unfreeze();
			frappe.msgprint({
				title: __("Error"),
				message: __("Could not create AI Review. Check the error log or AI Settings."),
				indicator: "red",
			});
		},
	});
}

function _poll_until_done(review_name) {
	let attempts = 0;
	const MAX = 75; // ~5 minutes

	const interval = setInterval(() => {
		if (++attempts > MAX) {
			clearInterval(interval);
			frappe.show_alert(
				{
					message:
						__("AI is taking longer than expected. ") +
						`<a href='/app/ai-accounting-review/${review_name}'>${__("Open review →")}</a>`,
					indicator: "orange",
				},
				10,
			);
			return;
		}

		frappe.call({
			method: "smart_journal.api.pr_automation.get_review_status",
			args: { review_name },
			callback(r) {
				if (!r || !r.message) return;
				const { status, validation_status, row_count } = r.message;
				if (status === "Draft") return; // still processing

				clearInterval(interval);

				const ok = validation_status !== "Failed";
				frappe.show_alert(
					{
						message: ok
							? `✅ ${row_count} ` +
								__("account rows suggested. ") +
								`<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review →")}</b></a>`
							: __("⚠️ AI review completed with warnings. ") +
								`<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review →")}</b></a>`,
						indicator: ok ? "green" : "orange",
					},
					15,
				);
			},
		});
	}, 4000);
}
