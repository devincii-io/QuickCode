// The ARIA tabs pattern for a strip of role="tab" buttons that each show a
// panel of their own (the side panel, the terminal drawer): tab and panel name
// each other, only the selected tab is a Tab stop, and ←/→, Home and End move
// between tabs. Each tab is known by its data-tab.

let serial = 0;

/** Pair every tab in `list` with `panelFor(tab)` and wire the keys; `select(id)`
 *  is the caller's own way of choosing a tab, run for the one a key lands on. */
export function wireTabs(list, panelFor, select) {
  const prefix = `tabs${++serial}`;
  for (const tab of list.querySelectorAll('[role="tab"]')) {
    const panel = panelFor(tab);
    if (!panel) continue;
    tab.id ||= `${prefix}-${tab.dataset.tab}`;
    panel.id ||= `${tab.id}-panel`;
    tab.setAttribute("aria-controls", panel.id);
    panel.setAttribute("role", "tabpanel");
    panel.setAttribute("aria-labelledby", tab.id);
  }
  list.addEventListener("keydown", (e) => {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    const at = tabs.indexOf(e.target.closest?.('[role="tab"]'));
    const to = { ArrowRight: at + 1, ArrowLeft: at - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (at < 0 || to === undefined) return;
    e.preventDefault();
    const tab = tabs[(to + tabs.length) % tabs.length];
    select(tab.dataset.tab);
    tab.focus();
  });
}

/** Show `id` as the selected tab: its state, and the one Tab stop in the strip. */
export function markSelected(list, id) {
  for (const tab of list.querySelectorAll('[role="tab"]')) {
    const on = tab.dataset.tab === id;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
  }
}
