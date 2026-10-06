// The overview: how the project stands, a run under way, and what is in Snowflake.

import { $, element, icon } from "../dom.js";
import { count, percent, plural, relative, timeNode, titled } from "../format.js";
import { ENTITY_HELP } from "../labels.js";
import { activeRun, projectRuns, store } from "../store.js";
import { totals } from "./runs.js";

const MINUTE = 60000;

function isActive(run) {
  return run.status === "queued" || run.status === "running";
}

function latest(times) {
  return times.filter(Boolean).sort().pop() || null;
}

// Synced on schedule: no further behind than two intervals and a little slack.
function onSchedule(watermark) {
  const schedule = store.schedule;
  if (!schedule || !schedule.enabled) return true;
  return Date.now() - new Date(watermark) < (schedule.every_minutes * 2 + 5) * MINUTE;
}

// ---- How the project stands -------------------------------------------------

function health() {
  const active = activeRun();
  if (active && active.project === store.project) {
    const doing = active.kind === "reconcile" ? "Reconciling" : "Syncing";
    const read = totals(active).read;
    return {
      tone: "busy",
      drawing: "sync",
      title: active.status === "queued" ? `${doing} is about to start` : `${doing}…`,
      detail: read ? `${plural(read, "record")} read so far.` : "Reading from Langfuse.",
    };
  }
  if (store.stateError) {
    return { tone: "danger", drawing: "alert", title: "Snowflake could not be read", detail: store.stateError };
  }
  const last = projectRuns().find((run) => !isActive(run));
  if (last && last.status === "failed") {
    return {
      tone: "danger",
      drawing: "alert",
      title: `The last ${last.kind} failed`,
      detail: last.error || "",
      when: last.finished_at,
    };
  }
  if (last && last.status === "interrupted") {
    return {
      tone: "warn",
      drawing: "stop",
      title: `The last ${last.kind} was interrupted`,
      detail: "The service stopped before it finished. The next sync picks up where it left off.",
    };
  }
  if (!store.state.length) {
    const days = store.config.values.initial_backfill_days;
    return {
      tone: "none",
      drawing: "empty",
      title: "Nothing synced yet",
      detail: `The first sync creates the tables and loads the last ${plural(days, "day")}.`,
    };
  }
  // The table furthest behind decides.
  const synced = store.state.map((entry) => entry.watermark).sort()[0];
  const schedule = store.schedule;
  if (!schedule || !schedule.enabled) {
    return {
      tone: "ok",
      drawing: "check",
      title: `Synced ${relative(synced)}`,
      detail: "No schedule is set: syncs start from here or from an outside scheduler.",
    };
  }
  if (!onSchedule(synced)) {
    return {
      tone: "warn",
      drawing: "clock",
      title: "Behind schedule",
      detail: `Synced ${relative(synced)}; a sync is due every ${plural(schedule.every_minutes, "minute")}.`,
    };
  }
  return { tone: "ok", drawing: "check", title: "Up to date", detail: `Synced ${relative(synced)}.` };
}

function renderHealth() {
  const { tone, drawing, title, detail, when } = health();
  $("health-mark").className = `health ${tone}`;
  $("health-icon").setAttribute("href", `#i-${drawing}`);
  $("health-title").textContent = title;
  const parts = [detail];
  const active = activeRun();
  if (active && active.project !== store.project) {
    parts.push(`A run of ${active.project} is in progress; the next one can start when it ends.`);
  }
  const line = $("health-detail");
  line.replaceChildren(parts.filter(Boolean).join(" "));
  if (when) line.append(" ", timeNode(when));

  $("sync-now").disabled = Boolean(active);
  $("reconcile-now").disabled = Boolean(active);
}

// ---- A run under way --------------------------------------------------------

function liveRow(name, standing, text) {
  const marks = { done: icon("tick"), reading: element("span", { class: "spinner" }), waiting: icon("empty") };
  return element("li", { class: standing }, [
    marks[standing],
    element("span", { class: "name", text: titled(name) }),
    element("span", { class: "counts", text }),
  ]);
}

function renderLive() {
  const active = activeRun();
  const live = Boolean(active) && active.project === store.project && active.status === "running";
  $("live").hidden = !live;
  if (!live) return;

  const results = (active.result && active.result.entities) || [];
  const planned = active.request.entities || store.entities.filter((entity) => entity.kind === "table").map((entity) => entity.name);
  const waiting = planned.filter((name) => !results.some((entity) => entity.entity === name));
  let reading = false;
  const rows = results.map((entity, index) => {
    // An entity is listed when it starts, except the ones read in one go,
    // which are listed when they are done.
    const under = index === results.length - 1 && !entity.snapshot && !entity.unavailable;
    const again = entity.reconcile || {};
    const read = entity.rows_fetched + (again.rows_compared || 0);
    const text = entity.unavailable
      ? "Not available from this project"
      : `${count(read)} read · ${count(entity.rows_inserted + (again.rows_inserted || 0))} new · ${count(entity.rows_updated + (again.rows_updated || 0))} updated`;
    reading = reading || under;
    return liveRow(entity.entity, under ? "reading" : "done", text);
  });
  waiting.forEach((name, index) => {
    const next = !reading && index === 0;
    rows.push(liveRow(name, next ? "reading" : "waiting", next ? "Reading" : "Waiting"));
  });
  $("live-rows").replaceChildren(...rows);
}

// ---- The figures ------------------------------------------------------------

function put(id, content) {
  $(id).replaceChildren(content);
}

function renderStats() {
  const { values } = store.config;
  const runs = projectRuns();

  const synced = runs.find((run) => run.kind === "sync" && run.status === "succeeded");
  const touched = latest(store.state.map((entry) => entry.updated_at));
  if (synced) {
    const total = totals(synced);
    put("stat-last", timeNode(synced.finished_at));
    put("stat-last-note", `${count(total.read)} read · ${count(total.added)} new · ${count(total.updated)} updated`);
  } else {
    put("stat-last", touched ? timeNode(touched) : "Never");
    put("stat-last-note", "");
  }

  const schedule = store.schedule;
  if (schedule && schedule.enabled) {
    const due = !schedule.next_run_at || new Date(schedule.next_run_at) - Date.now() < MINUTE;
    put("stat-next", due ? "Due now" : timeNode(schedule.next_run_at));
    put("stat-next-note", `Every ${plural(schedule.every_minutes, "minute")}`);
  } else {
    put("stat-next", "Not scheduled");
    put("stat-next-note", element("a", { href: "#settings/schedule", text: "Set a schedule" }));
  }

  const reconciled = latest(store.state.map((entry) => entry.reconciled_at));
  put("stat-reconciled", reconciled ? timeNode(reconciled) : "Not yet");
  put(
    "stat-reconciled-note",
    values.reconcile_every_hours
      ? `Every ${plural(values.reconcile_every_hours, "hour")}, the last ${plural(values.reconcile_days, "day")}`
      : "Only when asked for",
  );

  const tables = store.entities.filter((entity) => entity.kind === "table").length;
  const views = store.entities.length - tables;
  const narrowed = [];
  if (values.sample_rate < 1) narrowed.push(`${percent(values.sample_rate)} of traces`);
  if (values.filters.length) narrowed.push(plural(values.filters.length, "filter"));
  if (values.exclude_fields.length) narrowed.push(`${plural(values.exclude_fields.length, "field")} left out`);
  put("stat-scope", views ? `${plural(tables, "table")}, ${plural(views, "view")}` : plural(tables, "table"));
  put("stat-scope-note", narrowed.join(" · ") || "Every record, every field");
}

// ---- What is in Snowflake ---------------------------------------------------

function when(iso, tone) {
  return element("span", { class: "when" }, [element("span", { class: `dot ${tone}` }), timeNode(iso)]);
}

const SYNCED = "Synced up to";
const RECONCILED = "Reconciled";

// A cell of the table. On a narrow screen the heading is not above it, so it
// carries what it is about.
function cell(about, properties = {}, children = []) {
  return element("td", { "data-label": about, ...properties }, children);
}

// Words in place of a time. Across both columns when given no column of its own.
function quiet(text, about = "") {
  return about ? cell(about, { class: "quiet", text }) : element("td", { class: "quiet", colSpan: 2, text });
}

function objectRow(entity, entry, unavailable) {
  const row = element("tr", {}, [
    element("td", {}, [
      element("div", { class: "object" }, [
        icon(entity.kind === "view" ? "view" : "table"),
        element("div", {}, [
          element("span", { class: "mono", text: entity.object_name }),
          element("span", { class: "hint", text: ENTITY_HELP[entity.name] || "" }),
        ]),
      ]),
    ]),
  ]);
  if (entity.kind === "view") {
    row.append(quiet(`A view over ${entity.derived_from}: always current`));
  } else if (unavailable) {
    const refused = quiet("Not available from this project");
    refused.title = unavailable;
    row.append(refused);
  } else if (store.stateError) {
    row.append(quiet("Unknown"));
  } else {
    row.append(
      entry
        ? cell(SYNCED, {}, [when(entry.watermark, onSchedule(entry.watermark) ? "ok" : "warn")])
        : cell(SYNCED, { class: "quiet" }, [
            element("span", { class: "when" }, [element("span", { class: "dot none" }), "Not synced yet"]),
          ]),
    );
    if (entity.snapshot) row.append(quiet("Read in full on every sync", RECONCILED));
    else if (entry && entry.reconciled_at) row.append(cell(RECONCILED, {}, [timeNode(entry.reconciled_at)]));
    else row.append(quiet("Not yet", RECONCILED));
  }
  return row;
}

function renderObjects() {
  const { deployment } = store.config;
  $("objects-note").textContent = `${deployment.snowflake_database}.${deployment.snowflake_schema}`;
  const state = new Map(store.state.map((entry) => [entry.entity, entry]));
  // What Langfuse refused in the latest run that got as far as reading.
  const read = projectRuns().find((run) => run.result && run.result.entities.length);
  const refused = new Map(
    ((read && read.result.entities) || []).map((entity) => [entity.entity, entity.unavailable]),
  );
  $("state-rows").replaceChildren(
    ...store.entities.map((entity) => objectRow(entity, state.get(entity.name), refused.get(entity.name))),
  );
}

export function renderOverview() {
  if (!store.config) return;
  renderHealth();
  renderLive();
  renderStats();
  renderObjects();
}
