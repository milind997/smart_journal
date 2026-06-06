// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		// Wire the "Test Connection" button rendered in the HTML field.
		setTimeout(() => {
			const btn = frm.fields_dict.test_connection_html.$wrapper.find("#sj-test-connection");
			const result = frm.fields_dict.test_connection_html.$wrapper.find("#sj-test-result");
			btn.off("click").on("click", () => {
				if (frm.is_dirty()) {
					frappe.msgprint("Please save AI Settings before testing the connection.");
					return;
				}
				result.html("<span class='text-muted'>Testing…</span>");
				frappe.call({
					method: "smart_journal.smart_journal.doctype.ai_settings.ai_settings.test_connection",
					callback: (r) => {
						const m = r.message || {};
						const cls = m.ok ? "text-success" : "text-danger";
						const icon = m.ok ? "✓" : "✗";
						result.html(`<span class="${cls}">${icon} ${frappe.utils.escape_html(m.message || "")}</span>`);
					},
				});
			});
		}, 300);
	},
});
