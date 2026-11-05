"use strict";

// Preserve the editor draft while the user reviews deletion of the saved receipt.
function initializePaymentDeletion() {
  const dialog = document.getElementById("delete-payment-dialog");
  const openButton = document.getElementById("delete-payment-open");
  if (!dialog || !openButton) return;
  openButton.addEventListener("click", () => dialog.showModal());
  document.getElementById("delete-payment-cancel").addEventListener("click", () => dialog.close());
  openButton.hidden = false;
}


initializePaymentDeletion();
