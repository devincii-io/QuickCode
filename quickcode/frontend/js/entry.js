// Each agent view owns its event store, socket, composer and review dialogs.
// The outer window owns only workspaces and the positions of those views.
import { initAppearance } from "./appearance.js";

initAppearance();
if (new URLSearchParams(location.search).get("pane") === "1") {
  document.body.classList.add("agent-view");
  await import("./main.js");
} else {
  const { bootWorkspaces } = await import("./workspaces.js");
  await bootWorkspaces();
}
