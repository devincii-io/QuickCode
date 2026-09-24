// An agent pane's side of notifications (js/notify.js has the rest).
//
// Inside the workspace a pane only reports what happened; the shell knows which
// pane you are looking at and keeps the count. A pane opened on its own is its
// own window, so it badges its own title and raises its own notification.

import { readAppearance } from "./appearance.js";
import { Attention, badgeTitle, noticeCopy, showOsNotice, turnWatcher, windowActive } from "./notify.js";
import { store, subscribe } from "./store.js";

const SELF = "pane";

/** `tell(notice)` hands each notice to the workspace; without it the pane
 *  handles them itself. `label()` names the conversation in a notification. */
export function initPaneNotices({ tell = null, label = () => "" } = {}) {
  const watch = turnWatcher();
  const unseen = new Attention();
  const retitle = () => {
    const title = badgeTitle(document.title, unseen.total());
    if (title !== document.title) document.title = title;
  };
  const acknowledge = () => { if (windowActive() && unseen.clear(SELF)) retitle(); };

  subscribe((kind, ev) => {
    const notice = watch(kind, ev, store.replaying);
    if (!notice) return;
    if (tell) { tell(notice); return; }
    if (windowActive() || !unseen.add(SELF, notice.kind)) return;
    retitle();
    if (readAppearance().notify) {
      showOsNotice(noticeCopy(notice.kind, label(), notice.detail),
        { tag: "qc-pane", onClick: () => window.focus() });
    }
  });
  if (tell) return;
  window.addEventListener("focus", acknowledge);
  document.addEventListener("visibilitychange", acknowledge);
  // Views set the title as they change; the count has to survive that.
  const title = document.querySelector("title");
  if (title) new MutationObserver(retitle).observe(title, { childList: true });
}
