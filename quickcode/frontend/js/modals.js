// Compatibility surface. The dialogs and menus that used to live here each have
// a module of their own now; import from those directly. Importing this file
// loads all of them, including the socket (js/ws.js), which the outer
// workspace window must never do.

export { confirmModal } from "./ui/modal.js";
export { initReviews } from "./reviews.js";
export { MODES } from "./modes.js";
export { openModeMenu, openModelMenu } from "./menus.js";
export { openHelp } from "./help/quickref.js";
export { makeSelection, reportBulk } from "./selection.js";
export { openPurgeProjects } from "./purge.js";
export { openRenameSession } from "./session_rename.js";
export { openSessionMenu } from "./sessions_menu.js";
export { openDirBrowser } from "./dirbrowser.js";
export { creditLine, openQuickSettings } from "./quick_settings.js";
