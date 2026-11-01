"use strict";

// Initialize shared protection after each page's own form setup has completed.
function protectUnsavedForms() {
  // Remember original values across rejected submissions without storing a draft in the database.
  const forms = Array.from(document.querySelectorAll("form[data-unsaved-form]"));
  if (!forms.length) return;
  const baselines = new Map();
  for (const form of forms) {
    const field = form.elements._unsaved_baseline;
    const initial = field.value || (form.dataset.unsavedError === "true" ? null : snapshotForm(form));
    baselines.set(form, initial);
    field.value = initial || "";
  }
  const dialog = document.getElementById("unsaved-dialog");
  let departing = false;
  let destination = null;

  // Compare current values, including disabled drafts and dynamically added calendar controls.
  function hasChanges() {
    return forms.some(form => snapshotForm(form) !== baselines.get(form));
  }


  // Intercept links that replace this document, including tabs, breadcrumbs and cancel actions.
  function confirmLink(event) {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey) return;
    if (event.shiftKey || event.altKey) return;
    const link = event.target.closest("a[href]");
    if (!link || link.hasAttribute("download") || (link.target && link.target !== "_self")) return;
    const target = new URL(link.href, location.href);
    if (!["http:", "https:"].includes(target.protocol)) return;
    if (target.origin === location.origin && target.pathname === location.pathname &&
        target.search === location.search && target.hash) return;
    if (!hasChanges()) return;
    event.preventDefault();
    destination = target.href;
    if (!dialog.open) dialog.showModal();
  }


  // Leave once after an explicit confirmation without triggering the native warning as well.
  function leaveWithoutSaving() {
    departing = true;
    dialog.close();
    location.assign(destination);
  }


  // Request the browser's own confirmation for reload, close and history navigation.
  function confirmBrowserLeave(event) {
    if (departing || !hasChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  }


  // Suppress unload warnings only for an actual, uncancelled submission in the current tab.
  function submitForm(event) {
    queueMicrotask(() => {
      const target = event.submitter?.formTarget || event.target.target;
      departing = !event.defaultPrevented && (!target || target === "_self");
    });
  }

  // Keep the original baseline when the browser restores a document from its back-forward cache.
  document.addEventListener("click", confirmLink);
  document.getElementById("unsaved-stay").addEventListener("click", () => dialog.close());
  document.getElementById("unsaved-leave").addEventListener("click", leaveWithoutSaving);
  window.addEventListener("beforeunload", confirmBrowserLeave);
  window.addEventListener("pageshow", () => { departing = false; });
  for (const form of forms) form.addEventListener("submit", submitForm);
}


// Capture values rather than input events so restoring the original form removes the warning.
function snapshotForm(form) {
  return JSON.stringify(Array.from(form.elements)
    .filter(element => !element.hasAttribute("data-unsaved-ignore"))
    .filter(element => element.matches("input, select, textarea"))
    .filter(element => element.name || element.hasAttribute("data-unsaved-value"))
    .map(element => {
      let value = element.value;
      if (["checkbox", "radio"].includes(element.type)) value = [value, element.checked];
      if (element.type === "select-multiple") value = Array.from(element.selectedOptions, option => option.value);
      return [element.name || element.dataset.unsavedValue, value];
    }));
}

protectUnsavedForms();
