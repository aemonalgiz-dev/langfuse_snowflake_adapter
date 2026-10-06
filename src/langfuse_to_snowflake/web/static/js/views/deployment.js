// What engineering set in the environment, for orientation. Nothing is editable.

import { $, element } from "../dom.js";
import { store } from "../store.js";

function group(title, facts) {
  return element("section", {}, [
    element("h3", { text: title }),
    element(
      "dl",
      {},
      facts.map(([term, text]) => element("div", {}, [element("dt", { text: term }), element("dd", { text })])),
    ),
  ]);
}

export function renderDeployment() {
  const { deployment } = store.config;
  $("deployment-list").replaceChildren(
    group("Read from", [
      ["Project", store.config.project],
      ["Langfuse", deployment.langfuse_host],
      ["Langfuse API", deployment.api_version],
    ]),
    group("Written to", [
      ["Snowflake account", deployment.snowflake_account],
      ["Database and schema", `${deployment.snowflake_database}.${deployment.snowflake_schema}`],
      ["Warehouse", deployment.snowflake_warehouse],
      ["User and role", [deployment.snowflake_user, deployment.snowflake_role].filter(Boolean).join(" / ")],
      ["Object prefix", deployment.table_prefix || "(none)"],
    ]),
    group("Kept by the service", [
      ["Settings", store.config.stored_in || "Nowhere: they last until a restart"],
      ["Run history", store.config.runs_stored_in || "Memory: it starts empty after a restart"],
    ]),
  );
}
