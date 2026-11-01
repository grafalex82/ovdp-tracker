"use strict";

// Keep the unsaved editor intact while confirming deletion of the saved sale.
function initializeSaleDeletion() {
  const dialog = document.getElementById("delete-sale-dialog");
  const openButton = document.getElementById("delete-sale-open");
  if (!dialog || !openButton) return;
  openButton.addEventListener("click", () => dialog.showModal());
  document.getElementById("delete-sale-cancel").addEventListener("click", () => dialog.close());
  openButton.hidden = false;
}


initializeSaleDeletion();
