// Fields to leave out: the list itself, and a browser over the fields the
// project's newest records actually have.

import { api } from "../../api.js";
import { $, element } from "../../dom.js";
import { plural, titled } from "../../format.js";
import { scoped, store } from "../../store.js";
import { entitySelect, fieldInput, removeButton } from "./filters.js";

// What "Browse recent data" read from Langfuse, and which entity of it is shown.
let schema = null;
let shown = null;
let changed = () => {};

export function initFields(onChange) {
  changed = onChange;
  $("add-exclusion").addEventListener("click", () => {
    const row = exclusionRow({});
    $("exclusions").append(row);
    row.querySelector("input").focus();
    changed();
  });
  $("look").addEventListener("click", browse);
  $("explorer-search").addEventListener("input", renderExplorer);
}

// ---- The list ---------------------------------------------------------------

function exclusionRow(exclusion) {
  const entity = entitySelect(exclusion.entity, "Applies to");
  const field = fieldInput(entity, exclusion.path, "field, e.g. input or metadata.email");
  const remove = removeButton("Load this field again");
  const row = element("div", { class: "exclusion" }, [entity, field, remove]);
  remove.addEventListener("click", () => {
    row.remove();
    renderExplorer();
    changed();
  });
  entity.addEventListener("change", renderExplorer);
  field.addEventListener("change", renderExplorer);
  return row;
}

export function renderExclusions() {
  $("exclusions").replaceChildren(...store.config.exclude_fields.map(exclusionRow));
  renderExplorer();
}

function exclusions() {
  const found = [];
  for (const row of $("exclusions").children) {
    const [entity, field] = row.querySelectorAll("select, input");
    const path = field.value.trim();
    if (path) found.push({ entity: entity.value, path, row });
  }
  return found;
}

// As the API takes them: [entity:]path.
export function readExclusions() {
  return exclusions().map((item) => (item.entity ? `${item.entity}:${item.path}` : item.path));
}

// ---- The browser ------------------------------------------------------------

// How a field of an entity stands with the list above: left out by its own
// row, left out because a field it is part of is, or loaded.
function standingOf(entity, path) {
  const applying = exclusions().filter((item) => !item.entity || item.entity === entity);
  const own = applying.find((item) => item.path === path);
  if (own) return { excluded: true, row: own.row };
  const parent = applying.find((item) => path.startsWith(`${item.path}.`));
  return parent ? { excluded: true, parent: parent.path } : { excluded: false };
}

function setExcluded(entity, path, excluded) {
  const standing = standingOf(entity, path);
  if (excluded && !standing.excluded) $("exclusions").append(exclusionRow({ entity, path }));
  if (!excluded && standing.row) standing.row.remove();
  renderExplorer();
  changed();
}

function fieldRow(entity, field) {
  const standing = standingOf(entity.entity, field.path);
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

  const share = field.types.length
    ? element("span", { class: "share" }, [
        element("progress", { max: 1, value: field.share }),
        `${Math.round(field.share * 100)}%`,
      ])
    : "";
  const classes = [standing.excluded ? "out" : "", field.path.includes(".") ? "nested" : ""];
  return element("tr", { class: classes.join(" ").trim() }, [
    element("td", {}, [load]),
    element("td", {}, [element("span", { class: "path", text: field.path })]),
    element("td", { class: "quiet", text: field.types.join(", ") || "not in this sample" }),
    element("td", {}, [share]),
    element("td", { class: "quiet", text: note }),
  ]);
}

function renderExplorer() {
  $("explorer").hidden = !schema;
  if (!schema) return;
  if (!schema.some((entity) => entity.entity === shown)) shown = schema[0] ? schema[0].entity : null;

  $("explorer-tabs").replaceChildren(
    ...schema.map((entity) => {
      const tab = element("button", { type: "button", "aria-pressed": String(entity.entity === shown) }, [
        titled(entity.entity),
        element("span", { class: "count", text: String(entity.fields.length) }),
      ]);
      tab.addEventListener("click", () => {
        shown = entity.entity;
        renderExplorer();
      });
      return tab;
    }),
  );

  const entity = schema.find((item) => item.entity === shown);
  const wanted = $("explorer-search").value.trim().toLowerCase();
  const fields = entity ? entity.fields.filter((field) => field.path.toLowerCase().includes(wanted)) : [];

  let note = "";
  if (!entity) note = "There is nothing to look at.";
  else if (entity.unavailable) note = "Langfuse does not offer this for the project.";
  else if (!entity.sampled) note = "No records in the last few days to look at.";
  else if (!fields.length) note = "No field matches.";
  else {
    const records = entity.sampled === 1 ? "record" : `${entity.sampled} records`;
    note = `${plural(entity.fields.length, "field")} found in the newest ${records}. Untick one to leave it out, then save.`;
  }
  $("explorer-note").textContent = note;
  $("explorer-table").hidden = !fields.length;
  $("explorer-rows").replaceChildren(...fields.map((field) => fieldRow(entity, field)));
}

async function browse() {
  $("look").disabled = true;
  $("look-message").textContent = "Reading the newest records from Langfuse…";
  try {
    schema = await api(scoped("/schema"));
    $("look-message").textContent = "";
    renderExplorer();
  } catch (error) {
    if (error.status !== 401) $("look-message").textContent = `Could not read from Langfuse: ${error.message}`;
  } finally {
    $("look").disabled = false;
  }
}

// Another project logs other fields.
export function forgetSchema() {
  schema = null;
  shown = null;
  $("look-message").textContent = "";
  $("explorer-search").value = "";
  renderExplorer();
}
