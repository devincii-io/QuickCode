// The permission prompt, in a real browser. Run like smoke_workspaces.js,
// against `workspace_smoke_server.py <port> --ask`: every turn the preview
// agent reads README.md, edits it and runs a shell command, so the prompt has
// to show the edit's diff, the exact rule Always allow would save, the engine's
// own "Why?", the part of a command that cannot be saved, and ignore a click in
// its first 400 ms.
async (page) => {
  const failures = [];
  const errors = [];
  const check = (ok, text) => { if (!ok) failures.push(text); };
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.goto("http://127.0.0.1:8769/?pane=1#token=workspace-preview");
  await page.getByText("New chat").first().click();
  await page.locator("#input").waitFor();
  await page.waitForFunction(() => document.querySelector("#st-conn")?.title === "connection: open");
  await page.locator("#input").fill("Tidy the README");
  await page.locator("#input").press("Enter");

  // 1. The edit: a diff with the file's own context, one exact rule.
  const modal = page.locator(".modal");
  await modal.locator(".perm-diff").waitFor();
  const shownAt = Date.now();
  await modal.locator('[data-act="allow"]').click();
  check(Date.now() - shownAt < 400 ? await modal.locator(".perm-diff").count() === 1 : true,
    "a click inside 400 ms answered the review");
  const diffText = await modal.locator(".perm-diff").innerText();
  check(diffText.includes("+UI review project, with a permission preview."), "diff lacks the added line");
  check(diffText.includes("-UI review project."), "diff lacks the removed line");
  check(await modal.locator(".perm-diff .diff-add").count() >= 1, "added line is not coloured");
  check(await modal.locator(".perm-diff .diff-hunk").count() === 1, "no hunk header");
  const rules = await modal.locator(".perm-rules code").allInnerTexts();
  check(rules.length === 1 && rules[0] === "edit(README.md)", `edit rules: ${JSON.stringify(rules)}`);
  check(!(await modal.locator('[data-act="always"]').isDisabled()), "always allow disabled for an edit");

  // 2. Why? — the engine's own answer, inline.
  await modal.locator(".perm-why-toggle").click();
  await modal.locator(".perm-why .hp-verdict").waitFor();
  const verdict = await modal.locator(".perm-why .hp-verdict-badge").innerText();
  check(verdict.trim() === "ask", `why verdict ${verdict}`);
  check((await modal.locator(".perm-why").innerText()).includes("Always allow would write edit(README.md)"),
    "why does not name the rule");
  await page.waitForTimeout(450);
  await modal.locator('[data-act="allow"]').click();

  // 3. The command: the exact half that can be saved, and the half that cannot.
  await page.waitForFunction(() => document.querySelector(".modal .perm-preview")?.textContent.includes("FOO=1"));
  const bashRules = await modal.locator(".perm-rules code").allInnerTexts();
  check(JSON.stringify(bashRules) === JSON.stringify(["bash(FOO=1 npm test)"]), `bash rules ${JSON.stringify(bashRules)}`);
  const kept = await modal.locator(".perm-kept").innerText();
  check(kept.includes("cat .env") && kept.includes("protected path"), `kept: ${kept}`);
  check(await modal.locator(".perm-diff").count() === 0, "a command shows a diff");
  await page.waitForTimeout(450);
  // Deny takes a second click to confirm.
  await modal.locator('[data-act="deny"]').click();
  await modal.locator('[data-act="deny"]').click();
  await page.locator(".msg-assistant").last().waitFor();
  const transcript = await page.locator("#transcript").evaluate((n) => n.textContent);
  check(transcript.includes("edit(README.md)"), "chat card does not show the offered rule");
  check(transcript.includes("bash(FOO=1 npm test)"), "chat card does not show the bash rule");

  if (errors.length) failures.push(...errors);
  if (failures.length) throw new Error(failures.join("\n"));
  return { passed: true, checks: "diff, 400 ms guard, exact rule, why, kept parts, deny confirm, chat card", runtimeErrors: errors };
}
