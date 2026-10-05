"use strict";

// Everything on the page comes from the HTTP API. Text from the API is always
// placed with textContent, never as HTML.

const KEY_STORAGE = "langfuse-to-snowflake-api-key";
const PROJECT_STORAGE = "langfuse-to-snowflake-project";

const ENTITY_HELP = {
  observations: "Spans, generations and events: model, tokens, cost and timings.",
  scores: "Evaluation and review scores.",
  traces: "One row per trace.",
  sessions: "One row per session.",
  comments: "Comments left on traces, observations, sessions and prompts.",
  annotation_queues: "The review queues.",
  annotation_queue_items: "What is queued for review, and whether it is done.",
};

const FIELD_GROUP_HELP = {
  core: "IDs, type and start and end time. Always loaded.",
  basic: "Name, level, environment, user and session.",
  time: "When the record was created and last changed.",
  io: "Inputs and outputs: the prompts and completions.",
  metadata: "Custom metadata.",
  model: "Model, parameters and prices.",
  usage: "Tokens and cost.",
  prompt: "The managed prompt and its version.",
  metrics: "Latency and time to first token.",
  trace_context: "Trace name, tags and release.",
};

const OPERATOR_LABELS = {
  "=": "is",
  "!=": "is not",
  "~": "contains",
  "!~": "does not contain",
  ">": "is greater than",
  ">=": "is at least",
  "<": "is less than",
  "<=": "is at most",
};

// Form control id for each numeric setting.
const NUMBER_INPUTS = {
  schedule_minutes: "schedule-minutes",
  reconcile_every_hours: "reconcile-every-hours",
  reconcile_days: "reconcile-days",
  initial_backfill_days: "initial-backfill-days",
  lookback_minutes: "lookback-minutes",
  window_hours: "window-hours",
};
const CHECKBOX_INPUTS = { check_deletions: "check-deletions", reconcile_full: "reconcile-full" };

const $ = (id) => document.getElementById(id);
let project = null;
let config = null;
let schema = null;
let pollTimer = null;
let lastActive = false;

function element(tag, properties = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(properties)) {
    if (name === "text") node.textContent = value;
    else if (name === "class") node.className = value;
    else if (name in node) node[name] = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) node.append(child);
  return node;
}

class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  const key = sessionStorage.getItem(KEY_STORAGE);
  if (key) headers["X-API-Key"] = key;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401) {
    showKeyForm(key ? "That key was not accepted." : "");
    throw new ApiError(401, "Access key needed");
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) throw new ApiError(response.status, payload && payload.detail);
  return payload;
}

// Every setting and every status belongs to one project.
function scoped(path) {
  return `${path}${path.includes("?") ? "&" : "?"}project=${encodeURIComponent(project)}`;
}

// ---- Access key ------------------------------------------------------------

function showKeyForm(message) {
  clearTimeout(pollTimer);
  $("app").hidden = true;
  $("key-form").hidden = false;
  $("forget-key").hidden = true;
  $("project-picker").hidden = true;
  $("deployment").textContent = "";
  $("key-error").textContent = message;
  $("key-error").hidden = !message;
  $("key-input").focus();
}

$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  sessionStorage.setItem(KEY_STORAGE, $("key-input").value);
  $("key-input").value = "";
  start();
});

$("forget-key").addEventListener("click", () => {
  sessionStorage.removeItem(KEY_STORAGE);
  showKeyForm("");
});

// ---- Formatting ------------------------------------------------------------

function formatTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function timeCell(iso, empty) {
  const cell = element("td", { text: iso ? formatTime(iso) : empty });
  if (iso) cell.title = iso;
  else cell.className = "empty";
  return cell;
}

function emptyRow(columns, text) {
  return element("tr", {}, [element("td", { colSpan: columns, class: "empty", text })]);
}

function entitySelect(selected, label) {
  const select = element("select", { "aria-label": label }, [
    element("option", { value: "", text: "All entities" }),
    ...config.choices.entities.map((name) => element("option", { value: name, text: name })),
  ]);
  select.value = selected || "";
  return select;
}

// ---- Status ----------------------------------------------------------------

async function loadStatus() {
  const [entities, schedule] = await Promise.all([api(scoped("/entities")), api(scoped("/schedule"))]);
  let state = [];
  let stateError = "";
  try {
    state = await api(scoped("/state"));
  } catch (error) {
    if (error.status === 401) throw error;
    stateError = `Sync state could not be read from Snowflake: ${error.message}`;
  }
  $("notice").textContent = stateError;
  $("notice").hidden = !stateError;

  const byEntity = new Map(state.map((entry) => [entry.entity, entry]));
  const rows = entities.entities.map((entity) => {
    const entry = byEntity.get(entity.name);
    const row = element("tr", {}, [element("td", { class: "mono", text: entity.object_name })]);
    if (entity.kind === "view") {
      row.append(
        element("td", { text: "View" }),
        element("td", { colSpan: 2, class: "empty", text: `Always current: derived from ${entity.derived_from}` }),
      );
    } else {
      row.append(
        element("td", { text: "Table" }),
        timeCell(entry && entry.watermark, "Not synced yet"),
        timeCell(entry && entry.reconciled_at, entry ? "Not yet" : ""),
      );
    }
    return row;
  });
  $("state-rows").replaceChildren(...rows);

  $("schedule-line").textContent = schedule.enabled
    ? `Syncs every ${schedule.every_minutes} minutes. Next one ${schedule.next_run_at ? "at " + formatTime(schedule.next_run_at) : "soon"}.`
    : "No schedule is set: syncs start from here or from an outside scheduler.";
}

// ---- Earlier versions of the settings ---------------------------------------

function describeChange(settings) {
  const names = Object.keys(settings);
  if (!names.length) return "Nothing: everything at the deployment's defaults";
  return names
    .map((name) => {
      const value = settings[name];
      const text = Array.isArray(value) ? value.join(", ") || "(none)" : String(value);
      return `${name.replaceAll("_", " ")}: ${text}`;
    })
    .join("; ");
}

async function loadHistory() {
  $("history-message").textContent = "";
  try {
    const changes = await api(scoped("/config/history"));
    const rows = changes.map((change) =>
      element("tr", {}, [
        timeCell(change.changed_at, ""),
        element("td", { text: describeChange(change.settings) }),
      ]),
    );
    $("history-rows").replaceChildren(
      ...(rows.length ? rows : [emptyRow(2, "Nothing has been changed for this project yet.")]),
    );
  } catch (error) {
    if (error.status !== 401) {
      $("history-message").textContent = `The earlier versions could not be read: ${error.message}`;
    }
  }
}

// ---- Runs ------------------------------------------------------------------

function summarize(run) {
  if (run.error) return run.error;
  if (run.status === "interrupted") return "The service stopped before this run finished.";
  if (!run.result) return run.status === "queued" ? "Waiting to start" : "Starting";
  const total = { fetched: 0, inserted: 0, updated: 0, rejected: 0, deleted: 0 };
  const unavailable = [];
  for (const entity of run.result.entities) {
    if (entity.unavailable) unavailable.push(entity.entity);
    total.fetched += entity.rows_fetched;
    total.inserted += entity.rows_inserted;
    total.updated += entity.rows_updated;
    total.rejected += entity.rows_rejected;
    total.deleted += entity.rows_deleted_upstream || 0;
    if (entity.reconcile) {
      total.fetched += entity.reconcile.rows_compared;
      total.inserted += entity.reconcile.rows_inserted;
      total.updated += entity.reconcile.rows_updated;
      total.rejected += entity.reconcile.rows_rejected;
      total.deleted += entity.reconcile.rows_deleted_upstream || 0;
    }
  }
  const parts = [`${total.fetched} read`, `${total.inserted} new`, `${total.updated} updated`];
  if (total.rejected) parts.push(`${total.rejected} rejected`);
  if (total.deleted) parts.push(`${total.deleted} deleted in Langfuse`);
  if (unavailable.length) parts.push(`not available: ${unavailable.join(", ")}`);
  return parts.join(", ");
}

async function loadRuns() {
  // Only one run is active at a time across all projects, so the buttons
  // follow every run, while the table shows this project's.
  const runs = await api("/runs");
  const rows = runs
    .filter((run) => run.project === project)
    .map((run) =>
      element("tr", {}, [
        timeCell(run.started_at || run.created_at, ""),
        element("td", { text: run.trigger === "schedule" ? `${run.kind} (scheduled)` : run.kind }),
        element("td", { class: `status ${run.status}`, text: run.status }),
        element("td", { text: summarize(run) }),
      ]),
    );
  $("run-rows").replaceChildren(
    ...(rows.length
      ? rows
      : [
          emptyRow(
            4,
            config && config.runs_kept
              ? "No runs of this project yet."
              : "No runs of this project since the service started.",
          ),
        ]),
  );
  const active = runs.find((run) => run.status === "queued" || run.status === "running");
  $("sync-now").disabled = Boolean(active);
  $("reconcile-now").disabled = Boolean(active);
  if (active && active.project !== project) {
    $("action-message").textContent = `A run of ${active.project} is in progress.`;
  }
  return Boolean(active);
}

async function poll() {
  clearTimeout(pollTimer);
  let active = false;
  try {
    active = await loadRuns();
    // A run that just finished has moved the watermarks.
    if (lastActive && !active) {
      $("action-message").textContent = "";
      await loadStatus();
    }
    lastActive = active;
  } catch (error) {
    if (error.status === 401) return;
  }
  pollTimer = setTimeout(poll, active ? 3000 : 15000);
}

async function startRun(path, label) {
  $("action-message").textContent = "";
  try {
    await api(path, { method: "POST", body: { project } });
    $("action-message").textContent = `${label} started.`;
  } catch (error) {
    if (error.status === 401) return;
    $("action-message").textContent =
      error.status === 409 ? "A run is already in progress." : `${label} could not start: ${error.message}`;
  }
  poll();
}

$("sync-now").addEventListener("click", () => startRun("/sync", "Sync"));
$("reconcile-now").addEventListener("click", () => startRun("/reconcile", "Reconcile"));

// ---- Filters ---------------------------------------------------------------

function fieldInput(entity, value, placeholder) {
  const field = element("input", { type: "text", value: value || "", placeholder, "aria-label": "Field" });
  const suggest = () => field.setAttribute("list", `fields-${entity.value || "all"}`);
  entity.addEventListener("change", suggest);
  suggest();
  return field;
}

function filterRow(filter) {
  const entity = entitySelect(filter.entity, "Applies to");
  const field = fieldInput(entity, filter.field, "field, e.g. environment");
  const operator = element(
    "select",
    { "aria-label": "Condition" },
    config.choices.operators.map((op) => element("option", { value: op, text: OPERATOR_LABELS[op] || op })),
  );
  operator.value = filter.op || "=";
  const value = element("input", {
    type: "text",
    value: filter.value || "",
    placeholder: "value",
    "aria-label": "Value",
  });
  const remove = element("button", { type: "button", class: "icon", text: "Remove" });
  const row = element("div", { class: "filter" }, [entity, field, operator, value, remove]);
  remove.addEventListener("click", () => row.remove());
  return row;
}

function readFilters() {
  const filters = [];
  for (const row of $("filters").children) {
    const [entity, field, operator, value] = row.querySelectorAll("select, input");
    if (!field.value.trim() && !value.value.trim()) continue;
    const scope = entity.value ? `${entity.value}:` : "";
    filters.push(`${scope}${field.value.trim()}${operator.value}${value.value.trim()}`);
  }
  return filters;
}

// ---- Fields to leave out ---------------------------------------------------

function exclusionRow(exclusion) {
  const entity = entitySelect(exclusion.entity, "Applies to");
  const field = fieldInput(entity, exclusion.path, "field, e.g. input or metadata.email");
  const remove = element("button", { type: "button", class: "icon", text: "Remove" });
  const row = element("div", { class: "exclusion" }, [entity, field, remove]);
  remove.addEventListener("click", () => {
    row.remove();
    renderSchema();
  });
  entity.addEventListener("change", renderSchema);
  field.addEventListener("change", renderSchema);
  return row;
}

function readExclusions() {
  const exclusions = [];
  for (const row of $("exclusions").children) {
    const [entity, field] = row.querySelectorAll("select, input");
    const path = field.value.trim();
    if (path) exclusions.push({ entity: entity.value, path, row });
  }
  return exclusions;
}

// How a field of an entity stands with the rows above: left out by its own
// row, left out because a field it is part of is, or loaded.
function exclusionOf(entity, path) {
  const applying = readExclusions().filter((item) => !item.entity || item.entity === entity);
  const own = applying.find((item) => item.path === path);
  if (own) return { excluded: true, row: own.row };
  const parent = applying.find((item) => path.startsWith(`${item.path}.`));
  return parent ? { excluded: true, parent: parent.path } : { excluded: false };
}

function setExcluded(entity, path, excluded) {
  const current = exclusionOf(entity, path);
  if (excluded && !current.excluded) $("exclusions").append(exclusionRow({ entity, path }));
  if (!excluded && current.row) current.row.remove();
  renderSchema();
}

function schemaTable(entity) {
  const rows = entity.fields.map((field) => {
    const standing = exclusionOf(entity.entity, field.path);
    const load = element("input", {
      type: "checkbox",
      checked: !standing.excluded,
      disabled: field.required || Boolean(standing.parent),
      "aria-label": `Load ${field.path}`,
    });
    load.addEventListener("change", () => setExcluded(entity.entity, field.path, !load.checked));
    let note = field.column ? `Column ${field.column}` : "";
    if (field.required) note = "Needed to load the record";
    if (standing.parent) note = `Left out with ${standing.parent}`;
    return element("tr", { class: standing.excluded ? "out" : "" }, [
      element("td", {}, [load]),
      element("td", { class: "mono", text: field.path }),
      element("td", { text: field.types.join(", ") || "not in this sample" }),
      element("td", { text: field.types.length ? `${Math.round(field.share * 100)}%` : "" }),
      element("td", { class: "empty", text: note }),
    ]);
  });
  const head = element("thead", {}, [
    element("tr", {}, ["Load", "Field", "Type", "Has a value in", ""].map((text) => element("th", { scope: "col", text }))),
  ]);
  return element("div", { class: "scroll" }, [element("table", {}, [head, element("tbody", {}, rows)])]);
}

function renderSchema() {
  if (!schema) return;
  const sections = schema.map((entity) => {
    const records = entity.sampled === 1 ? "record" : `${entity.sampled} records`;
    let summary = `${entity.entity}: ${entity.fields.length} fields in the newest ${records}`;
    if (entity.unavailable) summary = `${entity.entity}: not available from this project`;
    else if (!entity.sampled) summary = `${entity.entity}: no recent records to look at`;
    const open = document.querySelector(`#schema details[data-entity="${entity.entity}"]`);
    const details = element("details", { "data-entity": entity.entity, open: open ? open.open : false }, [
      element("summary", { text: summary }),
    ]);
    if (entity.fields.length) details.append(schemaTable(entity));
    return details;
  });
  $("schema").replaceChildren(...sections);
}

$("add-exclusion").addEventListener("click", () => {
  const row = exclusionRow({});
  $("exclusions").append(row);
  row.querySelector("input").focus();
});

$("look").addEventListener("click", async () => {
  $("look").disabled = true;
  $("look-message").textContent = "Reading the newest records from Langfuse…";
  try {
    schema = await api(scoped("/schema"));
    $("look-message").textContent = "Untick a field to leave it out, then save.";
    renderSchema();
    const first = document.querySelector("#schema details");
    if (first) first.open = true;
  } catch (error) {
    if (error.status !== 401) $("look-message").textContent = `Could not read from Langfuse: ${error.message}`;
  } finally {
    $("look").disabled = false;
  }
});

// ---- Configuration form ----------------------------------------------------

function checkbox(name, value, checked, help, disabled = false) {
  const input = element("input", { type: "checkbox", name, value, checked, disabled });
  const text = element("span", {}, [
    element("strong", { text: value }),
    element("span", { class: "hint", text: help || "" }),
  ]);
  return element("label", { class: "check" }, [input, text]);
}

function buildFieldSuggestions() {
  document.querySelectorAll("datalist").forEach((list) => list.remove());
  const everything = new Set();
  for (const [entity, fields] of Object.entries(config.choices.fields)) {
    fields.forEach((name) => everything.add(name));
    document.body.append(
      element("datalist", { id: `fields-${entity}` }, fields.map((name) => element("option", { value: name }))),
    );
  }
  document.body.append(
    element("datalist", { id: "fields-all" }, [...everything].sort().map((name) => element("option", { value: name }))),
  );
}

function renderConfig() {
  const { values, choices, deployment, overridden } = config;
  const onV4 = deployment.api_version === "v4";

  $("deployment").textContent =
    `${deployment.langfuse_host} to ${deployment.snowflake_database}.${deployment.snowflake_schema}`;
  $("persist-warning").hidden = config.persisted;
  $("runs-note").textContent = config.runs_kept
    ? "Kept in Snowflake, so they are still here after a restart."
    : "Kept until the service restarts.";
  // Shown closed; the list is read when it is opened.
  $("history").hidden = !config.has_history;
  if ($("history").open) loadHistory();

  $("entities").replaceChildren(
    ...choices.entities.map((name) => {
      const derived = onV4 && (name === "traces" || name === "sessions");
      const help = derived ? `${ENTITY_HELP[name]} A view over observations.` : ENTITY_HELP[name];
      return checkbox("entities", name, values.entities.includes(name), help);
    }),
  );

  buildFieldSuggestions();
  $("filters").replaceChildren(...config.filters.map(filterRow));
  $("exclusions").replaceChildren(...config.exclude_fields.map(exclusionRow));
  renderSchema();

  $("sample-rate").value = Number((values.sample_rate * 100).toFixed(4));

  // Field groups only exist on the v4 API.
  $("content-section").hidden = !onV4;
  $("observation-fields").replaceChildren(
    ...choices.observation_fields.map((name) =>
      checkbox(
        "observation_fields",
        name,
        name === "core" || values.observation_fields.includes(name),
        FIELD_GROUP_HELP[name],
        name === "core",
      ),
    ),
  );
  $("expand-metadata").value = values.expand_metadata.join(", ");

  for (const [name, id] of Object.entries(NUMBER_INPUTS)) $(id).value = values[name];
  for (const [name, id] of Object.entries(CHECKBOX_INPUTS)) $(id).checked = values[name];

  document.querySelectorAll("[data-changed]").forEach((tag) => {
    const name = tag.dataset.changed;
    tag.hidden = !overridden.includes(name);
    tag.title = `Deployment default: ${JSON.stringify(config.defaults[name])}`;
  });

  const facts = [
    ["Project", config.project],
    ["Langfuse", deployment.langfuse_host],
    ["Langfuse API", deployment.api_version],
    ["Snowflake account", deployment.snowflake_account],
    ["Database and schema", `${deployment.snowflake_database}.${deployment.snowflake_schema}`],
    ["Warehouse", deployment.snowflake_warehouse],
    ["User and role", [deployment.snowflake_user, deployment.snowflake_role].filter(Boolean).join(" / ")],
    ["Object prefix", deployment.table_prefix || "(none)"],
    ["Settings kept in", config.stored_in || "Nowhere: they last until a restart"],
    ["Run history kept in", config.runs_stored_in || "Memory: it starts empty after a restart"],
  ];
  $("deployment-list").replaceChildren(
    ...facts.flatMap(([term, text]) => [element("dt", { text: term }), element("dd", { text })]),
  );
}

function checkedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

function readForm() {
  const update = {
    entities: checkedValues("entities"),
    filters: readFilters(),
    exclude_fields: readExclusions().map((item) => (item.entity ? `${item.entity}:${item.path}` : item.path)),
    sample_rate: Number($("sample-rate").value) / 100,
    expand_metadata: $("expand-metadata").value.split(",").map((key) => key.trim()).filter(Boolean),
  };
  if (!$("content-section").hidden) update.observation_fields = checkedValues("observation_fields");
  for (const [name, id] of Object.entries(NUMBER_INPUTS)) update[name] = Number($(id).value);
  for (const [name, id] of Object.entries(CHECKBOX_INPUTS)) update[name] = $(id).checked;
  return update;
}

function showProblems(problems) {
  document.querySelectorAll("[data-error]").forEach((node) => {
    node.textContent = "";
    node.hidden = true;
  });
  for (const problem of problems) {
    // The API reports a setting by name; FastAPI's own checks report a path.
    const name = problem.field !== undefined ? problem.field : (problem.loc || []).slice(-1)[0];
    const target =
      document.querySelector(`[data-error="${CSS.escape(String(name || ""))}"]`) ||
      document.querySelector('[data-error=""]');
    target.textContent = [target.textContent, problem.message || problem.msg].filter(Boolean).join(" ");
    target.hidden = false;
  }
  const first = document.querySelector("[data-error]:not([hidden])");
  if (first) {
    const closed = first.closest("details");
    if (closed) closed.open = true;
    first.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

async function saveConfig(method, body, done) {
  $("save").disabled = true;
  $("reset").disabled = true;
  $("save-message").textContent = "";
  showProblems([]);
  try {
    config = await api(scoped("/config"), { method, body });
    renderConfig();
    $("save-message").textContent = done;
    await loadStatus();
  } catch (error) {
    if (error.status === 422 && Array.isArray(error.detail)) showProblems(error.detail);
    else if (error.status !== 401) showProblems([{ field: "", message: error.message }]);
  } finally {
    $("save").disabled = false;
    $("reset").disabled = false;
  }
}

$("config-form").addEventListener("submit", (event) => {
  event.preventDefault();
  saveConfig("PUT", readForm(), "Saved. It applies from the next run.");
});

$("reset").addEventListener("click", () => {
  if (window.confirm(`Put every setting of ${project} back to the deployment's defaults?`)) {
    saveConfig("DELETE", undefined, "Back to the deployment's defaults.");
  }
});

$("add-filter").addEventListener("click", () => {
  const row = filterRow({});
  $("filters").append(row);
  row.querySelector("input").focus();
});

// ---- Projects and start ----------------------------------------------------

async function showProject(name) {
  clearTimeout(pollTimer);
  project = name;
  sessionStorage.setItem(PROJECT_STORAGE, name);
  schema = null;
  lastActive = false;
  $("schema").replaceChildren();
  $("look-message").textContent = "";
  $("action-message").textContent = "";
  $("save-message").textContent = "";
  showProblems([]);
  try {
    config = await api(scoped("/config"));
    renderConfig();
    await loadStatus();
  } catch (error) {
    if (error.status === 401) return;
    $("notice").textContent = `${name} could not be loaded: ${error.message}`;
    $("notice").hidden = false;
  }
  poll();
}

$("project").addEventListener("change", () => showProject($("project").value));
$("history").addEventListener("toggle", () => {
  if ($("history").open) loadHistory();
});

async function start() {
  let projects;
  try {
    projects = await api("/projects");
  } catch (error) {
    if (error.status !== 401) {
      $("deployment").textContent = `The service could not be reached: ${error.message}`;
    }
    return;
  }
  $("key-form").hidden = true;
  $("app").hidden = false;
  $("forget-key").hidden = !sessionStorage.getItem(KEY_STORAGE);

  const names = projects.map((item) => item.name);
  $("project").replaceChildren(...names.map((name) => element("option", { value: name, text: name })));
  // With one project there is nothing to choose.
  $("project-picker").hidden = names.length < 2;
  const remembered = sessionStorage.getItem(PROJECT_STORAGE);
  const chosen = names.includes(remembered) ? remembered : names[0];
  $("project").value = chosen;
  await showProject(chosen);
}

start();
