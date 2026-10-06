// The HTTP API, behind the access key.

const KEY_STORAGE = "langfuse-to-snowflake-api-key";

// The key lives for the tab only.
export const accessKey = {
  get: () => sessionStorage.getItem(KEY_STORAGE),
  set: (value) => sessionStorage.setItem(KEY_STORAGE, value),
  forget: () => sessionStorage.removeItem(KEY_STORAGE),
};

let refused = () => {};

// What to do when the service asks for a key, or turns down the one it was given.
export function onRefused(handler) {
  refused = handler;
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === "string" ? detail : `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

export async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  const key = accessKey.get();
  if (key) headers["X-API-Key"] = key;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  if (response.status === 401) {
    refused(Boolean(key));
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
