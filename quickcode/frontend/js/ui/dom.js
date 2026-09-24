// Building DOM from nodes rather than markup strings, for views that show text
// somebody else wrote (paths, diffs, command output): a text node cannot be
// anything but text, and nothing here needs an inline handler.

/** `h("div", {class, text, onclick, "aria-label": …}, ...children)`. A null,
 *  false or empty child is skipped; `true` sets a bare attribute. */
export function h(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false || child === "") continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}
