// What the page currently knows, shared by its views.

export const store = {
  // The project the page is about; every setting and status belongs to one.
  project: null,
  config: null,
  // How each entity is produced: a table that is synced, or a view.
  entities: [],
  schedule: null,
  // Per synced table: its watermark and when it was last reconciled.
  state: [],
  stateError: "",
  // Recent runs of every project, newest first.
  runs: [],
};

export function scoped(path) {
  return `${path}${path.includes("?") ? "&" : "?"}project=${encodeURIComponent(store.project)}`;
}

export function projectRuns() {
  return store.runs.filter((run) => run.project === store.project);
}

// Only one run is active at a time, across all projects.
export function activeRun() {
  return store.runs.find((run) => run.status === "queued" || run.status === "running") || null;
}
