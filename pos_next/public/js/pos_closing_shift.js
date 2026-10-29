frappe.ui.form.on("POS Closing Shift", {
	refresh(frm) {
		if (frm.doc.docstatus !== 1) return;

		const status = frm.doc.custom_consumption_status;
		const colour = { Processed: "green", "Nothing to Consume": "green", Pending: "orange", Failed: "red" }[status];
		if (colour) {
			frm.dashboard.add_indicator(__("Recipe consumption: {0}", [__(status)]), colour);
		}

		if (status === "Failed" || status === "Pending") {
			if (status === "Failed" && frm.doc.custom_consumption_log) {
				frm.dashboard.set_headline_alert(
					frappe.utils.escape_html(frm.doc.custom_consumption_log),
					"red"
				);
			}
			frm.add_custom_button(
				__("Process Recipe Consumption"),
				() => {
					frappe.call({
						method: "pos_next.api.recipe_consumption.retry_closing_shift",
						args: { closing_shift: frm.doc.name },
						freeze: true,
						callback: () => {
							frappe.show_alert({ message: __("Queued. Refresh in a minute."), indicator: "green" });
							frm.reload_doc();
						},
					});
				},
				__("Actions")
			);
		}
	},
});