"use strict";

const form = document.querySelector("form");
const isin = document.getElementById("isin");
const mode = document.getElementById("issue_mode");
const loadedIsin = document.getElementById("loaded_isin");
const issueToken = document.getElementById("issue_token");
const manualFields = document.getElementById("manual-fields");
const schedule = document.getElementById("coupon_schedule");
const maturity = document.getElementById("maturity_date");
const status = document.getElementById("lookup-status");
const preview = document.getElementById("issue-preview");
const save = document.getElementById("save-purchase");
let sourceValues = JSON.parse(document.getElementById("initial-source").textContent);
let sourceIsin = sourceValues.isin || "";
let sourceExisting = mode.value === "existing";
let sourceToken = issueToken.value;
let sequence = 0;
let pending = null;
let timer = null;
let dirty = Boolean(document.querySelector(".error"));
let submitting = false;

// Read the explicit mode selector without changing either mode's draft.
function manualSelected() {
  return form.elements.entry_mode.value === "manual";
}


// Render exact source money as text without floating point calculations.
function displayMoney(value) {
  const [integer, fraction = ""] = String(value).split(".");
  return `${integer},${fraction.padEnd(2, "0")}`;
}


// Render a source ISO date in the same day-first format as the surrounding interface.
function displayDate(value) {
  return value ? String(value).split("-").reverse().join(".") : "—";
}


// Recount manual coupons and require a separate maturity only for an empty schedule.
function updateCalendar() {
  const rows = schedule.value.split(/\r?\n/).filter(row => row.trim());
  document.getElementById("coupon-count").value = String(rows.length);
  document.getElementById("manual-maturity").hidden = rows.length > 0;
  maturity.disabled = manualFields.disabled || rows.length > 0;
  maturity.required = !maturity.disabled;
}


// Present all source-owned values as plain text and all source payments as table rows.
function renderPreview() {
  const hasSource = sourceIsin && sourceIsin === isin.value.trim().toUpperCase();
  preview.hidden = !hasSource || (manualSelected() && mode.value !== "existing");
  if (!hasSource) return;
  for (const element of preview.querySelectorAll("[data-issue-field]")) {
    const name = element.dataset.issueField;
    const value = sourceValues[name];
    element.textContent = name === "nominal" ? displayMoney(value) : (value || "—");
    if (name.endsWith("_date")) element.textContent = displayDate(value);
  }
  const coupons = sourceValues.coupon_schedule.split(/\r?\n/).filter(row => row.trim());
  document.getElementById("source-coupon-count").textContent = String(coupons.length);
  const events = coupons.map(row => {
    const [day, amount] = row.trim().split(/\s+/);
    return [day, "Купон", amount];
  });
  events.push([sourceValues.maturity_date, "Погашение", sourceValues.redemption_amount]);
  const table = document.getElementById("source-payments");
  table.replaceChildren();
  for (const [day, kind, amount] of events) {
    const row = document.createElement("tr");
    for (const value of [displayDate(day), kind, `${displayMoney(amount)} ${sourceValues.currency}`]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    table.append(row);
  }
}


// Keep purchase fields independent of the chosen source and protect saved issue data.
function updateMode() {
  const manual = manualSelected();
  manualFields.hidden = !manual || mode.value === "existing";
  manualFields.disabled = manualFields.hidden;
  document.getElementById("nbu-controls").hidden = manual;
  save.disabled = mode.value === "pending";
  renderPreview();
  updateCalendar();
}


// Cancel pending lookups so late responses cannot switch the mode or replace another ISIN.
function cancelLookup() {
  sequence += 1;
  clearTimeout(timer);
  if (pending) pending.abort();
  pending = null;
  form.removeAttribute("aria-busy");
}


// Apply a complete preview without copying source data into the editable manual draft.
function applySource(data, requestedIsin) {
  sourceValues = data.values;
  sourceIsin = requestedIsin;
  sourceExisting = data.existing;
  sourceToken = data.issue_token || "";
  loadedIsin.value = requestedIsin;
  issueToken.value = sourceToken;
  mode.value = data.existing ? "existing" : "auto";
  status.textContent = data.warning || (data.existing ? "Показан сохранённый выпуск." : "Данные НБУ загружены.");
  const details = document.getElementById("saved-details");
  details.replaceChildren();
  details.hidden = !data.details_url;
  if (data.details_url) {
    const link = document.createElement("a");
    link.href = data.details_url;
    link.textContent = "Сохранённые общие реквизиты и календарь выпуска";
    details.append(link);
  }
  dirty = true;
}


// Load NBU data only in NBU mode; manual entry checks saved issues without contacting NBU.
async function loadIssue() {
  cancelLookup();
  const requestId = sequence;
  const requestedIsin = isin.value.trim().toUpperCase();
  const manual = manualSelected();
  if (!requestedIsin) return;
  if (!manual && !/^[A-Z]{2}[A-Z0-9]{9}[0-9]$/.test(requestedIsin)) {
    status.textContent = "Enter a complete 12-character ISIN, or select manual entry.";
    return;
  }

  // Disable saving only while a new NBU draft is unresolved.
  if (!manual) mode.value = "pending";
  updateMode();
  status.textContent = manual ? "Проверка сохранённого выпуска…" : "Загрузка данных НБУ…";
  form.setAttribute("aria-busy", "true");
  pending = new AbortController();
  try {
    const source = manual ? "&source=local" : "";
    const response = await fetch(`${form.dataset.lookupUrl}?isin=${encodeURIComponent(requestedIsin)}${source}`, {
      signal: pending.signal,
    });
    const data = await response.json();
    if (requestId !== sequence || requestedIsin !== isin.value.trim().toUpperCase()) return;
    if (!response.ok) throw new Error(data.error || "Could not load issue data.");
    if (manual && !data.existing) {
      mode.value = "manual";
      status.textContent = "Ручной ввод параметров выпуска.";
    } else {
      applySource(data, requestedIsin);
    }
  } catch (error) {
    if (requestId !== sequence || error.name === "AbortError") return;
    status.textContent = error.message || "Could not load issue data. Retry or select manual entry.";
  } finally {
    if (requestId === sequence) {
      pending = null;
      form.removeAttribute("aria-busy");
      updateMode();
    }
  }
}


// Reset only the preview when ISIN changes and keep the user's manual and purchase drafts.
function changeIsin() {
  cancelLookup();
  sourceIsin = "";
  sourceExisting = false;
  loadedIsin.value = "";
  issueToken.value = "";
  mode.value = manualSelected() ? "manual" : "pending";
  document.getElementById("saved-details").hidden = true;
  status.textContent = "";
  updateMode();
  timer = setTimeout(loadIssue, 300);
}


// Switch between independent mode drafts and reuse a current complete source preview.
function changeEntryMode() {
  cancelLookup();
  const hasSource = sourceIsin && sourceIsin === isin.value.trim().toUpperCase();
  mode.value = hasSource && sourceExisting ? "existing" : (manualSelected() ? "manual" : "pending");
  status.textContent = manualSelected() ? "Ручной ввод параметров выпуска." : "";
  if (mode.value === "existing") status.textContent = "Показан сохранённый выпуск.";
  if (hasSource && !manualSelected()) {
    mode.value = sourceExisting ? "existing" : "auto";
    loadedIsin.value = sourceIsin;
    issueToken.value = sourceToken;
    status.textContent = sourceExisting ? "Показан сохранённый выпуск." : "Данные НБУ загружены.";
  } else if (!hasSource && isin.value.trim()) {
    timer = setTimeout(loadIssue, 300);
  }
  dirty = true;
  updateMode();
}

isin.addEventListener("input", changeIsin);
schedule.addEventListener("input", updateCalendar);
for (const radio of form.querySelectorAll('[name="entry_mode"]')) radio.addEventListener("change", changeEntryMode);
document.getElementById("lookup-retry").addEventListener("click", loadIssue);
form.addEventListener("input", () => { dirty = true; });
form.addEventListener("submit", () => { submitting = true; });
window.addEventListener("beforeunload", event => {
  if (!dirty || submitting) return;
  event.preventDefault();
  event.returnValue = "";
});
updateMode();
