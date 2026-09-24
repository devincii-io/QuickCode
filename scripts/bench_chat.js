// Run with Playwright's browser_run_code against workspace_smoke_server.py.
// The chat transcript's counterpart to bench_trajectory.js (which stubs chat
// out): replays the same synthetic 10k-event session into one agent pane with
// the side panel closed, then times live events, streaming, and scrolling back
// through the history. Fails on structure — the transcript must stay windowed,
// follow the newest event only while the reader is at the bottom, and keep its
// cards, permissions, subagents and trace links wired — the timings are for
// reading.
async (page) => {
  const origin = "http://127.0.0.1:8769";
  await page.goto(`${origin}/#token=workspace-preview`);
  const pid = await page.evaluate(async () => {
    const res = await fetch("/api/projects", { headers: { "x-quickcode-token": "workspace-preview" } });
    const body = await res.json();
    return (body.projects || body)[0].id;
  });
  await page.setViewportSize({ width: 1500, height: 950 });
  await page.goto(`${origin}/?pane=1&project=${encodeURIComponent(pid)}#token=workspace-preview`);
  await page.locator("#input").waitFor();
  return page.evaluate(async () => {
    const { ingest, store } = await import("/js/store.js");
    const transcript = document.getElementById("transcript");
    const frame = () => new Promise((r) => requestAnimationFrame(r));
    const settle = async () => { await frame(); await frame(); };
    const stats = (d) => {
      d.sort((a, b) => a - b);
      const at = (q) => +d[Math.min(d.length - 1, Math.floor(q * d.length))].toFixed(2);
      return { p50: at(0.5), p95: at(0.95), max: at(1) };
    };
    // Frame times while `step` runs once per frame, and the synchronous cost
    // of `step` itself — what one live event costs before the browser paints.
    const frames = async (n, step) => {
      const d = [], sync = [];
      let last = performance.now();
      for (let i = 0; i < n; i++) {
        const s = performance.now();
        step(i);
        sync.push(performance.now() - s);
        await frame();
        const now = performance.now();
        d.push(now - last);
        last = now;
      }
      return { frame: stats(d), sync: stats(sync) };
    };
    const fail = (msg) => { throw new Error(msg); };
    const gap = () => transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight;
    // The pane's own socket replays its (empty) conversation first; start
    // from the state it was handed.
    for (let i = 0; i < 600 && (!store.state || store.replaying); i++) await frame();
    if (!store.state) fail("the pane never attached to its conversation");

    // The session bench_trajectory.js replays, event for event.
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
    await settle();
    out.replayToPaintMs = +(performance.now() - t0).toFixed(0);
    out.attachedBlocks = transcript.children.length;
    out.attachedNodes = transcript.getElementsByTagName("*").length;
    if (out.attachedNodes > 20000) fail(`transcript is not windowed: ${out.attachedNodes} nodes`);
    if (transcript.querySelector(".tool-dot.running")) fail("a replayed call still shows as running");
    // Smooth scrolling is animated; give the jump to the end time to land.
    for (let i = 0; i < 60 && gap() > 2; i++) await frame();
    if (gap() > 2) fail("the replay did not end at the newest event");

    // Live: a call and its result per frame, the reader at the bottom.
    let live = Date.now();
    out.liveCallAndResult = await frames(120, (i) => {
      live += 400;
      ingest({ type: "tool_call", seq: ++seq, ts: iso(live), id: "live" + i, name: "read", arguments: "{}" });
      ingest({ type: "tool_result", seq: ++seq, ts: iso(live + 30), id: "live" + i, name: "read", content: "", ms: 30 });
    });
    for (let i = 0; i < 60 && gap() > 2; i++) await frame();
    if (gap() > 2) fail("live events did not follow the newest line");
    const lastCard = [...transcript.querySelectorAll(".tool-card")].at(-1);
    if (lastCard?.dataset.call !== "live119" || !lastCard.querySelector(".tool-dot.ok")) {
      fail("the last live result did not land on its card");
    }

    // Streaming: a delta per frame into the live bubble.
    out.streamDelta = await frames(120, (i) => {
      ingest({ type: "text_delta", text: i % 10 === 9 ? "\n\n" : "word " });
    });
    ingest({ type: "assistant_message", seq: ++seq, ts: iso(live), text: "streamed", finish_reason: "stop" });
    await settle();
    const settled = [...transcript.querySelectorAll(".msg-assistant")].at(-1);
    if (!settled?.textContent.includes("streamed") || transcript.textContent.includes("word word")) {
      fail("the settled message did not replace the streamed one");
    }

    // A permission, a subagent and a trace link, all found by id.
    ingest({ type: "tool_call", seq: ++seq, ts: iso(live), id: "gated", name: "bash", arguments: '{"command":"make"}' });
    ingest({ type: "permission_request", seq: ++seq, ts: iso(live), req_id: "r1", tool: "bash", arg: "make", call_id: "gated", agent: "main" });
    await frame();
    const gated = transcript.querySelector('.tool-card[data-call="gated"]');
    if (gated?.dataset.perm !== "pending") fail("the permission request missed its card");
    ingest({ type: "permission_resolved", seq: ++seq, ts: iso(live), req_id: "r1", allow: true, tool: "bash", arg: "make", call_id: "gated" });
    if (gated.dataset.perm !== "allowed") fail("the permission verdict missed its card");
    ingest({ type: "agent_spawned", seq: ++seq, ts: iso(live), agent_id: "late", definition: "explore" });
    ingest({ type: "agent_event", seq: ++seq, ts: iso(live), agent_id: "late", ev: { type: "tool_call", id: "s0", name: "grep", arguments: "{}" } });
    ingest({ type: "agent_event", seq: ++seq, ts: iso(live), agent_id: "late", ev: { type: "tool_result", id: "s0", name: "grep", content: "hit", ms: 5 } });
    const sub = transcript.querySelector('.agent-card[data-agent="late"] .tool-card[data-call="s0"]');
    if (!sub?.querySelector(".tool-dot.ok")) fail("a subagent's result missed its own card");

    // Scrolled up, the reader stays put while events arrive...
    await settle();
    transcript.scrollTo({ top: Math.max(0, transcript.scrollTop - 1500), behavior: "instant" });
    await settle();
    const held = transcript.scrollTop;
    for (let i = 0; i < 5; i++) {
      ingest({ type: "system_note", seq: ++seq, ts: iso(live), text: "note " + i });
      await frame();
    }
    if (Math.abs(transcript.scrollTop - held) > 1) fail("a live event yanked a reader who had scrolled up");

    // ...and scrolling to the top reveals the history in chunks, keeping the
    // first visible block where it was.
    const revealed = [];
    for (let i = 0; i < 400 && transcript.querySelector(".chat-more"); i++) {
      const s = performance.now();
      const anchor = transcript.querySelector(".chat-more").nextElementSibling;
      transcript.scrollTo({ top: 0, behavior: "instant" });
      const before = anchor.getBoundingClientRect().top;
      await frame();                     // the scroll event brings the chunk back
      const after = anchor.getBoundingClientRect().top;
      await frame();
      const later = anchor.getBoundingClientRect().top;
      if (Math.abs(after - before) > 1 || Math.abs(later - before) > 1) {
        fail("revealing history moved the page");
      }
      if (anchor.previousElementSibling?.classList.contains("chat-more")) fail("scrolling up revealed nothing");
      revealed.push(performance.now() - s);
    }
    if (transcript.querySelector(".chat-more")) fail("scrolling up never reached the start");
    if (!transcript.querySelector(".sys-prompt")) fail("the first event is not in the transcript");
    out.revealChunk = revealed.length ? stats(revealed) : null;
    out.fullyRevealedNodes = transcript.getElementsByTagName("*").length;

    // The ⌕ trace link opens the inspector on its event.
    const link = transcript.querySelector(".tool-card .trace-link[data-seq]");
    link.click();
    await settle();
    if (document.getElementById("traj-detail").classList.contains("hidden")) {
      fail("a trace link did not open the inspector");
    }
    return out;
  });
}
