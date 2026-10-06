// Earlier versions of a project's settings, and the way back to the defaults.

import { api } from "../../api.js";
import { $, element } from "../../dom.js";
import { plural, timeNode } from "../../format.js";
import { describeSetting } from "../../labels.js";
import { scoped, store } from "../../store.js";

function change(name, value) {
  const [label, text] = describeSetting(name, value);
  return element("span", { class: "change" }, [element("b", { text: `${label} ` }), text]);
}

function version(entry) {
  const names = Object.keys(entry.settings);
  const changes = names.length
    ? names.map((name) => change(name, entry.settings[name]))
    : [element("span", { class: "muted", text: "Everything at the deployment's defaults" })];
  return element("li", {}, [timeNode(entry.changed_at), element("div", { class: "changes" }, changes)]);
}

export async function renderHistory() {
  const { overridden, has_history: kept } = store.config;
  const differ = overridden.length
    ? `${plural(overridden.length, "setting")} of this project ${overridden.length === 1 ? "differs" : "differ"} from the deployment's defaults.`
    : "Every setting of this project is at the deployment's default.";
  $("history-note").textContent = kept
    ? `${differ} Below is every saved version, newest first, each listing what differed at the time.`
    : differ;
  $("reset").disabled = !overridden.length;

  const message = $("history-message");
  message.hidden = true;
  if (!kept) {
    $("history-rows").replaceChildren();
    return;
  }
  try {
    const versions = await api(scoped("/config/history"));
    $("history-rows").replaceChildren(...versions.map(version));
    message.hidden = versions.length > 0;
    message.textContent = "Nothing has been changed for this project yet.";
  } catch (error) {
    if (error.status === 401) return;
    message.hidden = false;
    message.textContent = `The earlier versions could not be read: ${error.message}`;
  }
}
