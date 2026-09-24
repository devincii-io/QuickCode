// A reusable set of child nodes. Windowed views repaint every frame while the
// reader scrolls or zooms; reusing nodes instead of re-parsing markup keeps
// that cheap, and it keeps copy.js's MutationObserver from sweeping hundreds
// of freshly inserted nodes each frame.

export function nodePool(parent, make) {
  const nodes = [];
  let used = 0;
  return {
    /** The next node for this paint, created on first need. */
    next() {
      let n = nodes[used];
      if (!n) { n = make(); nodes.push(n); parent.appendChild(n); }
      if (n.hidden) n.hidden = false;
      used++;
      return n;
    },
    /** Start a paint. */
    begin() { used = 0; },
    /** End a paint: hide whatever this one did not use. */
    end() { for (let i = used; i < nodes.length; i++) if (!nodes[i].hidden) nodes[i].hidden = true; },
  };
}
