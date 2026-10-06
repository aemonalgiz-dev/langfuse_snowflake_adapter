// Times, durations and counts, in words.

import { element } from "./dom.js";

const RELATIVE = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const UNITS = [
  ["year", 31536000],
  ["month", 2592000],
  ["day", 86400],
  ["hour", 3600],
  ["minute", 60],
];

export function count(number) {
  return Number(number || 0).toLocaleString("en");
}

export function plural(number, word, many = `${word}s`) {
  return `${count(number)} ${number === 1 ? word : many}`;
}

export function absolute(iso) {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// "12 minutes ago", "in 3 hours", "yesterday".
export function relative(iso, now = Date.now()) {
  const seconds = (new Date(iso).getTime() - now) / 1000;
  const size = Math.abs(seconds);
  if (size < 45) return seconds <= 0 ? "just now" : "in a moment";
  for (const [unit, length] of UNITS) {
    if (size >= length || unit === "minute") return RELATIVE.format(Math.round(seconds / length), unit);
  }
  return "";
}

// A time shown as how long ago it was, with the exact time on hover. The words
// are brought up to date by refreshTimes.
export function timeNode(iso, className = "") {
  const node = element("time", { text: relative(iso), title: absolute(iso), class: className });
  node.dateTime = iso;
  node.dataset.relative = "";
  return node;
}

export function refreshTimes() {
  document.querySelectorAll("time[data-relative]").forEach((node) => {
    node.textContent = relative(node.dateTime);
  });
}

// How long something took: "8 s", "1 min 12 s", "2 h 5 min".
export function took(start, end) {
  if (!start || !end) return "";
  const seconds = Math.max(0, Math.round((new Date(end) - new Date(start)) / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

// The stretch between two instants, for a table cell.
export function range(start, end) {
  const options = { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" };
  const from = new Date(start).toLocaleString(undefined, options);
  const to = new Date(end).toLocaleString(undefined, options);
  return `${from} to ${to}`;
}

// "annotation_queue_items" as a heading: "Annotation queue items".
export function titled(name) {
  const words = name.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function percent(share) {
  return `${Number((share * 100).toFixed(2))}%`;
}
