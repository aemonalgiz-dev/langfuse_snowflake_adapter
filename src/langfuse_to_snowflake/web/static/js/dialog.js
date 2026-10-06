// Asking before something that cannot be taken back.

import { $ } from "./dom.js";

export function confirm({ title, body, action, danger = false }) {
  const dialog = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  const accept = $("confirm-accept");
  accept.textContent = action;
  accept.className = danger ? "danger" : "primary";
  return new Promise((resolve) => {
    dialog.returnValue = "";
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), { once: true });
    dialog.showModal();
  });
}
