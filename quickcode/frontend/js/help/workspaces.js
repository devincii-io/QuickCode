import { pageHtml, sub } from "./ui.js";

export function renderWorkspaces(host) {
  host.innerHTML = pageHtml("Workspaces & agent panes", {
    lede: "Each project folder is a workspace. Each pane is an independent conversation with an agent.",
    body: `${sub("Open and arrange agents")}
      <p class="hp-p">Choose Open folder in the sidebar. New agent opens another conversation alongside the focused pane.
      Use Split right or Split below in a pane header to choose a direction. A workspace holds up to eight panes.</p>
      <p class="hp-p">Drag a divider to resize. Double-click it for equal sizes, or focus it with Tab and use the arrow keys; Home and End jump to
      the smallest and largest size. Equalize pane sizes in a workspace's menu evens out every row and column.
      Drag a pane header onto another pane to move it to that pane's left, right, top, or bottom edge.</p>
      ${sub("Keep your place")}
      <p class="hp-p">Switch projects in the sidebar without disconnecting their agents. The sidebar shows whether each
      mounted agent is ready, working, offline, or waiting for approval. Maximize a pane to work in it alone;
      Restore panes brings the others back.</p>
      <p class="hp-p">When an agent you are not looking at finishes, needs approval, or fails, its pane, its sidebar row
      and its workspace get a count, and the window title shows the total. Focusing the pane clears it.
      Desktop notifications for a window in the background are off until you turn them on under Appearance.</p>
      <p class="hp-p">The session list in a pane's top bar searches every conversation in the project, titles and
      messages both, and opens a match at the message that matched.</p>
      <p class="hp-p">Layouts and conversation references are saved on this device. Reloading restores the last workspace;
      other workspaces reconnect when you select them. Unsent drafts are kept for this browser tab.</p>
      ${sub("Close and recover")}
      <p class="hp-p">Closing a pane detaches its view. It does not interrupt the agent or delete its conversation.
      Reopen closed pane restores the most recently closed pane. All projects also provides the full session history.
      Use Stop inside a pane to interrupt its agent before closing it if you want the work to stop.</p>
      ${sub("Appearance and settings")}
      <p class="hp-p">Appearance in the sidebar controls text size, spacing, conversation width, metrics, animation,
      and desktop notifications.
      Settings opens provider defaults, themes, agents, tools, and permissions. Each conversation also has its own model,
      permission mode, and composition controls beside the composer.</p>
      <p class="hp-p">Alt+N opens an agent. Alt+Z maximizes or restores. Alt+B toggles the sidebar.
      Alt+arrow keys focus another pane. These shortcuts also work while typing in an agent pane.
      Ctrl+K (⌘K on a Mac) opens the command palette for wherever you are: in a pane, its commands and past
      conversations; in the sidebar, every agent, workspace and page.</p>`,
  });
}
