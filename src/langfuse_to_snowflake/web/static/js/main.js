// The page: the access key, the project it is about, and keeping what it shows
// up to date. Everything on it comes from the HTTP API.

import { accessKey, api, onRefused } from "./api.js";
import { confirm } from "./dialog.js";
import { $, element } from "./dom.js";
import { refreshTimes } from "./format.js";
import { startRouter } from "./router.js";
import { activeRun, projectRuns, scoped, store } from "./store.js";
import { toast } from "./toast.js";
import { renderDeployment } from "./views/deployment.js";
import { renderOverview } from "./views/overview.js";
import { forgetRuns, renderRuns } from "./views/runs.js";
import { forgetSettings, hasUnsaved, initSettings, renderSettings } from "./views/settings/form.js";

const PROJECT_STORAGE = "langfuse-to-snowflake-project";

// How often runs are asked for: while one is under way, and otherwise.
const BUSY_MS = 1500;
const IDLE_MS = 15000;

let pollTimer = null;
// The newest run of the project that has ended, as last seen.
let lastEnded = null;

// ---- Access key --------------------------------------------------------------

function showGate(message) {
  clearTimeout(pollTimer);
  $("boot").hidden = true;
  $("shell").hidden = true;
  $("gate").hidden = false;
  $("key-error").textContent = message;
  $("key-error").hidden = !message;
  $("key-input").focus();
}

onRefused((hadKey) => showGate(hadKey ? "That key was not accepted." : ""));

$("key-form").addEventListener("submit", (event) => {
  event.preventDefault();
  accessKey.set($("key-input").value);
  $("key-input").value = "";
  start();
});

$("forget-key").addEventListener("click", () => {
  accessKey.forget();
  showGate("");
});

// ---- Status and runs ---------------------------------------------------------

async function loadStatus() {
  const [entities, schedule] = await Promise.all([api(scoped("/entities")), api(scoped("/schedule"))]);
  store.entities = entities.entities;
  store.schedule = schedule;
  try {
    store.state = await api(scoped("/state"));
    store.stateError = "";
  } catch (error) {
    if (error.status === 401) throw error;
    store.state = [];
    store.stateError = error.message;
  }
  renderOverview();
}

// A short run can start and end between two looks, so it is the newest ended
// run that is watched, not whether one happens to be under way.
function newestEnded() {
  const ended = projectRuns().find((run) => run.status !== "queued" && run.status !== "running");
  return ended ? ended.id : "";
}

async function poll() {
  clearTimeout(pollTimer);
  let active = false;
  try {
    store.runs = await api("/runs");
    active = Boolean(activeRun());
    // A run that ended has moved the watermarks, and the schedule with them.
    const ended = newestEnded();
    if (lastEnded !== null && ended !== lastEnded) await loadStatus();
    lastEnded = ended;
    $("runs-live").hidden = !active;
    renderRuns();
    renderOverview();
  } catch (error) {
    if (error.status === 401) return;
  }
  pollTimer = setTimeout(poll, active ? BUSY_MS : IDLE_MS);
}

async function startRun(path, label) {
  try {
    await api(path, { method: "POST", body: { project: store.project } });
    toast(`${label} started.`);
  } catch (error) {
    if (error.status === 401) return;
    const already = error.status === 409;
    toast(already ? "A run is already in progress." : `${label} could not start: ${error.message}`, "problem");
  }
  poll();
}

$("sync-now").addEventListener("click", () => startRun("/sync", "Sync"));
$("reconcile-now").addEventListener("click", () => startRun("/reconcile", "Reconcile"));

// ---- Projects ----------------------------------------------------------------

function renderRoute() {
  const { deployment } = store.config;
  $("route-source").textContent = deployment.langfuse_host.replace(/^https?:\/\//, "");
  $("route-target").textContent = `${deployment.snowflake_database}.${deployment.snowflake_schema}`;
}

async function showProject(name) {
  clearTimeout(pollTimer);
  store.project = name;
  sessionStorage.setItem(PROJECT_STORAGE, name);
  Object.assign(store, { config: null, entities: [], schedule: null, state: [], stateError: "" });
  lastEnded = null;
  forgetRuns();
  forgetSettings();
  $("notice").hidden = true;
  try {
    store.config = await api(scoped("/config"));
    renderRoute();
    renderSettings();
    renderDeployment();
    await loadStatus();
  } catch (error) {
    if (error.status === 401) return;
    $("notice").textContent = `${name} could not be loaded: ${error.message}`;
    $("notice").hidden = false;
  }
  poll();
}

$("project").addEventListener("change", async () => {
  const chosen = $("project").value;
  if (hasUnsaved()) {
    const agreed = await confirm({
      title: "Discard unsaved changes?",
      body: `The changes to the settings of ${store.project} that were not saved will be lost.`,
      action: "Discard",
      danger: true,
    });
    if (!agreed) {
      $("project").value = store.project;
      return;
    }
  }
  showProject(chosen);
});

// ---- Start -------------------------------------------------------------------

async function start() {
  let projects;
  try {
    projects = await api("/projects");
  } catch (error) {
    if (error.status !== 401) {
      $("boot").hidden = false;
      $("boot").textContent = `The service could not be reached: ${error.message}`;
    }
    return;
  }
  $("boot").hidden = true;
  $("gate").hidden = true;
  $("shell").hidden = false;
  $("forget-key").hidden = !accessKey.get();

  const names = projects.map((item) => item.name);
  $("project").replaceChildren(...names.map((name) => element("option", { value: name, text: name })));
  // With one project there is nothing to choose.
  $("project-picker").hidden = names.length < 2;
  const remembered = sessionStorage.getItem(PROJECT_STORAGE);
  const chosen = names.includes(remembered) ? remembered : names[0];
  $("project").value = chosen;
  await showProject(chosen);
}

initSettings({
  onSaved: () => {
    renderRoute();
    renderDeployment();
    loadStatus().catch(() => {});
  },
});
startRouter();
window.addEventListener("hashchange", renderRuns);
// "5 minutes ago" does not stay true.
setInterval(() => {
  refreshTimes();
  renderOverview();
}, 20000);
start();
