// Building the page. Text from the API is always placed with textContent,
// never as HTML.

const SVG = "http://www.w3.org/2000/svg";

export const $ = (id) => document.getElementById(id);

export function element(tag, properties = {}, children = []) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(properties)) {
    if (name === "text") node.textContent = value;
    else if (name === "class") node.className = value;
    else if (name in node) node[name] = value;
    else node.setAttribute(name, value);
  }
  for (const child of children) if (child) node.append(child);
  return node;
}

// One of the drawings in the page's sprite.
export function icon(name, className = "") {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", `icon ${className}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(SVG, "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}
