// Install — "where do the models come from", plus the other things that
// belong to the install rather than to any agent: the provider catalog, the
// appearance, and which version of QuickCode this is.
//
// The first three pages already existed and already worked, so they are reused
// exactly as they are (settings/general.js) rather than rewritten into the new
// shell for the sake of it. The quick-settings modal on the composer is a
// shortcut into the first of them and says so.
//
// Updates and Web search are the two later ones, and they are here rather than
// anywhere else for the same reason the others are: a version and a search
// engine are facts about the install, not about a project or a session.
// Updates is also the only page in QuickCode that describes an outbound network
// request QuickCode makes on its own, so it describes it in full.

import { esc, relTime } from "../util.js";
import { MODES } from "../modals.js";
import {
  renderAppearancePage, renderGeneralPage, renderModelsPage,
} from "../settings/general.js";
// ./search.js is the configuration view's own search box; this is the page
// that configures the agent's.
import { renderWebSearchPage } from "./websearch.js";

const TABS = [
  ["general", "Provider & defaults", renderGeneralPage],
  ["models", "Model catalog", renderModelsPage],
  ["search", "Web search", renderWebSearchPage],
  ["appearance", "Appearance", renderAppearancePage],
  ["updates", "Updates", renderUpdatesPage],
];

export function renderInstall(host, ctx, tab = "general") {
  const current = TABS.find(([id]) => id === tab) || TABS[0];
  host.innerHTML = `<div class="cfg-page-inner">
    <header class="cfg-head">
      <div class="cfg-crumbs">Install</div>
      <div class="cfg-head-main">
        <span class="k-sigil big" data-kind="provider">»</span>
        <h2>Install</h2>
      </div>
    </header>
    <div class="cfg-lede">Per install, not per project and not per session:
      the endpoint tokens come from, the key, the mode new sessions start in,
      which search engine the agent may ask, how the app looks, and which
      version of it this is.</div>
    <div class="seg cfg-tabs">${TABS.map(([id, label]) =>
      `<a href="#/config/install/${id}" class="${id === current[0] ? "active" : ""}"
        >${esc(label)}</a>`).join("")}</div>
    <div class="cfg-install-body"></div>
  </div>`;
  const body = host.querySelector(".cfg-install-body");
  return current[2](body, { api: ctx.api, modes: MODES });
}

// ---------------------------------------------------------------------------
// Updates
// ---------------------------------------------------------------------------

const HOURS = (s) => Math.round((s || 0) / 3600);

// One sentence per state, written from the same six-question habit the plugin
// cards use: what happened, and what it means for you.
const STATE_TEXT = {
  available: "A newer release is published.",
  current: "This is the newest published release.",
  incomparable: "There is a published release, but nothing to compare it to.",
  disabled: "Automatic checking is switched off.",
  unknown: "The last check did not complete.",
};

function versionRow(s) {
  const cls = s.state === "available" ? "upd-v-new" : "";
  return `<div class="upd-versions">
    <div class="upd-v"><span class="upd-v-label">Installed</span>
      <code class="upd-v-num">${esc(s.installed || "unknown")}</code></div>
    <div class="upd-v-arrow">→</div>
    <div class="upd-v ${cls}"><span class="upd-v-label">Latest release</span>
      <code class="upd-v-num">${esc(s.latest || "—")}</code></div>
  </div>`;
}

function releaseBlock(s) {
  const r = s.release;
  if (!r) return "";
  const when = r.published_at ? new Date(r.published_at).toLocaleDateString() : "";
  return `<div class="upd-release">
    <div class="upd-release-name">${esc(r.name || r.tag || "")}</div>
    <div class="upd-release-meta">${esc(r.tag || "")}${when ? ` · published ${esc(when)}` : ""}</div>
    <a class="k-link" href="${esc(r.html_url)}" target="_blank" rel="noreferrer noopener"
      >Read the release notes ↗</a>
  </div>`;
}

// What updating can honestly mean here. The detection is quoted rather than
// paraphrased: if the app cannot tell how it was installed it says so, and
// shows the manual route, instead of offering a button that cannot work.
function methodBlock(s) {
  const info = s.install || {};
  const label = {
    installer: "Windows installer",
    pip: "pip / uv package",
    source: "source checkout",
    unknown: "could not be determined",
  }[info.method] || info.method;
  const steps = (s.instructions || [])
    .map((line) => `<li>${esc(line)}</li>`).join("");
  return `<div class="upd-method">
    <div class="upd-method-head">How this copy was installed:
      <strong>${esc(label)}</strong></div>
    <div class="upd-method-detail">${esc(info.detail || "")}</div>
    ${steps ? `<ul class="upd-steps">${steps}</ul>` : ""}
  </div>`;
}

function downloadBlock(s) {
  if (s.state !== "available") return "";
  if (!s.downloadable) {
    return s.artifacts_note
      ? `<div class="upd-note">${esc(s.artifacts_note)}</div>` : "";
  }
  const asset = s.release?.installer?.name || "the installer";
  return `<div class="upd-download">
    <div class="upd-download-what">Downloading fetches
      <code>SHA256SUMS.txt</code> from the release first, then
      <code>${esc(asset)}</code>, hashing it as it arrives. A digest that does
      not match the published one is refused and the download is deleted —
      nothing is ever run on bytes that were not verified twice.</div>
    <div class="f-actions">
      <button class="btn primary" id="upd-download">⤓ Download and verify</button>
      <span class="set-flash" id="upd-dl-msg"></span>
    </div>
    <div id="upd-dl-result"></div>
  </div>`;
}

// The privacy statement is not a footnote here. This is the only request
// QuickCode makes on its own, so the setting that governs it says exactly what
// goes out, to whom, how often, and what does not go with it.
function settingBlock(s) {
  return `<div class="upd-setting">
    <label class="upd-toggle">
      <input type="checkbox" id="upd-auto" ${s.auto_check ? "checked" : ""}>
      <span>Check for updates automatically</span>
    </label>
    <div class="upd-setting-help">
      <p>When this is on, QuickCode asks github.com once every
        ${HOURS(s.interval_s)} hours at most whether a newer release exists.
        That is <em>the only request this app makes to the internet on its own
        initiative</em>; everything else it sends goes to the model provider
        you configured, because you asked it to.</p>
      <p>The request is a plain unauthenticated GET of
        <code>${esc(s.endpoint || "")}</code>. It carries no API key, no
        cookies, no identifier, no project path, no session or usage data and
        no version number — GitHub requires a User-Agent, so it gets the fixed
        string <code>QuickCode</code> and learns nothing else. There is no
        telemetry here and no second endpoint.</p>
      <p>Switching it off stops the request entirely. Nothing on this page will
        contact anything, and the button below will be the only way to ask.</p>
    </div>
  </div>`;
}

function statusBlock(s) {
  const when = s.checked_at ? relTime(s.checked_at) : "never";
  const bits = [`Last checked: ${when}`];
  if (s.cached) bits.push("this is the stored answer, not a fresh request");
  if (s.retry_after > Date.now() / 1000) {
    bits.push("backing off until GitHub's rate limit resets");
  }
  return `<div class="upd-when">${esc(bits.join(" · "))}</div>`;
}

// A failed check is silent everywhere else in the app. Here it is spelled out,
// including which failure it was, because "it just never tells me about
// updates" is the outcome that has to be impossible.
function errorBlock(s) {
  if (!s.error) return "";
  const advice = {
    offline: "Nothing is wrong with this install; github.com was simply not "
             + "reachable. It will try again on its own.",
    rate_limited: "This is per IP address and resets within the hour. Nothing "
                  + "was refused because of anything you did.",
    http: "GitHub answered, but not with a release.",
    malformed: "GitHub answered with something this version cannot read.",
  }[s.error_kind] || "";
  return `<div class="upd-error">
    <div class="upd-error-head">The check did not complete</div>
    <div>${esc(s.error)}</div>
    ${advice ? `<div class="upd-error-advice">${esc(advice)}</div>` : ""}
  </div>`;
}

export async function renderUpdatesPage(c, { api }) {
  c.innerHTML = `<div class="set-page"><div class="set-loading">Reading the
    installed version…</div></div>`;

  const paint = (s) => {
    c.innerHTML = `<div class="set-page upd-page">
      <div class="set-lede">Which QuickCode this is, whether there is a newer
        one, and what "update" can honestly mean for the way this copy was
        installed.</div>
      <div class="upd-state upd-state-${esc(s.state)}">
        <span class="upd-dot"></span>${esc(STATE_TEXT[s.state] || "")}</div>
      ${versionRow(s)}
      ${s.note ? `<div class="upd-note">${esc(s.note)}</div>` : ""}
      ${errorBlock(s)}
      ${releaseBlock(s)}
      ${methodBlock(s)}
      ${downloadBlock(s)}
      ${settingBlock(s)}
      <div class="f-actions">
        <button class="btn" id="upd-now">Check now</button>
        <span class="set-flash" id="upd-msg"></span>
      </div>
      ${statusBlock(s)}
    </div>`;
    wire(s);
  };

  const say = (id, text, kind = "ok") => {
    const node = c.querySelector(id);
    if (!node) return;
    node.className = `set-flash ${kind}`;
    node.textContent = text;
  };

  const wire = (s) => {
    c.querySelector("#upd-now").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      say("#upd-msg", "Asking github.com…", "ok");
      try {
        paint(await api.update(true));
      } catch (err) {
        btn.disabled = false;
        say("#upd-msg", "Could not run the check: " + err.message, "err");
      }
    });

    c.querySelector("#upd-auto").addEventListener("change", async (e) => {
      const on = e.currentTarget.checked;
      e.currentTarget.disabled = true;
      try {
        const next = await api.setUpdateCheck(on);
        paint(next);
        say("#upd-msg", on
          ? "Automatic checking is on."
          : "Automatic checking is off. Nothing will be sent.", "ok");
      } catch (err) {
        e.currentTarget.disabled = false;
        e.currentTarget.checked = !on;
        say("#upd-msg", "Could not save that: " + err.message, "err");
      }
    });

    const dl = c.querySelector("#upd-download");
    if (dl) dl.addEventListener("click", () => download(s, dl));
  };

  const download = async (s, btn) => {
    btn.disabled = true;
    say("#upd-dl-msg", "Fetching the checksums, then the installer…", "ok");
    let result;
    try {
      result = await api.downloadUpdate();
    } catch (err) {
      btn.disabled = false;
      // A checksum refusal is a 409 and deserves its own shape on screen: it
      // is not "the download failed", it is "the bytes were wrong and they
      // have been deleted".
      const mismatch = /^409:/.test(err.message || "");
      c.querySelector("#upd-dl-result").innerHTML = mismatch
        ? `<div class="upd-error upd-refused">
             <div class="upd-error-head">Refused — checksum mismatch</div>
             <div>${esc((err.message || "").replace(/^409:\s*/, ""))}</div>
             <div class="upd-error-advice">The file has been deleted. Nothing
               was run. Do not install this release by hand until you know why;
               download it from the release page and check it yourself.</div>
           </div>`
        : "";
      say("#upd-dl-msg", mismatch ? "" : "Download failed: " + err.message,
          mismatch ? "ok" : "err");
      return;
    }
    say("#upd-dl-msg", "Verified against the release's SHA256SUMS.txt.", "ok");
    c.querySelector("#upd-dl-result").innerHTML = `
      <div class="upd-verified">
        <div class="upd-verified-head">✓ Verified</div>
        <div class="upd-verified-file"><code>${esc(result.path)}</code></div>
        <div class="upd-verified-sum">SHA-256 <code>${esc(result.sha256)}</code></div>
        <div class="upd-verified-what">Running it starts
          <code>${esc(result.name)}</code> on this machine. It installs over
          the existing copy, keeps your configuration, and QuickCode will be
          restarted by you afterwards. The file is hashed once more against the
          digest above before it starts.</div>
        <div class="f-actions">
          <button class="btn primary" id="upd-run">▶ Run this installer</button>
          <span class="set-flash" id="upd-run-msg"></span>
        </div>
      </div>`;
    c.querySelector("#upd-run").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      try {
        await api.installUpdate(result.path, result.sha256);
        say("#upd-run-msg", "The installer is running. Close QuickCode when it "
                          + "asks you to.", "ok");
      } catch (err) {
        e.currentTarget.disabled = false;
        say("#upd-run-msg", "Refused: " + err.message, "err");
      }
    });
  };

  try {
    paint(await api.update());
  } catch (err) {
    c.innerHTML = `<div class="set-page"><div class="set-error">Could not read
      the update status: ${esc(err.message)}</div></div>`;
  }
}
