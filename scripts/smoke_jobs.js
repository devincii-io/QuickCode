// Run with Playwright's browser_run_code against `workspace_smoke_server.py --jobs`.
// One pane: the preview agent starts a background ticker, and the terminal
// drawer's Jobs tab has to list it, tail it live, and kill it.
async (page) => {
  const failures = [];
  const errors = [];
  const check = (ok, text) => { if (!ok) failures.push(text); };
  page.on("pageerror", (error) => errors.push(error.message));
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto("http://127.0.0.1:8769/?pane=1#token=workspace-preview");
  await page.locator(".hc-new").first().click();
  await page.locator("#input").waitFor();
  await page.waitForFunction(() => document.querySelector("#st-conn")?.title === "connection: open");

  // Before anything runs: the tab says what it is for, and no badge.
  await page.locator("#btn-term-toggle").click();
  await page.locator('.qt-tab[data-tab="jobs"]').click();
  await page.locator(".qt-jobs.qt-jobs-none .qt-jobs-empty").waitFor();
  check(!(await page.locator("#btn-term-toggle").getAttribute("data-jobs")), "a badge with no job running");
  check(await page.locator("#btn-term-clear").isHidden(), "the shell's Clear shows on the Jobs tab");

  // The agent starts the job; the real permission prompt approves it.
  await page.locator("#input").fill("Start the ticker");
  await page.locator("#input").press("Enter");
  await page.locator('.modal [data-act="allow"]').waitFor();
  await page.waitForTimeout(500);   // a review ignores clicks for its first 400 ms
  await page.locator('.modal [data-act="allow"]').click();

  const row = page.locator('.qt-job[data-id="bash_1"]');
  await row.waitFor();
  await page.waitForFunction(() =>
    document.querySelector('.qt-job[data-id="bash_1"] .qt-job-chip')?.textContent === "running");
  check(await page.locator("#btn-term-toggle").getAttribute("data-jobs") === "1", "no running-job badge on the toggle");
  check(await page.locator('.qt-tab[data-tab="jobs"] .qt-tab-count').textContent() === "1", "no count on the Jobs tab");
  check((await page.locator(".qt-jobs-cmd").textContent()).includes("ticker.py"), "the head does not show the command");

  // Live: the tick count keeps climbing without a reload, in the emulator's colours.
  const lastTick = () => page.locator(".qt-jobs-out").evaluate((out) => {
    const all = [...out.textContent.matchAll(/tick (\d+)/g)];
    return all.length ? Number(all[all.length - 1][1]) : 0;
  });
  await page.waitForFunction(() => /tick \d+/.test(document.querySelector(".qt-jobs-out")?.textContent || ""));
  const first = await lastTick();
  await page.waitForFunction((n) => {
    const all = [...(document.querySelector(".qt-jobs-out")?.textContent || "").matchAll(/tick (\d+)/g)];
    return all.length && Number(all[all.length - 1][1]) >= n + 3;
  }, first);
  check(await page.locator('.qt-jobs-out span[style*="--qt-a2"]').count() > 0, "the output lost its colour");
  check(await page.locator(".qt-jobs-out").evaluate((out) => out.scrollHeight - out.scrollTop - out.clientHeight < 30),
    "the live output does not follow its end");
  check(!(await page.locator(".qt-jobs-out").textContent()).includes("\x1b"), "raw escape codes reached the page");

  // Reading here is not reading for the model.
  const unread = await page.evaluate(async () => {
    const auth = { headers: { "x-quickcode-token": "workspace-preview" } };
    const [session] = await (await fetch("/api/sessions", auth)).json();
    const res = await fetch(`/api/conversations/${session.conv_id}/jobs`, auth);
    return (await res.json()).jobs[0].unread;
  });
  check(unread > 0, `the panel's reads consumed the model's output (unread=${unread})`);

  // Copy command reports what it copied.
  await page.locator(".qt-jobs-btn", { hasText: "Copy command" }).click();
  await page.locator(".toast", { hasText: "Command copied" }).waitFor();

  // Kill asks first, and Cancel means no.
  await page.locator(".qt-jobs-kill").click();
  await page.locator(".modal", { hasText: "Kill bash_1?" }).waitFor();
  await page.locator(".modal .btn", { hasText: "Cancel" }).click();
  check(await row.locator(".qt-job-chip").textContent() === "running", "Cancel killed the job");
  await page.locator(".qt-jobs-kill").click();
  await page.locator(".modal [data-yes]").click();
  await page.waitForFunction(() =>
    document.querySelector('.qt-job[data-id="bash_1"] .qt-job-chip')?.textContent === "killed by you");
  await page.waitForFunction(() => !document.querySelector("#btn-term-toggle")?.dataset.jobs);
  check(await page.locator(".qt-jobs-kill").isHidden(), "Kill still offered for a job that ended");
  await page.locator("#transcript", { hasText: "killed from the Jobs tab" }).waitFor();
  const after = await lastTick();
  await page.waitForTimeout(800);
  check(await lastTick() === after, "output kept arriving after the kill");

  // Narrow: list above output, nothing overflows the window.
  await page.setViewportSize({ width: 700, height: 800 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "the Jobs tab overflows a narrow window");

  if (errors.length) failures.push(...errors);
  if (failures.length) throw new Error(failures.join("\n"));
  return { passed: true, checks: "empty state, permission, list, badges, live tail, colour, follow, model cursor, copy, kill confirm/cancel, killed by you, transcript note, narrow", runtimeErrors: errors };
}
