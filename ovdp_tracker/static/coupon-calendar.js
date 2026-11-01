"use strict";

// Read the structured calendar for rendering while leaving malformed payloads for server validation.
function parseCouponPayload(field) {
  try {
    const rows = JSON.parse(field.value);
    if (Array.isArray(rows) && rows.every(row => row && typeof row === "object" &&
        typeof row.date === "string" && typeof row.amount === "string")) return rows;
  } catch {
    // Preserve invalid submitted JSON in the field until the user edits the calendar.
  }
  return [];
}


// Initialize reusable calendar sections before capturing the form's original values.
function initializeCouponEditors() {
  return Array.from(document.querySelectorAll("[data-coupon-editor]"), initializeCouponEditor);
}


// Keep editable rows and their structured form payload synchronized for one calendar.
function initializeCouponEditor(editor) {
  // Locate the source and reusable controls without depending on the surrounding purchase form.
  const state = editor.querySelector("[data-coupon-state]");
  const list = editor.querySelector("[data-coupon-list]");
  const template = editor.querySelector("template");

  // Read ordered date-and-amount pairs, including empty controls awaiting completion.
  function readRows() {
    return Array.from(list.children, row => ({
      date: row.querySelector("[data-coupon-date]").value,
      amount: row.querySelector("[data-coupon-amount]").value,
    }));
  }


  // Publish every edit before shared dirty-form protection or purchase validation can inspect it.
  function publish() {
    const coupons = readRows();
    state.value = JSON.stringify(coupons);
    editor.dispatchEvent(new CustomEvent("couponschange", { bubbles: true, detail: coupons }));
  }


  // Add a labeled native date picker and an exact monetary input without moving their pairing.
  function appendRow(day = "", amount = "") {
    // Preserve malformed dates from rejected submissions instead of clearing them.
    const row = template.content.firstElementChild.cloneNode(true);
    const dateInput = row.querySelector("[data-coupon-date]");
    dateInput.value = day;
    if (dateInput.value !== day) {
      dateInput.type = "text";
      dateInput.placeholder = "ГГГГ-ММ-ДД";
      dateInput.value = day;
    }
    row.querySelector("[data-coupon-amount]").value = amount;

    // Track edits and delete only the selected date and its own amount.
    row.addEventListener("input", publish);
    row.querySelector("button").addEventListener("click", () => {
      const focusTarget = row.nextElementSibling || row.previousElementSibling;
      row.remove();
      publish();
      if (focusTarget) focusTarget.querySelector("input").focus();
      else editor.querySelector("[data-coupon-add]").focus();
    });
    list.append(row);
    return row;
  }


  // Restore the single structured calendar, including unfinished date or amount controls.
  for (const row of parseCouponPayload(state)) appendRow(row.date, row.amount);

  // Enable editing without rewriting the original payload during initialization.
  editor.querySelector("[data-coupon-add]").addEventListener("click", () => {
    const row = appendRow();
    publish();
    row.querySelector("input").focus();
  });
  editor.querySelector("[data-coupon-sort]").addEventListener("click", () => {
    // Move existing rows so dates, exact amounts and any input validation stay together.
    const ordered = Array.from(list.children).sort((left, right) => {
      const a = left.querySelector("[data-coupon-date]").value || "9999-99-99";
      const b = right.querySelector("[data-coupon-date]").value || "9999-99-99";
      return a.localeCompare(b);
    });
    list.append(...ordered);
    publish();
  });

  // Expose structured rows directly to other components without serializing between them.
  return { getCoupons: readRows };
}
