// The settings form: drawing a project's settings, noticing what was changed,
// and saving it.

import { api } from "../../api.js";
import { confirm } from "../../dialog.js";
import { $, element } from "../../dom.js";
import { titled } from "../../format.js";
import { describeSetting, ENTITY_HELP, FIELD_GROUP_HELP, FIELD_GROUP_NAMES } from "../../labels.js";
import { scoped, store } from "../../store.js";
import { toast } from "../../toast.js";
import { forgetSchema, initFields, readExclusions, renderExclusions } from "./fields.js";
import { addFilter, buildFieldSuggestions, readFilters, renderFilters } from "./filters.js";
import { renderHistory } from "./history.js";

// Form control id for each numeric setting.
const NUMBER_INPUTS = {
  schedule_minutes: "schedule-minutes",
  reconcile_every_hours: "reconcile-every-hours",
  reconcile_days: "reconcile-days",
  initial_backfill_days: "initial-backfill-days",
  lookback_minutes: "lookback-minutes",
  window_hours: "window-hours",
};
// And for each one that is on or off.
const CHECKBOX_INPUTS = {
  check_deletions: "check-deletions",
  reconcile_full: "reconcile-full",
};

// The form as it stood when it was last drawn from the saved settings.
let saved = {};
let afterSaving = () => {};

// ---- Drawing ----------------------------------------------------------------

function option(group, value, label, help, checked, { disabled = false, pill = "" } = {}) {
  return element("label", { class: "option" }, [
    element("input", { type: "checkbox", name: group, value, checked, disabled }),
    element("span", { class: "option-name" }, [label, pill ? element("span", { class: "pill", text: pill }) : null]),
    element("span", { class: "hint", text: help || "" }),
  ]);
}

export function renderSettings() {
  const { values, choices, deployment, overridden, defaults } = store.config;
  const onV4 = deployment.api_version === "v4";

  $("settings-project").textContent = store.config.project;
  $("persist-warning").hidden = store.config.persisted;

  $("entities").replaceChildren(
    ...choices.entities.map((name) => {
      // On the v4 API these two are not read from Langfuse but derived in Snowflake.
      const derived = onV4 && (name === "traces" || name === "sessions");
      return option("entities", name, titled(name), ENTITY_HELP[name], values.entities.includes(name), {
        pill: derived ? "View over observations" : "",
      });
    }),
  );

  buildFieldSuggestions();
  renderFilters(touched);
  renderExclusions();

  const share = Number((values.sample_rate * 100).toFixed(4));
  $("sample-rate").value = share;
  $("sample-slider").value = Math.max(1, Math.round(share));

  // Field groups only exist on the v4 API.
  $("section-content").hidden = !onV4;
  $("content-link").hidden = !onV4;
  $("observation-fields").replaceChildren(
    ...choices.observation_fields.map((name) =>
      option(
        "observation_fields",
        name,
        FIELD_GROUP_NAMES[name] || titled(name),
        FIELD_GROUP_HELP[name],
        name === "core" || values.observation_fields.includes(name),
        { disabled: name === "core" },
      ),
    ),
  );
  $("expand-metadata").value = values.expand_metadata.join(", ");

  for (const [name, id] of Object.entries(NUMBER_INPUTS)) $(id).value = values[name];
  for (const [name, id] of Object.entries(CHECKBOX_INPUTS)) $(id).checked = values[name];

  document.querySelectorAll("[data-changed]").forEach((tag) => {
    const name = tag.dataset.changed;
    tag.hidden = !overridden.includes(name);
    tag.title = `Changed from the deployment's default: ${describeSetting(name, defaults[name])[1]}`;
  });

  showProblems([]);
  saved = snapshot();
  touched();
  renderHistory();
}

// ---- Reading ----------------------------------------------------------------

function checkedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map((input) => input.value);
}

function readForm() {
  const update = {
    entities: checkedValues("entities"),
    filters: readFilters(),
    exclude_fields: readExclusions(),
    sample_rate: Number($("sample-rate").value) / 100,
    expand_metadata: $("expand-metadata").value.split(",").map((key) => key.trim()).filter(Boolean),
  };
  if (!$("section-content").hidden) update.observation_fields = checkedValues("observation_fields");
  for (const [name, id] of Object.entries(NUMBER_INPUTS)) update[name] = Number($(id).value);
  for (const [name, id] of Object.entries(CHECKBOX_INPUTS)) update[name] = $(id).checked;
  return update;
}

// ---- What has been changed and not saved -------------------------------------

function snapshot() {
  return Object.fromEntries(Object.entries(readForm()).map(([name, value]) => [name, JSON.stringify(value)]));
}

function unsaved() {
  const now = snapshot();
  return Object.keys(now).filter((name) => now[name] !== saved[name]);
}

export function hasUnsaved() {
  return Boolean(store.config) && unsaved().length > 0;
}

// Called whenever something in the form may have changed.
function touched() {
  $("filters-empty").hidden = $("filters").children.length > 0;
  $("exclusions-empty").hidden = $("exclusions").children.length > 0;
  const names = unsaved();
  $("savebar").hidden = !names.length;
  $("unsaved-dot").hidden = !names.length;
  $("save-message").textContent = names.length === 1 ? "1 unsaved change" : `${names.length} unsaved changes`;
}

// ---- Saving -----------------------------------------------------------------

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

async function save(method, body, done) {
  const buttons = [$("save"), $("discard"), $("reset")];
  buttons.forEach((button) => (button.disabled = true));
  showProblems([]);
  try {
    store.config = await api(scoped("/config"), { method, body });
    renderSettings();
    toast(done);
    afterSaving();
  } catch (error) {
    if (error.status === 422 && Array.isArray(error.detail)) showProblems(error.detail);
    else if (error.status !== 401) showProblems([{ field: "", message: error.message }]);
  } finally {
    buttons.forEach((button) => (button.disabled = false));
    $("reset").disabled = !store.config.overridden.length;
  }
}

async function reset() {
  const agreed = await confirm({
    title: "Reset to defaults?",
    body: `Every setting of ${store.project} goes back to the deployment's default, from the next run.`,
    action: "Reset",
    danger: true,
  });
  if (agreed) save("DELETE", undefined, "Back to the deployment's defaults.");
}

// ---- The list of sections at the side ---------------------------------------

function followSections() {
  const links = [...document.querySelectorAll("#settings-nav a")];
  const mark = () => {
    const visible = links.filter((link) => !link.hidden);
    let current = visible[0];
    for (const link of visible) {
      const section = document.getElementById(`section-${link.hash.split("/")[1]}`);
      if (section && section.getBoundingClientRect().top <= window.innerHeight * 0.35) current = link;
    }
    // The last sections are too short to ever reach the top of the window.
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) {
      current = visible[visible.length - 1];
    }
    links.forEach((link) => link.classList.toggle("current", link === current));
  };
  let waiting = false;
  const later = () => {
    if (waiting) return;
    waiting = true;
    requestAnimationFrame(() => {
      waiting = false;
      mark();
    });
  };
  window.addEventListener("scroll", later, { passive: true });
  window.addEventListener("hashchange", later);
  mark();
}

// ---- Start ------------------------------------------------------------------

export function initSettings({ onSaved }) {
  afterSaving = onSaved;
  initFields(touched);

  const form = $("config-form");
  form.addEventListener("input", touched);
  form.addEventListener("change", touched);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    save("PUT", readForm(), "Saved. It applies from the next run.");
  });

  $("sample-slider").addEventListener("input", () => {
    $("sample-rate").value = $("sample-slider").value;
  });
  $("sample-rate").addEventListener("input", () => {
    $("sample-slider").value = Math.min(100, Math.max(1, Math.round(Number($("sample-rate").value) || 1)));
  });

  $("add-filter").addEventListener("click", () => addFilter(touched));
  $("discard").addEventListener("click", renderSettings);
  $("reset").addEventListener("click", reset);

  window.addEventListener("beforeunload", (event) => {
    if (hasUnsaved()) event.preventDefault();
  });
  followSections();
}

// Another project: nothing looked up for the last one applies.
export function forgetSettings() {
  forgetSchema();
  showProblems([]);
}
