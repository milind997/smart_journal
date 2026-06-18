// Smart Journal desk bundle.
// Loaded globally on desk via `app_include_js` in hooks.py.
//
// NOTE: form scripts for CUSTOM doctypes (e.g. "Purchase Request") must be
// loaded this way. Frappe's `doctype_js` hook is ignored for custom doctypes
// (see FormMeta.add_code() — it returns early when doctype.custom == 1), so the
// only way to ship their client scripts from an app is to bundle them here.
import "./purchase_request";
