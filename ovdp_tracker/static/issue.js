"use strict";

// Initialize the shared calendar and count all coupon rows before dirty-form protection starts.
function initializeIssueEditor() {
  // Restore the editable date-and-amount pairs from the saved or rejected calendar.
  const [calendar] = initializeCouponEditors();
  const count = document.getElementById("coupon-count");
  count.value = String(calendar.getCoupons().length);

  // Keep the displayed count derived from the calendar without moving explicit maturity.
  document.querySelector("[data-coupon-editor]").addEventListener("couponschange", event => {
    count.value = String(event.detail.length);
  });
}

initializeIssueEditor();
