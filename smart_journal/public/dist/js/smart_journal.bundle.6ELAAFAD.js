(() => {
  // ../smart_journal/smart_journal/public/js/purchase_request.js
  frappe.ui.form.on("Purchase Request", {
    refresh(frm) {
      if (frm.is_new())
        return;
      frm.add_custom_button(__("\u{1F916} Create AI Review"), () => _on_click(frm));
    }
  });
  function _on_click(frm) {
    frappe.confirm(
      __(
        "Send this Purchase Request to accounting? The AI Review will read the attachments, assign account heads and build a balanced Journal Entry. Continue?"
      ),
      () => _start_review(frm)
    );
  }
  function _start_review(frm) {
    frappe.dom.freeze(__("Sending to AI\u2026 please wait"));
    frappe.call({
      method: "smart_journal.api.pr_automation.create_ai_review",
      args: { pr_name: frm.doc.name },
      callback(r) {
        frappe.dom.unfreeze();
        if (!r || !r.message)
          return;
        const { review_name, already_exists } = r.message;
        if (already_exists) {
          frappe.msgprint({
            title: __("Review Already Exists"),
            message: __("An AI Review already exists for this Purchase Request. ") + `<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open it \u2192")}</b></a>`,
            indicator: "orange"
          });
          return;
        }
        frappe.show_alert(
          {
            message: __("\u2705 Review created! AI is reading attachments in the background. ") + `<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review \u2192")}</b></a>`,
            indicator: "blue"
          },
          15
        );
        _poll_until_done(review_name);
      },
      error() {
        frappe.dom.unfreeze();
        frappe.msgprint({
          title: __("Error"),
          message: __("Could not create AI Review. Check the error log or AI Settings."),
          indicator: "red"
        });
      }
    });
  }
  function _poll_until_done(review_name) {
    let attempts = 0;
    const MAX = 75;
    const interval = setInterval(() => {
      if (++attempts > MAX) {
        clearInterval(interval);
        frappe.show_alert(
          {
            message: __("AI is taking longer than expected. ") + `<a href='/app/ai-accounting-review/${review_name}'>${__("Open review \u2192")}</a>`,
            indicator: "orange"
          },
          10
        );
        return;
      }
      frappe.call({
        method: "smart_journal.api.pr_automation.get_review_status",
        args: { review_name },
        callback(r) {
          if (!r || !r.message)
            return;
          const { status, validation_status, row_count } = r.message;
          if (status === "Draft")
            return;
          clearInterval(interval);
          const ok = validation_status !== "Failed";
          frappe.show_alert(
            {
              message: ok ? `\u2705 ${row_count} ` + __("account rows suggested. ") + `<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review \u2192")}</b></a>` : __("\u26A0\uFE0F AI review completed with warnings. ") + `<a href='/app/ai-accounting-review/${review_name}'><b>${__("Open Review \u2192")}</b></a>`,
              indicator: ok ? "green" : "orange"
            },
            15
          );
        }
      });
    }, 4e3);
  }
})();
//# sourceMappingURL=smart_journal.bundle.6ELAAFAD.js.map
