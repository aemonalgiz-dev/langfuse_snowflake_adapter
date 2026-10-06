// The words for what the API names: entities, field groups, operators, settings.

import { percent, plural } from "./format.js";

export const ENTITY_HELP = {
  observations: "Spans, generations and events: model, tokens, cost and timings.",
  scores: "Evaluation and review scores.",
  traces: "One row per trace.",
  sessions: "One row per session.",
  comments: "Comments left on traces, observations, sessions and prompts.",
  annotation_queues: "The review queues.",
  annotation_queue_items: "What is queued for review, and whether it is done.",
};

export const FIELD_GROUP_HELP = {
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

export const FIELD_GROUP_NAMES = {
  core: "Core",
  basic: "Basics",
  time: "Timestamps",
  io: "Inputs and outputs",
  metadata: "Metadata",
  model: "Model",
  usage: "Usage",
  prompt: "Prompt",
  metrics: "Metrics",
  trace_context: "Trace context",
};

export const OPERATOR_LABELS = {
  "=": "is",
  "!=": "is not",
  "~": "contains",
  "!~": "does not contain",
  ">": "is greater than",
  ">=": "is at least",
  "<": "is less than",
  "<=": "is at most",
};

const every = (number, unit) => (number ? `every ${plural(number, unit)}` : "off");
const list = (values) => (values.length ? values.join(", ") : "none");
const onOff = (value) => (value ? "on" : "off");

// Each editable setting: what to call it, and how to say a value of it.
const SETTINGS = {
  entities: ["Data", list],
  filters: ["Filters", list],
  exclude_fields: ["Fields left out", list],
  sample_rate: ["Sampling", (value) => `${percent(value)} of traces`],
  observation_fields: ["Observation content", list],
  expand_metadata: ["Metadata kept in full", list],
  schedule_minutes: ["Sync", (value) => every(value, "minute")],
  reconcile_every_hours: ["Reconcile", (value) => every(value, "hour")],
  reconcile_days: ["Reconcile the last", (value) => plural(value, "day")],
  reconcile_full: ["Compare by content", onOff],
  check_deletions: ["Report deletions", onOff],
  initial_backfill_days: ["First sync goes back", (value) => plural(value, "day")],
  lookback_minutes: ["Each sync re-reads", (value) => plural(value, "minute")],
  window_hours: ["Progress saved every", (value) => plural(value, "hour")],
};

// A setting and a value of it as a pair of words: ["Sampling", "50% of traces"].
export function describeSetting(name, value) {
  const [label, say] = SETTINGS[name] || [name.replaceAll("_", " "), String];
  try {
    return [label, say(value)];
  } catch {
    return [label, JSON.stringify(value)];
  }
}
