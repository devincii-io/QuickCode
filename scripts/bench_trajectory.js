// Run with Playwright's browser_run_code against workspace_smoke_server.py.
// Replays a synthetic 10k-event session into one agent pane and times the
// trajectory: replay-to-paint, then frame times while scrolling the table,
// zooming and panning the lanes, and appending live events. Fails only on
// structure (the DOM must stay windowed); the timings are for reading.
//
// ISOLATE swaps the chat renderer for a no-op so that replayToPaintMs times the
// trajectory alone. The transcript is windowed now and leaves the frame times
// unchanged, but it still does its own replay work, several times the
// trajectory's (roughly 90 ms against 450 ms with it at 10k events).
// scripts/bench_chat.js times that side; set ISOLATE false to time the pane
// as a user gets it.
async (page) => {
  const ISOLATE = true;
  const origin = "http://127.0.0.1:8769";
  if (ISOLATE) {
    await page.route("**/js/chat.js*", (route) => route.fulfill({
      contentType: "text/javascript", body: "export function initChat() {}\n" }));
  }
  await page.goto(`${origin}/#token=workspace-preview`);
  const pid = await page.evaluate(async () => {
    const res = await fetch("/api/projects", { headers: { "x-quickcode-token": "workspace-preview" } });
    const body = await res.json();
    return (body.projects || body)[0].id;
  });
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.goto(`${origin}/?pane=1&project=${encodeURIComponent(pid)}#token=workspace-preview`);
  await page.locator("#input").waitFor();
  await page.click("#btn-panel-toggle");
  return page.evaluate(async () => {
    const { ingest, store } = await import("/js/store.js");
    const frame = () => new Promise((r) => requestAnimationFrame(r));
    const frames = async (n, step) => {
      const d = [];
      let last = performance.now();
      for (let i = 0; i < n; i++) {
        step(i);
        await frame();
        const now = performance.now();
        d.push(now - last);
        last = now;
      }
      d.sort((a, b) => a - b);
      const at = (q) => +d[Math.min(d.length - 1, Math.floor(q * d.length))].toFixed(1);
      return { p50: at(0.5), p95: at(0.95), max: at(1) };
    };

    // A session shaped like a real one: turns of rounds of parallel calls,
    // subagents with their own calls, idle breaks.
    const evs = [];
    let t = Date.parse("2026-01-01T09:00:00"), seq = 0, call = 0;
    const iso = (ms) => new Date(ms).toISOString().slice(0, -1);
    const push = (ev) => evs.push({ seq: ++seq, ts: iso(t), turn: 1, ...ev });
    push({ type: "system_prompt", text: "You are QuickCode. ".repeat(400) });
    while (evs.length < 10000) {
      t += seq % 97 === 0 ? 12 * 60000 : 4000;
      push({ type: "user_message", text: "task" });
      for (let r = 0; r < 5; r++) {
        t += 1500;
        push({ type: "assistant_message", text: "Let me look.", finish_reason: "tool_calls" });
        const ids = [1, 2, 3].map(() => "c" + ++call);
        for (const id of ids) push({ type: "tool_call", id, name: "read", arguments: "{}" });
        for (const id of ids) { t += 200; push({ type: "tool_result", id, name: "read", content: "ok", ms: 200 }); }
        if (r === 2) {
          const agent = "explore-" + call;
          push({ type: "agent_spawned", agent_id: agent, definition: "explore" });
          for (let k = 0; k < 3; k++) {
            t += 600;
            push({ type: "agent_event", agent_id: agent, ev: { type: "tool_call", id: "s" + k, name: "grep", arguments: "{}" } });
            t += 300;
            push({ type: "agent_event", agent_id: agent, ev: { type: "tool_result", id: "s" + k, name: "grep", content: "", ms: 300 } });
          }
          push({ type: "agent_done", agent_id: agent, status: "done" });
        }
      }
      push({ type: "assistant_message", text: "Done.", finish_reason: "stop" });
    }
    evs.length = 10000;

    const out = {};
    const t0 = performance.now();
    ingest({ ...store.state, type: "state", busy: false });
    ingest({ type: "replay_start" });
    for (const ev of evs) ingest(ev);
    ingest({ type: "replay_done" });
    await frame(); await frame();
    out.replayToPaintMs = +(performance.now() - t0).toFixed(0);

    const table = document.getElementById("traj-table");
    const plot = document.getElementById("tj-plot");
    const box = plot.getBoundingClientRect();
    const wheel = (init) => plot.dispatchEvent(new WheelEvent("wheel",
      { bubbles: true, cancelable: true, clientY: box.top + 10, ...init }));
    out.scroll = await frames(120, (i) => { table.scrollTop = (i * 997) % table.scrollHeight; });
    out.zoom = await frames(120, (i) => wheel({ deltaY: i < 60 ? -120 : 120, clientX: box.left + box.width * 0.6 }));
    out.pan = await frames(120, (i) => wheel({ deltaX: i < 60 ? 80 : -80, clientX: box.left + 50 }));
    document.getElementById("traj-follow").click();
    let live = Date.now();
    out.liveAppend = await frames(120, (i) => {
      live += 400;
      ingest({ type: "tool_call", seq: ++seq, ts: iso(live), id: "live" + i, name: "read", arguments: "{}" });
      ingest({ type: "tool_result", seq: ++seq, ts: iso(live + 30), id: "live" + i, name: "read", content: "", ms: 30 });
    });
    out.rowNodes = document.querySelectorAll("#tj-rows > .tj-row:not([hidden])").length;
    out.barNodes = document.querySelectorAll(".tj-lane > i:not([hidden])").length;
    out.followedToEnd = table.scrollHeight - table.scrollTop - table.clientHeight < 30;
    if (out.rowNodes > 120) throw new Error(`table is not windowed: ${out.rowNodes} rows`);
    if (out.barNodes > 2400) throw new Error(`lanes are not windowed: ${out.barNodes} bars`);
    if (!out.followedToEnd) throw new Error("follow-live lost the newest row");
    return out;
  });
}
