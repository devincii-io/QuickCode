// Searching past conversations from the session chip. Run like
// smoke_workspaces.js, against `workspace_smoke_server.py <port>`: a hit opens
// its event in the inspector, focuses the pane that already shows its
// conversation, or reopens a closed one at the event — and the saved layout
// never keeps that event's seq.
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
  const paneIds = () => page.locator(".ws-pane").evaluateAll((nodes) => nodes.map((n) => n.dataset.pane));
  const pane = (id) => page.locator(`.ws-pane[data-pane="${id}"]`);
  const frame = (id) => pane(id).frameLocator("iframe");
  let ids = await paneIds();
  const first = frame(ids[0]);
  await first.locator("#input").fill("Please review the workspace manager module");
  await first.locator("#input").press("Enter");
  await first.locator(".msg-assistant").waitFor();
  await ready(1);

  // 1. Search from the session chip, hit in the open conversation.
  await first.locator("#session-chip").click();
  const search = first.locator(".menu .menu-search");
  await search.waitFor();
  check(await search.evaluate((n) => n === document.activeElement), "session search box did not take focus");
  await search.fill("WORKSPACE manager");
  await first.locator(".menu .mi-hit").first().waitFor();
  const hitHtml = await first.locator(".menu .mi-hit").first().innerHTML();
  check(/<mark>workspace<\/mark>/i.test(hitHtml) && /<mark>manager<\/mark>/i.test(hitHtml), "hit snippet not highlighted: " + hitHtml);
  check(await first.locator(".menu .menu-rows .menu-item[data-conv]").count() >= 1, "title filter hid the matching session");
  await search.fill("zzzz-no-such-thing");
  await first.locator(".menu .menu-hits .menu-note", { hasText: "No messages match." }).waitFor();
  check((await first.locator(".menu .menu-rows").innerText()).includes("No session titles match."), "no empty-title note");
  await search.press("Escape");
  check(await search.inputValue() === "", "Escape did not clear the search first");
  check(await first.locator(".menu").count() === 1, "Escape with a query closed the menu");
  await search.fill("workspace manager");
  await first.locator(".menu .mi-hit").first().waitFor();
  await first.locator(".menu .mi-hit").first().click();
  await first.locator("#traj-detail:not(.hidden)").waitFor();
  check((await first.locator("#traj-detail").innerText()).includes("workspace manager"), "inspector did not open on the hit");

  // 2. From a second pane, a hit in the first pane's conversation focuses it.
  await pane(ids[0]).locator('[data-action="h"]').click();
  await ready(2);
  ids = await paneIds();
  const second = frame(ids[1]);
  await second.locator("#btn-panel-close").click();
  await second.locator("#input").fill("Something unrelated");
  await second.locator("#input").press("Enter");
  await second.locator(".msg-assistant").waitFor();
  await ready(2);
  // Close the first pane's inspector so the reveal is observable.
  await first.locator("#btn-panel-close").click();
  await second.locator("#session-chip").click();
  await second.locator(".menu .menu-search").fill("workspace manager");
  await second.locator(".menu .mi-hit").first().waitFor();
  await second.locator(".menu .mi-hit").first().click();
  await page.waitForFunction((id) => document.querySelector(`.ws-pane[data-pane="${id}"]`).classList.contains("focused"), ids[0]);
  await first.locator("#traj-detail:not(.hidden)").waitFor();
  check(await page.locator(".ws-pane").count() === 2, "a hit in an open conversation opened a duplicate pane");

  // 3. Close pane 1; the hit reopens its conversation in a new pane at the event.
  await pane(ids[0]).locator('[data-action="close"]').click();
  await ready(1);
  await second.locator("#session-chip").click();
  await second.locator(".menu .menu-search").fill("workspace manager");
  await second.locator(".menu .mi-hit").first().waitFor();
  await second.locator(".menu .mi-hit").first().click();
  await ready(2);
  const src = await page.locator(".ws-pane iframe").evaluateAll((nodes) => nodes.map((f) => f.getAttribute("src")));
  check(src.some((s) => /[?&]at=\d+/.test(s) && /[?&]resume=/.test(s)), "new pane had no at= " + src.join(" "));
  ids = await paneIds();
  const which = src.findIndex((s) => /[?&]at=/.test(s));
  const revealed = frame(ids[which]);
  await revealed.locator("#traj-detail:not(.hidden)").waitFor();
  check((await revealed.locator("#traj-detail").innerText()).includes("workspace manager"), "reopened pane did not reveal the hit");

  // 4. The saved layout never carries the transient seq.
  const saved = await page.evaluate(() => localStorage.getItem("qc-workspaces-v1"));
  check(!/"reveal"|"at"/.test(saved), "layout persisted the reveal seq");

  if (errors.length) failures.push(...errors);
  if (failures.length) throw new Error(failures.join("\n"));
  return { passed: true, checks: "chip search, highlight, empty notes, escape, inspector, focus open pane, reopen at event, layout hygiene", runtimeErrors: errors };
}
