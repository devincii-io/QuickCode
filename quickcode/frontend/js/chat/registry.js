// Every card in the transcript, by the ids later events name it with.
//
// A tool result, a permission verdict and a subagent's next step each refer
// back to a card drawn earlier. Finding it with a selector walked the whole
// transcript once per event: quadratic over a replay, and nearly all of what a
// 10k-event session cost to open. Nothing here touches the DOM; an entry's
// `card` is whatever the caller stores.
//
// Call ids are only unique within one agent's stream, so each agent (and the
// main agent, MAIN) gets its own map.

export const MAIN = "";

export class CardRegistry {
  constructor() { this.clear(); }

  clear() {
    this.calls = new Map();      // scope -> Map<call id, entry>
    this.running = new Map();    // scope -> Set<entry> still awaiting a result
    this.undecided = new Map();  // tool name -> Set<entry> no permission verdict has reached
    this.agents = new Map();     // agent_id -> the caller's record for its card
    this.serial = 0;
  }

  /** An ordinal for a new top-level block. Cards sort by (block, their own
   *  serial), which is document order: blocks only ever append, and a card only
   *  ever appends inside its block. */
  nextBlock() { return ++this.serial; }

  /** Register a card: `{ scope, id, name, summary, block, card }`. */
  addCall(entry) {
    entry.n = ++this.serial;
    let calls = this.calls.get(entry.scope);
    if (!calls) this.calls.set(entry.scope, calls = new Map());
    // A repeated id keeps resolving to the first card, as the query did.
    if (!calls.has(entry.id)) calls.set(entry.id, entry);
    setIn(this.running, entry.scope).add(entry);
    setIn(this.undecided, entry.name).add(entry);
    return entry;
  }

  /** The card for a call id within one agent's stream. */
  callIn(scope, id) { return this.calls.get(scope)?.get(id) || null; }

  /** The card for a call id: the named agent's own first, then the earliest of
   *  anyone else's. */
  call(id, scope = MAIN) {
    const own = this.callIn(scope, id);
    if (own) return own;
    let best = null;
    for (const [s, calls] of this.calls) {
      const hit = s === scope ? null : calls.get(id);
      if (hit && (!best || before(hit, best))) best = hit;
    }
    return best;
  }

  /** The call got its answer, or never will. */
  settle(entry) { this.running.get(entry.scope)?.delete(entry); }

  /** Calls in one scope still awaiting a result, oldest first. */
  runningIn(scope) { return [...(this.running.get(scope) || [])]; }

  /** A permission verdict (or its request) has reached this card. */
  decide(entry) { this.undecided.get(entry.name)?.delete(entry); }

  /** Cards of one tool no permission event has reached, in document order. */
  undecidedFor(name) { return [...(this.undecided.get(name) || [])].sort(order); }
}

function setIn(map, key) {
  let set = map.get(key);
  if (!set) map.set(key, set = new Set());
  return set;
}

function order(a, b) { return a.block - b.block || a.n - b.n; }
function before(a, b) { return order(a, b) < 0; }
