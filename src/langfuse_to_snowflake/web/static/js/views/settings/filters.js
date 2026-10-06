// The filter builder: which records are loaded at all.

import { $, element, icon } from "../../dom.js";
import { titled } from "../../format.js";
import { OPERATOR_LABELS } from "../../labels.js";
import { store } from "../../store.js";

// Which kind of record a filter or a left-out field applies to.
export function entitySelect(selected, label) {
  const select = element("select", { "aria-label": label }, [
    element("option", { value: "", text: "All data" }),
    ...store.config.choices.entities.map((name) => element("option", { value: name, text: titled(name) })),
  ]);
  select.value = selected || "";
  return select;
}

// A field name, with the fields of the chosen entity as suggestions.
export function fieldInput(entity, value, placeholder) {
  const field = element("input", { type: "text", value: value || "", placeholder, "aria-label": "Field" });
  const suggest = () => field.setAttribute("list", `fields-${entity.value || "all"}`);
  entity.addEventListener("change", suggest);
  suggest();
  return field;
}

export function removeButton(label) {
  return element("button", { type: "button", class: "remove", "aria-label": label, title: label }, [icon("close")]);
}

// Suggestions are per entity, and "all" for a filter that applies to every one.
export function buildFieldSuggestions() {
  document.querySelectorAll("datalist").forEach((list) => list.remove());
  const everything = new Set();
  for (const [entity, fields] of Object.entries(store.config.choices.fields)) {
    fields.forEach((name) => everything.add(name));
    document.body.append(
      element("datalist", { id: `fields-${entity}` }, fields.map((name) => element("option", { value: name }))),
    );
  }
  document.body.append(
    element("datalist", { id: "fields-all" }, [...everything].sort().map((name) => element("option", { value: name }))),
  );
}

function filterRow(filter, changed) {
  const entity = entitySelect(filter.entity, "Applies to");
  const field = fieldInput(entity, filter.field, "field, e.g. environment");
  const operator = element(
    "select",
    { "aria-label": "Condition" },
    store.config.choices.operators.map((op) => element("option", { value: op, text: OPERATOR_LABELS[op] || op })),
  );
  operator.value = filter.op || "=";
  const value = element("input", { type: "text", value: filter.value || "", placeholder: "value", "aria-label": "Value" });
  const remove = removeButton("Remove this filter");
  const row = element("div", { class: "filter" }, [
    element("span", { class: "joiner", "aria-hidden": "true" }),
    entity,
    field,
    operator,
    value,
    remove,
  ]);
  remove.addEventListener("click", () => {
    row.remove();
    changed();
  });
  return row;
}

export function renderFilters(changed) {
  $("filters").replaceChildren(...store.config.filters.map((filter) => filterRow(filter, changed)));
}

export function addFilter(changed) {
  const row = filterRow({}, changed);
  $("filters").append(row);
  row.querySelector("input").focus();
  changed();
}

// As the API takes them: [entity:]field<op>value.
export function readFilters() {
  const filters = [];
  for (const row of $("filters").children) {
    const [entity, field, operator, value] = row.querySelectorAll("select, input");
    if (!field.value.trim() && !value.value.trim()) continue;
    const scope = entity.value ? `${entity.value}:` : "";
    filters.push(`${scope}${field.value.trim()}${operator.value}${value.value.trim()}`);
  }
  return filters;
}
