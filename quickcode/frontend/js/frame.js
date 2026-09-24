// Work that only has to be done by the next paint, however often it is asked
// for: a burst of stream deltas, a replay's worth of events, a drag.

/** `fn`, run at most once per animation frame. Calling the returned function
 *  queues it; `.pending()` says whether a run is queued, `.cancel()` drops it
 *  and `.flush()` runs a queued one now instead of at the frame. */
export function perFrame(fn) {
  let id = 0;
  const run = () => { id = 0; fn(); };
  const queue = () => { if (!id) id = requestAnimationFrame(run); };
  queue.pending = () => id !== 0;
  queue.cancel = () => {
    if (id) cancelAnimationFrame(id);
    id = 0;
  };
  queue.flush = () => {
    if (!id) return;
    queue.cancel();
    fn();
  };
  return queue;
}
