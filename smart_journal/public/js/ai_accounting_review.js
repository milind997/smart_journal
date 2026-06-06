// Copyright (c) 2026, Raissyon and contributors
// For license information, please see license.txt

frappe.ui.form.on("AI Accounting Review Account", {
	form_render(frm, cdt, cdn) {
		const row = locals[cdt][cdn];

		// Primary: use attachment stored on the row.
		// Fallback: scan extracted_documents on the parent doc and match by
		// filename found in ai_reason (handles rows created before attachment
		// field was added).
		let url = row.attachment || _find_attachment_from_extracted(frm, row);

		const grid = frm.fields_dict.accounting_rows && frm.fields_dict.accounting_rows.grid;
		if (!grid) return;
		const grid_row = grid.grid_rows_by_docname && grid.grid_rows_by_docname[cdn];
		if (!grid_row || !grid_row.grid_form) return;
		const preview_field = grid_row.grid_form.fields_dict.attachment_preview;
		if (!preview_field) return;

		if (!url) {
			preview_field.$wrapper.html(
				`<div style="padding:10px 0;color:#8d99a6;font-size:12px;">No attachment found</div>`
			);
			return;
		}

		_render_preview(preview_field.$wrapper, url);
	},
});

function _find_attachment_from_extracted(frm, row) {
	const extracted = frm.doc.extracted_documents || [];
	if (!extracted.length) return null;

	// Only one extracted doc → use it directly
	if (extracted.length === 1) return extracted[0].attachment || null;

	// Try to match filenames mentioned in ai_reason
	const reason = (row.ai_reason || "").toLowerCase();
	for (const ed of extracted) {
		if (!ed.attachment) continue;
		const fname = ed.attachment.split("/").pop().toLowerCase();
		if (fname && reason.includes(fname)) return ed.attachment;
	}

	// Last resort: return the first available attachment
	return (extracted.find((e) => e.attachment) || {}).attachment || null;
}

function _render_preview($wrapper, url) {
	const is_pdf = /\.pdf(\?|$)/i.test(url);
	const is_image = /\.(png|jpg|jpeg|gif|bmp|webp)(\?|$)/i.test(url);

	const safe_url = frappe.utils.escape_html(url);
	const label = `<div style="font-size:11px;color:#8d99a6;margin-bottom:6px;font-weight:500;">
		📎 Invoice Preview
	</div>`;

	if (is_pdf) {
		$wrapper.html(`
			<div style="margin-top:8px;">
				${label}
				<iframe
					src="${safe_url}"
					style="width:100%;height:520px;border:1px solid #d1d8dd;border-radius:6px;background:#fff;"
				></iframe>
			</div>
		`);
	} else if (is_image) {
		$wrapper.html(`
			<div style="margin-top:8px;">
				${label}
				<img
					src="${safe_url}"
					style="max-width:100%;max-height:520px;object-fit:contain;
					       border:1px solid #d1d8dd;border-radius:6px;display:block;"
				/>
			</div>
		`);
	} else {
		$wrapper.html(`
			<div style="margin-top:8px;">
				<a href="${safe_url}" target="_blank" class="btn btn-default btn-sm">
					📎 Open Attachment
				</a>
			</div>
		`);
	}
}
