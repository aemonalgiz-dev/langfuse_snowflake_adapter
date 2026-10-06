// Runs: the short list on the overview and the full one, each run opening to
// what it did entity by entity.

import { $, element, icon } from "../dom.js";
import { absolute, count, percent, plural, range, timeNode, titled, took } from "../format.js";
import { route } from "../router.js";
import { projectRuns, store } from "../store.js";

const LATEST = 5;

// For each status: what to call it and which drawing goes with it.
const STATUS = {
  queued: ["Queued", "clock"],
  running: ["Running", null],
  succeeded: ["Succeeded", "check"],
  failed: ["Failed", "alert"],
  interrupted: ["Interrupted", "stop"],
};

// Runs the reader has opened, by ID, so that they stay open as the list refreshes.
const opened = new Set();
let linked = "";
let drawn = "";

// What a run read and changed, over all its entities.
export function totals(run) {
  const total = { read: 0, added: 0, updated: 0, rejected: 0, deleted: 0, unavailable: [] };
  for (const entity of (run.result && run.result.entities) || []) {
    if (entity.unavailable) total.unavailable.push(entity.entity);
    for (const part of [entity, entity.reconcile]) {
      if (!part) continue;
      total.read += part.rows_fetched || part.rows_compared || 0;
      total.added += part.rows_inserted;
      total.updated += part.rows_updated;
      total.rejected += part.rows_rejected;
      total.deleted += part.rows_deleted_upstream || 0;
    }
  }
  return total;
}

function summary(run) {
  if (run.error) return run.error;
  if (run.status === "interrupted") return "The service stopped before this run finished.";
  if (!run.result) return run.status === "queued" ? "Waiting to start" : "Starting";
  const total = totals(run);
  const parts = [`${count(total.read)} read`, `${count(total.added)} new`, `${count(total.updated)} updated`];
  if (total.rejected) parts.push(`${count(total.rejected)} rejected`);
  if (total.deleted) parts.push(`${count(total.deleted)} deleted in Langfuse`);
  if (total.unavailable.length) {
    parts.push(`not available: ${total.unavailable.join(", ").replaceAll("_", " ")}`);
  }
  return parts.join(" · ");
}

function badge(status) {
  const [label, drawing] = STATUS[status] || [titled(status), "stop"];
  const mark = drawing ? icon(drawing) : element("span", { class: "spinner" });
  return element("span", { class: `badge ${status}` }, [mark, label]);
}

// A row of the table: two cells of words, then counts. A count of nothing recedes.
function cells(values) {
  return values.map((value, index) => {
    if (index < 2) return element("td", { text: value });
    return element("td", { class: value ? "num" : "num zero", text: value === null ? "" : count(value) });
  });
}

function deleted(number) {
  return number ? ` · ${count(number)} deleted in Langfuse` : "";
}

function reconciled(again, label) {
  const how = again.mode === "full" ? "record by record" : "by listing";
  return cells([
    label,
    `Compared ${how}, ${range(again.window_start, again.window_end)}${deleted(again.rows_deleted_upstream)}`,
    again.rows_compared,
    null,
    again.rows_inserted,
    again.rows_updated,
    again.rows_rejected,
  ]);
}

function entityRows(entity) {
  const name = titled(entity.entity);
  const again = entity.reconcile;
  const read = entity.unavailable || entity.snapshot || entity.window_start;
  // A run that only reconciled read nothing new for the entity.
  if (!read && again) return [element("tr", {}, reconciled(again, name))];

  let words = "Nothing to read";
  if (entity.unavailable) words = "Not available from this project";
  else if (entity.snapshot) words = "Read in full";
  else if (entity.window_start) words = range(entity.window_start, entity.window_end);
  const row = element("tr", { class: again ? "has-sub" : "" }, cells([
    name,
    words + deleted(entity.rows_deleted_upstream),
    entity.rows_fetched,
    entity.rows_skipped,
    entity.rows_inserted,
    entity.rows_updated,
    entity.rows_rejected,
  ]));
  if (entity.unavailable) row.title = entity.unavailable;
  return again ? [row, element("tr", { class: "sub" }, reconciled(again, "Reconciled"))] : [row];
}

function entityTable(result) {
  const head = ["Data", "What was read", "Read", "Filtered out", "New", "Updated", "Rejected"].map((text, index) =>
    element("th", { scope: "col", class: index > 1 ? "num" : "", text }),
  );
  return element("div", { class: "inset" }, [
    element("div", { class: "scroll" }, [
      element("table", {}, [
        element("thead", {}, [element("tr", {}, head)]),
        element("tbody", {}, result.entities.flatMap(entityRows)),
      ]),
    ]),
  ]);
}

// What applied to the run: its own range and entities, and the selection in effect.
function notes(run) {
  const request = run.request || {};
  const result = run.result;
  const items = [`Started ${absolute(run.started_at || run.created_at)}`];
  if (run.finished_at) items.push(`Finished ${absolute(run.finished_at)}`);
  if (request.from) items.push(`Range ${range(request.from, request.to || run.created_at)}`);
  if (request.entities) items.push(`Only ${request.entities.join(", ")}`);
  if (result && result.sample_rate < 1) items.push(`${percent(result.sample_rate)} of traces`);
  if (result && result.filters.length) items.push(`Filters: ${result.filters.join(", ")}`);
  if (result && result.excluded_fields.length) items.push(`Left out: ${result.excluded_fields.join(", ")}`);
  items.push(`Run ${run.id}`);
  return element("p", { class: "run-meta" }, items.map((text) => element("span", { text })));
}

function details(run) {
  const body = element("div", { class: "run-body" });
  if (run.error) body.append(element("pre", { class: "trace", text: run.error }));
  if (run.result && run.result.entities.length) body.append(entityTable(run.result));
  body.append(notes(run));
  return body;
}

function runItem(run) {
  const open = opened.has(run.id);
  const head = element("button", { type: "button", class: "run-head", "aria-expanded": String(open) }, [
    badge(run.status),
    element("span", { class: "run-kind" }, [
      titled(run.kind),
      run.trigger === "schedule" ? element("span", { class: "muted", text: " · scheduled" }) : null,
    ]),
    timeNode(run.started_at || run.created_at, "run-when"),
    element("span", {
      class: "run-took num",
      text: took(run.started_at, run.finished_at || (run.status === "running" ? new Date() : null)),
    }),
    element("span", { class: `run-summary${run.error ? " problem" : ""}`, text: summary(run) }),
    icon("chevron"),
  ]);
  head.addEventListener("click", () => {
    if (!opened.delete(run.id)) opened.add(run.id);
    renderRuns();
  });
  const item = element("article", { class: `run${open ? " open" : ""}`, "data-run": run.id }, [head]);
  if (open) item.append(details(run));
  return item;
}

function fill(container, runs, nothing) {
  // Redrawing must not take the keyboard away from the run someone is on.
  const focused = container.contains(document.activeElement) && document.activeElement.closest("[data-run]");
  container.replaceChildren(
    ...(runs.length ? runs.map(runItem) : [element("p", { class: "empty", text: nothing })]),
  );
  if (focused) {
    const again = container.querySelector(`[data-run="${CSS.escape(focused.dataset.run)}"] .run-head`);
    if (again) again.focus();
  }
}

// A link to a run, #runs/<id>, shows it opened.
function openLinked() {
  const { view, section } = route();
  if (view === "runs" && section && section !== linked) opened.add(section);
  linked = section;
}

export function renderRuns() {
  openLinked();
  const runs = projectRuns();
  const kept = Boolean(store.config && store.config.runs_kept);
  const signature = JSON.stringify([runs, [...opened], kept]);
  if (signature === drawn) return;
  drawn = signature;

  const nothing = kept
    ? "No runs of this project yet."
    : "No runs of this project since the service started.";
  fill($("run-list"), runs, nothing);
  fill($("latest-runs"), runs.slice(0, LATEST), nothing);
  $("runs-note").textContent = kept
    ? "Every sync and reconcile of this project, newest first. They are kept in Snowflake, so they are still here after a restart."
    : "Every sync and reconcile of this project since the service started, newest first.";
}

// A new project, or new settings: draw again whatever was drawn before.
export function forgetRuns() {
  drawn = "";
}
