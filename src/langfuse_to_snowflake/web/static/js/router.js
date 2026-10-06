// Which view is shown follows the address: #overview, #settings/schedule, ...

const VIEWS = ["overview", "settings", "runs", "deployment"];
let shown = null;

export function route() {
  const [view, section] = window.location.hash.slice(1).split("/");
  return { view: VIEWS.includes(view) ? view : VIEWS[0], section: section || "" };
}

export function applyRoute() {
  const { view, section } = route();
  for (const name of VIEWS) {
    document.getElementById(`view-${name}`).hidden = name !== view;
  }
  document.querySelectorAll(".tabs a").forEach((link) => {
    if (link.dataset.view === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  const target = section && document.getElementById(`section-${section}`);
  if (target) {
    if (target.tagName === "DETAILS") target.open = true;
    target.scrollIntoView({ block: "start" });
  } else if (view !== shown) {
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  shown = view;
}

export function startRouter() {
  window.addEventListener("hashchange", applyRoute);
  applyRoute();
}
