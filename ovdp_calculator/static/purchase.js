"use strict";

const couponDates = document.getElementById("coupon_dates");
const maturityDate = document.getElementById("maturity_date");
const maturityManual = document.getElementById("maturity_manual");

// Update automatic maturity from the latest coupon date until maturity is edited manually.
function updateMaturity() {
  if (maturityManual.value === "1") return;
  const dates = couponDates.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
  if (dates.some(value => !/^\d{4}-\d{2}-\d{2}$/.test(value))) return;
  maturityDate.value = dates.sort().at(-1) || "";
}

maturityDate.addEventListener("input", () => { maturityManual.value = "1"; });
couponDates.addEventListener("input", updateMaturity);
updateMaturity();
