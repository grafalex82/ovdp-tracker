"use strict";

const currencySelect = document.getElementById("currency");

// Refresh the current portfolio view as soon as the issue currency changes.
currencySelect.addEventListener("change", () => currencySelect.form.requestSubmit());
