// Checkpoints and rewind, end to end in a real browser. Run like
// smoke_workspaces.js, against `workspace_smoke_server.py <port> --edits`: the
// preview model reads and edits README.md and writes notes.md in the first
// turn, auto-allowed, and this script rewinds them from the transcript and
// checks the files on disk.
async (page) => {
  const { readFileSync, writeFileSync, existsSync } = await import("node:fs");
  const { join } = await import("node:path");
  const base = "http://127.0.0.1:8769";
  const failures = [];
  const errors = [];
  const check = (ok, text) => { if (!ok) failures.push(text); };
  page.on("pageerror", (error) => errors.push(error.message));

  const { projects } = await (await page.request.get(`${base}/api/projects`,
    { headers: { "x-quickcode-token": "workspace-preview" } })).json();
  const project = projects.find((p) => p.name === "Website redesign");
  const readme = join(project.path, "README.md");
  const notes = join(project.path, "notes.md");
  const original = readFileSync(readme, "utf8");

  await page.setViewportSize({ width: 1300, height: 900 });
  await page.goto(`${base}/?pane=1&project=${project.id}#token=workspace-preview`);
  await page.locator("#input").fill("Tidy the readme and leave a note");
  await page.locator("#input").press("Enter");
  const rewindTurn1 = page.getByRole("button", { name: "Rewind files to before turn 1" });
  await rewindTurn1.waitFor();
  await page.locator(".msg-assistant").waitFor();
  check(readFileSync(readme, "utf8").includes("edited by the agent"), "turn 1 did not edit README.md");
  check(existsSync(notes), "turn 1 did not write notes.md");

  // Something other than the agent changes notes.md: a conflict.
  writeFileSync(notes, "typed by hand\n");

  // Keyboard: the per-turn button is a real button.
  await rewindTurn1.focus();
  await page.keyboard.press("Enter");
  const dialog = page.locator(".rw-modal");
  await dialog.locator(".rw-file").nth(1).waitFor();
  const row = (path) => dialog.locator(`.rw-file[data-path="${path}"]`);
  const go = dialog.locator("[data-rewind]");
  check(await row("notes.md").evaluate((n) => n.classList.contains("rw-conflicted")), "conflict not called out");
  check(!(await row("notes.md").locator("input[type=checkbox]").isChecked()), "a conflicted file started selected");
  check(await row("README.md").locator("input[type=checkbox]").isChecked(), "README.md not selected");
  check((await row("notes.md").locator(".rw-conflicts").innerText()).includes("Changed on disk since turn 1"),
    "conflict detail missing");
  check((await dialog.locator(".rw-untracked").innerText()).includes("Bash changes are not tracked"), "bash notice missing");
  // The turn's closing state event can land just after its last message.
  await dialog.locator(".rw-busy[hidden]").waitFor({ state: "attached" });
  check(await go.innerText() === "Rewind 1 file" && await go.isEnabled(), "rewind button not ready");

  await row("README.md").locator(".rw-toggle").click();
  const diff = await row("README.md").locator(".rw-pre").innerText();
  check(diff.includes("-UI review project, edited by the agent.") && diff.includes("+UI review project."),
    `README diff wrong: ${diff}`);
  check(await row("README.md").locator(".rw-toggle").getAttribute("aria-expanded") === "true", "toggle not expanded");

  await row("notes.md").locator("input[type=checkbox]").check();
  check(await go.isDisabled(), "a conflicted file could be rewound without Overwrite anyway");
  check((await dialog.locator(".rw-msg").innerText()).includes("Overwrite anyway"), "no hint to overwrite");
  await row("notes.md").locator("input[type=checkbox]").uncheck();

  await go.click();
  await dialog.locator(".rw-headline").filter({ hasText: "Rewound to before turn 1: 1 restored." }).waitFor();
  check(readFileSync(readme, "utf8") === original, "README.md was not put back exactly");
  check(readFileSync(notes, "utf8") === "typed by hand\n", "the deselected file was touched");
  await dialog.getByRole("button", { name: "Close" }).click();
  await page.locator(".rewind-note").first().waitFor();
  check((await page.locator(".rewind-note").first().innerText()).includes("files rewound to before turn 1 · 1 restored"),
    "the transcript has no files_rewound note");
  const focusBack = () => document.activeElement?.classList.contains("turn-rewind");
  await page.waitForFunction(focusBack, null, { timeout: 3000 }).catch(() => {});
  check(await page.evaluate(focusBack), "focus did not return to the rewind button");

  // A replayed log draws the same marks: a button for what is left, a note for what was done.
  const [latest] = await (await page.request.get(`${base}/api/projects/${project.id}/sessions`,
    { headers: { "x-quickcode-token": "workspace-preview" } })).json();
  await page.goto(`${base}/?pane=1&project=${project.id}&resume=${latest.conv_id}#token=workspace-preview`);
  await rewindTurn1.waitFor();
  await page.locator(".rewind-note").first().waitFor();
  check(await page.locator(".rewind-note").count() === 1, "a replay drew the rewind note wrong");

  // While a turn runs a rewind waits, and the dialog says so in the API's words.
  await page.locator("#input").fill("One more thing");
  await page.locator("#input").press("Enter");
  await rewindTurn1.click();
  await dialog.locator(".rw-busy").filter({ hasText: "a turn is running" }).waitFor();
  check(await go.isDisabled(), "rewind allowed while a turn runs");
  await dialog.locator(".rw-busy[hidden]").waitFor({ state: "attached" });
  check(await dialog.locator(".rw-file").count() === 1, "README.md is still offered after its rewind");

  await dialog.locator(".rw-force input").check();
  check(await go.isEnabled() && await go.evaluate((b) => b.classList.contains("danger")), "Overwrite anyway did not arm");
  await go.click();
  await dialog.locator(".rw-headline").filter({ hasText: "1 deleted" }).waitFor();
  check(!existsSync(notes), "forced rewind did not delete notes.md");
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  await page.locator(".rewind-note").nth(1).waitFor();
  check((await page.locator(".rewind-note").nth(1).innerText()).includes("overwrote changes made since"),
    "the forced rewind's note does not say so");
  check(await rewindTurn1.count() === 0, "the button stayed after everything was rewound");

  await page.locator("#btn-panel-toggle").click();
  await page.locator('.panel-tab[data-tab="trajectory"]').click();
  const rows = await page.locator(".tj-row .preview").allInnerTexts();
  check(rows.some((t) => t.startsWith("checkpoint README.md · turn 1")), "trajectory has no checkpoint row");
  check(rows.some((t) => t.startsWith("files rewound to before turn 1 · 1 file: notes.md deleted")),
    `trajectory has no rewind row: ${rows.join(" | ")}`);

  if (errors.length) failures.push(...errors);
  if (failures.length) throw new Error(failures.join("\n"));
  return { passed: true, checks: "per-turn button (keyboard), preview, diff, conflict, deselect, rewind on disk, note, focus return, replay, busy refusal, force, trajectory", runtimeErrors: errors };
}
