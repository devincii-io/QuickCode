// The command palette (Ctrl+K), in the workspace shell and inside an agent
// pane. Run like smoke_workspaces.js, against `workspace_smoke_server.py <port>`.
async (page) => {
  const failures = [];
  const errors = [];
  const check = (ok, text) => { if (!ok) failures.push(text); };
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("http://127.0.0.1:8769/#token=workspace-preview");
  await page.evaluate(() => {
    for (const key of ["qc-workspaces-v1", "qc-workspace-chrome", "qc-appearance-v1"]) localStorage.removeItem(key);
  });
  await page.reload();
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.getByRole("button", { name: /^Website redesign/ }).click();
  const ready = (count) => page.waitForFunction((count) =>
    [...document.querySelectorAll(".ws-pane:not([hidden]) .ws-pane-status")].filter((n) => n.textContent === "Ready").length === count, count);
  await ready(1);
  const palette = page.locator(".menu.palette");

  // ---- the outer window ----
  await page.locator("#ws-title").click();
  await page.keyboard.press("Control+K");
  await palette.waitFor();
  const aria = await palette.evaluate((m) => {
    const input = m.querySelector("input");
    const opt = document.getElementById(input.getAttribute("aria-activedescendant"));
    return {
      role: m.getAttribute("role"), focused: document.activeElement === input,
      combobox: input.getAttribute("role"), listbox: m.querySelector(".menu-list").getAttribute("role"),
      option: opt?.getAttribute("role"), selected: opt?.getAttribute("aria-selected"),
      groups: [...m.querySelectorAll(".menu-head")].map((h) => h.textContent),
    };
  });
  check(aria.role === "dialog" && aria.focused && aria.combobox === "combobox" && aria.listbox === "listbox"
    && aria.option === "option" && aria.selected === "true", "outer palette aria: " + JSON.stringify(aria));
  check(aria.groups.includes("Agents") && aria.groups.includes("Settings") && aria.groups.includes("Help"), "outer groups: " + aria.groups);
  check(!aria.groups.includes("Slash commands"), "the shell listed conversation commands");
  await page.keyboard.press("ArrowDown");
  check(await palette.evaluate((m) => m.querySelector('[aria-selected="true"]').dataset.i) === "1", "ArrowDown did not move the selection");
  await page.keyboard.type("split right");
  check((await palette.locator('[aria-selected="true"]').innerText()).includes("Split right"), "split right not first");
  await page.keyboard.press("Enter");
  await ready(2);
  check(await palette.count() === 0, "palette stayed open after running");

  await page.locator("#ws-title").click();
  await page.keyboard.press("Control+K");
  await page.keyboard.type("sidebar");
  await page.keyboard.press("Enter");
  check(await page.locator("#workspace-shell.ws-collapsed").count() === 1, "sidebar did not collapse");
  await page.keyboard.press("Control+K");
  await page.keyboard.type("sidebar");
  await page.keyboard.press("Enter");
  check(await page.locator("#workspace-shell.ws-collapsed").count() === 0, "sidebar did not come back");

  await page.keyboard.press("Control+K");
  await palette.waitFor();
  await page.keyboard.press("Control+K");
  check(await palette.count() === 0, "Ctrl+K did not close an open palette");
  await page.keyboard.press("Control+K");
  await page.keyboard.press("Escape");
  check(await palette.count() === 0, "Escape did not close the palette");

  // A Help page opens in the utility dialog, and the palette is back after it.
  await page.keyboard.press("Control+K");
  await palette.waitFor();
  await page.keyboard.type("keyboard commands");
  await page.keyboard.press("Enter");
  await page.locator(".ws-utility iframe").waitFor();
  check((await page.locator(".ws-utility iframe").getAttribute("src")).includes("#/help/keyboard"), "help page not opened");
  await page.frameLocator(".ws-utility iframe").locator(".hp-keys dt", { hasText: "Ctrl + K" }).waitFor();
  await page.evaluate(() => document.querySelector(".ws-utility").close());
  await page.waitForFunction(() => !document.querySelector(".ws-utility"));
  await page.keyboard.press("Control+K");
  check(await palette.count() === 1, "palette would not open after the utility closed");
  await page.keyboard.press("Escape");

  // ---- inside a pane ----
  const ids = await page.locator(".ws-pane").evaluateAll((nodes) => nodes.map((n) => n.dataset.pane));
  const first = page.locator(`.ws-pane[data-pane="${ids[0]}"]`).frameLocator("iframe");
  const inner = first.locator(".menu.palette");
  const input = first.locator(".menu.palette input");
  await first.locator("#input").fill("Please review the workspace manager module");
  await first.locator("#input").press("Enter");
  await first.locator(".msg-assistant").waitFor();
  await ready(2);
  await first.locator("#input").press("Control+K");
  await inner.waitFor();
  check(await palette.count() === 0, "the shell opened a palette for a key pressed in a pane");
  const groups = await inner.locator(".menu-head").allTextContents();
  for (const g of ["Slash commands", "Permission mode", "Model", "Sessions", "Panes", "View", "Settings", "Help"]) {
    check(groups.includes(g), `pane palette lacks ${g}: ${groups}`);
  }
  await input.fill("/comp");
  check((await inner.locator('[aria-selected="true"]').innerText()).startsWith("/compact"), "/compact not first");
  await input.press("Escape");
  check(await first.locator("#input").evaluate((n) => n === document.activeElement), "focus did not return to the composer");

  await first.locator("#input").press("Control+K");
  await input.fill("plan mode");
  await input.press("Enter");
  await first.locator("#mode-pill", { hasText: "plan" }).waitFor();

  // Past conversations are searched too, and a hit opens the inspector on it.
  await first.locator("#input").press("Control+K");
  await input.fill("workspace manager");
  await inner.locator(".menu-head", { hasText: "In past conversations" }).waitFor();
  await inner.locator("[role=group]").last().locator(".pal-item").first().click();
  await first.locator("#traj-detail:not(.hidden)").waitFor();

  await first.locator("#btn-panel-close").click();
  await first.locator("#input").press("Control+K");
  await input.fill("split below");
  await input.press("Enter");
  await ready(3);

  // "Go to another agent or workspace…" hands over to the shell's palette.
  await first.locator("#input").press("Control+K");
  await input.fill("another agent");
  await input.press("Enter");
  await palette.waitFor();
  check(await inner.count() === 0, "pane palette still open after handing over");
  await page.keyboard.type("client portal");
  await palette.locator(".pal-item", { hasText: "Open Client portal" }).waitFor();
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => document.querySelector("#ws-title").textContent === "Client portal");

  // A pane on its own shows Settings in place; a sheet there is a dialog the
  // palette would open underneath, so the key does nothing.
  await page.goto("http://127.0.0.1:8769/?pane=1#token=workspace-preview");
  await page.evaluate(() => { location.hash = "#/config/parts/tools"; });
  await page.locator(".k-card [data-raw]").first().click();
  await page.locator(".set-sheet").waitFor();
  await page.keyboard.press("Control+K");
  await page.waitForTimeout(200);
  check(await page.locator(".menu.palette").count() === 0, "the palette opened under a Settings sheet");

  if (errors.length) failures.push(...errors);
  if (failures.length) throw new Error(failures.join("\n"));
  return { passed: true, checks: "shell palette aria, groups, keys, run, toggle, utility, pane palette, slash, mode, message search, split, hand-over, not under a sheet", runtimeErrors: errors };
}
