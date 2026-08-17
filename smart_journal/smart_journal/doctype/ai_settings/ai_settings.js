// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt

const SJ_PROVIDER_DEFAULT_MODELS = {
	Anthropic: "claude-sonnet-4-6",
	OpenAI: "gpt-4o",
	"Google Vertex AI": "gemini-2.5-flash",
};

function sj_update_provider_requirements(frm) {
	const is_google_vertex = frm.doc.provider === "Google Vertex AI";
	frm.toggle_reqd("api_key", !is_google_vertex);
	frm.toggle_reqd("google_project_id", is_google_vertex);
	frm.toggle_reqd("google_location", is_google_vertex);
}

function sj_set_provider_default_model(frm) {
	const default_model = SJ_PROVIDER_DEFAULT_MODELS[frm.doc.provider];
	if (!default_model) return;

	const known_default_models = Object.values(SJ_PROVIDER_DEFAULT_MODELS);
	if (!frm.doc.model || known_default_models.includes(frm.doc.model)) {
		frm.set_value("model", default_model);
	}
}

frappe.ui.form.on("AI Settings", {
	refresh(frm) {
		sj_update_provider_requirements(frm);

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
						let html = `<span class="${cls}">${icon} ${frappe.utils.escape_html(m.message || "")}</span>`;
						if (m.command) {
							html += `<pre style="margin-top: 8px; white-space: pre-wrap;">${frappe.utils.escape_html(m.command)}</pre>`;
						}
						if (m.details) {
							html += `<div class="text-muted small">${frappe.utils.escape_html(m.details)}</div>`;
						}
						result.html(html);
					},
				});
			});
		}, 300);
	},

	provider(frm) {
		sj_update_provider_requirements(frm);
		sj_set_provider_default_model(frm);
	},
});
