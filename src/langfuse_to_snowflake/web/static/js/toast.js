// Short messages that come and go at the corner of the page.

import { $, element, icon } from "./dom.js";

const ICONS = { done: "tick", problem: "alert" };

export function toast(message, kind = "done") {
  const node = element("div", { class: `toast ${kind}` }, [
    icon(ICONS[kind] || ICONS.done),
    element("span", { text: message }),
  ]);
  $("toasts").append(node);
  const lasts = kind === "problem" ? 8000 : 4000;
  setTimeout(() => node.classList.add("leaving"), lasts);
  setTimeout(() => node.remove(), lasts + 250);
}
