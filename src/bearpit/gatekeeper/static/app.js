/* Bearpit control panel — a small hash-routed SPA over the Gatekeeper REST API.
   No framework (the realm CSP blocks external hosts): a tiny el() DOM builder, a router, and one
   render function per page. State is deliberately thin — every page refetches on mount. */
"use strict";

/* ---------- tiny DOM builder ---------- */
function el(tag, props, ...kids) {
  const e = document.createElement(tag);
  if (props) for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    // value must be set as a PROPERTY, not an attribute — <textarea> ignores the value attribute,
    // so prefilling a textarea (persona/rubric/guidelines when editing) needs e.value.
    else if (k === "value") e.value = v;
    else if (v === true) e.setAttribute(k, "");
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return e;
}
const $ = (s, r = document) => r.querySelector(s);
const clear = (n) => { while (n.firstChild) n.removeChild(n.firstChild); };

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body instanceof FormData ? opts.body
      : opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 401) {
    // The console shell loads without a token (it has to, or there would be nowhere to present
    // one), so an arriving visitor with no cookie would otherwise see an empty page and a console
    // full of failures. Say what to do instead.
    showTokenGate();
    throw new Error("not authorised");
  }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch { /* non-json */ }
    throw new Error(detail);
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res.text();
}


// Shown when the API answers 401: this console is reachable but not yet authorised.
let authGated = false;
function showTokenGate() {
  if (authGated) return;
  authGated = true;   // read by render(): the shell it renders into no longer exists
  const box = el("div", { class: "panel", style: "max-width:640px;margin:60px auto" },
    el("h2", null, "This console needs its access token"),
    el("p", { class: "panel-sub" },
      "The control plane can start realms and spend money, so it is not open to anything that can reach the port."),
    el("p", { class: "inline-note", style: "margin-top:14px" }, "Open the URL that "),
    el("span", { class: "kbd", text: "pit serve" }),
    el("span", { class: "inline-note", text: " printed — it looks like " }),
    el("span", { class: "kbd", text: "http://127.0.0.1:8000/?token=…" }),
    el("p", { class: "inline-note", style: "margin-top:14px" },
      "Visiting it once stores a cookie and this address works afterwards. The token is in "),
    el("span", { class: "kbd", text: "~/.bearpit/api-token" }));
  const root = document.getElementById("shell") || document.body;
  root.replaceChildren(box);
}

/* ---------- formatters ---------- */
const esc = (s) => String(s ?? "");
const money = (n) => "$" + (Number(n) || 0).toFixed(Number(n) >= 1 ? 2 : 3);
const fmtTokens = (n) => {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e4) return (n / 1e3).toFixed(1) + "k";
  return n.toLocaleString();
};
// Agent name from its mxid. The localpart is `<realmId>-<agentId>` and BOTH can contain dashes, so
// strip the realm prefix rather than split on '-' (which mangled e.g. 'juror-a' -> 'a').
function nameOf(mxid, realmId) {
  const local = String(mxid || "").split(":")[0].replace(/^@/, "");
  if (realmId && local.startsWith(realmId + "-")) return local.slice(realmId.length + 1);
  return local.split("-").pop();  // best-effort fallback when the realm id isn't known
}
const shortName = (mxid) => nameOf(mxid);
// Spend/score/violation keys are already bare agent ids (e.g. 'juror-a'); only message senders are
// full mxids. Derive a name only from an mxid — a bare id is shown as-is (deriving mangles dashes).
const agentLabel = (key, realmId) => {
  const k = String(key || "");
  return (k.startsWith("@") || k.includes(":")) ? nameOf(k, realmId) : k;
};

// Per-agent message colors. Mirrors core/colors.py AGENT_COLOR_PALETTE (and its order) so a color
// auto-assigned client-side matches what the server resolves — the two only diverge in the brief
// window before a scenario edit is reloaded server-side, or for an archived realm whose package is
// gone, where a stable client fallback is exactly what we want.
const AGENT_COLOR_PALETTE = [
  "#e15759", "#4e9bd9", "#59a14f", "#eda13a", "#b07aa1", "#4bc0c0",
  "#e377c2", "#9c755f", "#7fbf4f", "#6a8ec9", "#d4a017", "#ba5fd0",
  "#52b3a4", "#c9739b",
];
// Build an {agentId: color} map from a scenario's ordered roster: each agent's explicit color, else
// the next palette color by position (matching resolve_agent_colors on the server).
function agentColorMap(agents) {
  const map = {};
  (agents || []).forEach((a, i) => {
    map[a.id] = a.color || AGENT_COLOR_PALETTE[i % AGENT_COLOR_PALETTE.length];
  });
  return map;
}

// Wrap an async click handler so the button disables + shows a pending label while it runs, then
// restores. Prevents the "nothing happened, so I clicked 5 times → 5 messages" double-submit.
function guard(pending, fn) {
  return async (e) => {
    const btn = e.currentTarget;
    if (btn.disabled) return;
    const saved = [...btn.childNodes];
    btn.disabled = true;
    if (pending) btn.textContent = pending;
    try { await fn(e); }
    finally { btn.disabled = false; if (pending) btn.replaceChildren(...saved); }
  };
}
function ago(ms) {
  if (!ms) return "—";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
const stateChip = (st) => el("span", { class: `chip state-${st}` }, el("span", { class: "dot" }), st || "—");

/* ---------- toasts ---------- */
function toast(title, body, kind = "") {
  const t = el("div", { class: `toast ${kind}` },
    el("div", { class: "t-title", text: title }),
    body && el("div", { class: "t-body", text: body }));
  $("#toasts").append(t);
  setTimeout(() => { t.style.opacity = "0"; t.style.transform = "translateX(30px)"; }, 3600);
  setTimeout(() => t.remove(), 4000);
}
const ok = (t, b) => toast(t, b, "ok");
const fail = (t, b) => toast(t, b || "", "err");

/* ---------- modal ---------- */
function modal({ title, body, actions, wide }) {
  const root = $("#modal-root");
  const close = () => { clear(root); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  const box = el("div", { class: "modal", style: wide ? "max-width:820px" : "" },
    el("div", { class: "modal-head" },
      el("h2", { text: title }),
      el("button", { class: "modal-x", onclick: close, "aria-label": "Close" }, "✕")),
    el("div", { class: "modal-body" }, body),
    actions && el("div", { class: "modal-foot" }, ...actions(close)));
  const scrim = el("div", { class: "modal-scrim", onclick: (e) => { if (e.target === scrim) close(); } }, box);
  clear(root); root.append(scrim);
  return close;
}

/* ---------- router ---------- */
const ROUTES = [];
const route = (re, fn) => ROUTES.push([re, fn]);
let POLLS = [];
function stopPolls() { POLLS.forEach(clearInterval); POLLS = []; }
function poll(fn, ms) { fn(); POLLS.push(setInterval(fn, ms)); }

async function render() {
  stopPolls();
  closeInfoPop();
  const hash = location.hash.replace(/^#/, "") || "/";
  const view = $("#view");
  // active nav
  const seg = hash === "/" ? "home" : (hash.split("/")[1] || "realms");
  if (authGated) return;   // the token gate owns the page now
  document.querySelectorAll(".nav-links a").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === seg));
  for (const [re, fn] of ROUTES) {
    const m = hash.match(re);
    if (m) {
      clear(view);
      view.append(el("div", { class: "loading" }, el("span", { class: "spinner" }), "loading"));
      try { const node = await fn(...m.slice(1)); clear(view); view.append(node); }
      catch (e) { clear(view); view.append(errorState(e.message)); }
      $("#main").scrollTop = 0; window.scrollTo(0, 0);
      return;
    }
  }
  clear(view); view.append(errorState("Page not found"));
}
window.addEventListener("hashchange", render);
window.addEventListener("load", async () => {
  try { await refreshCapacity(); } catch { /* 401 shows the token gate */ }
  render();
});

function errorState(msg) {
  return el("div", { class: "empty" },
    el("div", { class: "big" }, "⚠"),
    el("h3", { text: "Something went wrong" }),
    el("p", { class: "inline-note", text: msg }));
}

async function refreshCapacity() {
  try {
    const s = await api("/api/settings");
    $("#cap-label").textContent = `${s.active} / ${s.capacity} realms`;
    $("#cap-dot").classList.toggle("live", s.active > 0);
  } catch { /* server may be booting */ }
}

/* ---------- shared bits ---------- */
function pageHead(eyebrow, title, sub, actions) {
  return el("div", { class: "page-head" },
    el("div", null,
      el("div", { class: "eyebrow", text: eyebrow }),
      el("h1", { text: title }),
      sub && el("p", { class: "sub", text: sub })),
    actions && el("div", { class: "head-actions" }, ...actions));
}
function emptyState(glyph, title, sub, action) {
  return el("div", { class: "empty" },
    el("div", { class: "big" }, glyph),
    el("h3", { text: title }),
    el("p", { class: "inline-note", text: sub }),
    action && el("div", { style: "margin-top:18px" }, action));
}
function skillPill(ref, onClick) {
  const [src, name] = String(ref).split(":");
  return el("span", { class: `skill-pill ${src}`, onclick: onClick, title: "View skill" },
    el("span", { class: "src", text: src }), name || src);
}
async function showSkill(source, ref) {
  let s;
  try { s = await api(`/api/skills/${source}/${encodeURIComponent(ref)}`); }
  catch (e) { return fail("Couldn't load skill", e.message); }
  const files = (s.files && s.files.length) ? s.files : ["SKILL.md"];
  const pre = el("pre", { class: "skill-md", text: s.content });
  const cache = { "SKILL.md": s.content };
  const base = `/api/skills/${source}/${encodeURIComponent(ref)}`;
  let body = pre;
  if (files.length > 1) {  // Agent-Skills folder: show a file browser
    const list = el("div", { class: "skill-files" });
    const load = async (path, node) => {
      list.querySelectorAll(".skill-file").forEach((n) => n.classList.remove("active"));
      node.classList.add("active");
      if (cache[path] == null) {
        try { cache[path] = (await api(`${base}/file?path=${encodeURIComponent(path)}`)).content; }
        catch { cache[path] = "(binary or unreadable file — download the .zip to inspect)"; }
      }
      pre.textContent = cache[path];
    };
    files.forEach((f, i) => {
      const node = el("div", { class: "skill-file" + (i === 0 ? " active" : ""), title: f });
      node.onclick = () => load(f, node);
      list.append(node);
      node.textContent = f;
    });
    body = el("div", { class: "skill-browser" }, list, pre);
  }
  modal({
    title: ref, wide: true, body,
    actions: (close) => [
      el("a", { class: "btn ghost", href: `${base}/export` }, "↧ Export .zip"),
      el("button", { class: "btn ghost", onclick: close }, "Close")],
  });
}

/* ================= REALMS (live + recent runs) ================= */
/* ================= LANDING / HOME ================= */
// The signature: an isolated realm holding agent nodes meshed to a central referee, message
// pulses traveling the links. Injected as raw SVG (the el() helper builds HTML, not SVG-namespace).
const REALM_FIELD_SVG = `
<svg viewBox="0 0 420 320" class="rf" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <g class="rf-links">
    <line class="rf-link rf-flow" x1="70" y1="78" x2="210" y2="152"/>
    <line class="rf-link" x1="200" y1="50" x2="210" y2="152"/>
    <line class="rf-link rf-flow" x1="332" y1="82" x2="210" y2="152"/>
    <line class="rf-link" x1="108" y1="212" x2="210" y2="152"/>
    <line class="rf-link rf-flow" x1="326" y1="220" x2="210" y2="152"/>
    <line class="rf-link" x1="182" y1="270" x2="210" y2="152"/>
    <line class="rf-link" x1="70" y1="78" x2="108" y2="212"/>
    <line class="rf-link" x1="200" y1="50" x2="332" y2="82"/>
    <line class="rf-link" x1="326" y1="220" x2="182" y2="270"/>
  </g>
  <g class="rf-nodes">
    <circle class="rf-node iris" cx="70" cy="78" r="6"/>
    <circle class="rf-node teal" cx="200" cy="50" r="5"/>
    <circle class="rf-node iris" cx="332" cy="82" r="5"/>
    <circle class="rf-node teal" cx="108" cy="212" r="6"/>
    <circle class="rf-node amber ref" cx="210" cy="152" r="8.5"/>
    <circle class="rf-node iris" cx="326" cy="220" r="5"/>
    <circle class="rf-node teal" cx="182" cy="270" r="5"/>
  </g>
</svg>`;

route(/^\/$/, async () => {
  // live platform stats — best-effort, the page renders fine offline
  let realms = [], scenarios = [];
  try {
    const [rd, pd] = await Promise.all([api("/api/realms"), api("/api/packages")]);
    realms = Array.isArray(rd) ? rd : (rd.realms || []);
    scenarios = Array.isArray(pd) ? pd : (pd.packages || []);
  } catch { /* offline-friendly */ }
  const activeN = realms.filter((r) => r.active).length;
  const link = (href, label, cls) => el("a", { class: `btn ${cls || ""}`, href }, label);

  const field = el("div", { class: "realm-field" });
  field.innerHTML = REALM_FIELD_SVG;

  const hero = el("section", { class: "landing-hero" },
    el("div", { class: "hero-copy" },
      el("div", { class: "eyebrow", text: "Autonomous agent realms" }),
      el("h1", { class: "hero-title" }, "Give AI agents a world, a goal, ",
        el("span", { class: "acc", text: "and each other." })),
      el("p", { class: "hero-sub", text:
        "Define a project and a roster of agents. Bearpit runs them as independent, always-on "
        + "actors in an isolated realm — free to collaborate or compete — and referees the outcome. "
        + "You configure them at birth, then watch." }),
      el("div", { class: "hero-cta" },
        link("#/realms", "Watch the realms", "primary"),
        link("#/scenarios", "Browse scenarios", "ghost")),
      el("div", { class: "hero-signal" },
        el("span", { class: `sig-dot ${activeN ? "live" : ""}` }),
        el("span", { class: "mono-micro", text: activeN
          ? `${activeN} realm${activeN > 1 ? "s" : ""} live now`
          : "idle — no realms running" }))),
    el("div", { class: "hero-visual" }, field,
      el("div", { class: "hv-cap mono-micro", text: "one realm · many agents · a referee" })));

  const step = (n, t, d) => el("div", { class: "step" },
    el("div", { class: "step-n mono", text: n }), el("h3", { text: t }), el("p", { text: d }));
  const how = el("section", { class: "landing-band" },
    el("div", { class: "eyebrow", text: "How a realm runs" }),
    el("div", { class: "steps" },
      step("01", "Define", "A project — goals, rules, resources, how it ends — and a roster of "
        + "agents, each with a persona, model, budget and tools."),
      step("02", "Provision", "Every agent is sealed into its own container on an isolated network. "
        + "Your keys stay in the proxy; the realm can't reach your host."),
      step("03", "Run", "Agents act in parallel, always-on — talking over the bus, spending metered "
        + "budget, calling tools. No turns unless you impose them."),
      step("04", "Conclude", "The Warden watches for the ending you defined, freezes the realm, and "
        + "writes the verdict. Every message and event is chronicled.")));

  const principle = (ic, t, d) => el("div", { class: "principle" },
    el("div", { class: "pr-ic", text: ic }),
    el("div", null, el("h3", { text: t }), el("p", { text: d })));
  const why = el("section", { class: "landing-band" },
    el("div", { class: "eyebrow", text: "What makes a realm different" }),
    el("div", { class: "principles" },
      principle("◎", "Black-box sovereignty", "Agents are configured only at birth. After that you "
        + "influence by message and control by kill — never by reaching in."),
      principle("⇌", "Always-on & parallel", "No lockstep turns. Agents run concurrently, and speed "
        + "is a legitimate competitive advantage."),
      principle("⚔", "Collaborate or compete", "Cooperation, rivalry, alliances, even deception — "
        + "whatever the goals and the open surface allow."),
      principle("≡", "Everything chronicled", "Every message, tool call and spend lands in an "
        + "append-only log. Replay any run; settle any dispute.")));

  const stat = (v, l) => el("div", { class: "lstat" },
    el("div", { class: "lstat-v", text: String(v) }),
    el("div", { class: "lstat-l mono-micro", text: l }));
  const foot = el("section", { class: "landing-cta" },
    el("div", { class: "lstats" },
      stat(realms.length, "realms run"), stat(activeN, "live now"),
      stat(scenarios.length, "scenarios")),
    el("div", { class: "cta-actions" },
      el("h2", { text: "Start a realm" }),
      el("p", { class: "sub", text: "Launch a bundled scenario, or author your own roster." }),
      el("div", { class: "hero-cta" },
        link("#/scenarios", "Browse scenarios", "primary"),
        link("#/scenarios/new", "Author a scenario", "ghost"))));

  return el("div", { class: "landing" }, hero, how, why, foot);
});

route(/^\/realms$/, async () => {
  const [{ runs }, { packages }] = await Promise.all([api("/api/runs"), api("/api/packages")]);
  const live = runs.filter((r) => r.active);
  const recent = runs.filter((r) => !r.active).slice(0, 12);
  const wrap = el("div", null,
    pageHead("Live telemetry", "Realms", "Autonomous agent runs — watch them unfold, or launch a new one.",
      [el("button", { class: "btn primary", onclick: () => launchModal(packages) },
        el("span", { class: "ic" }, "▶"), "Launch run")]));

  wrap.append(el("div", { class: "mono-micro", style: "margin:4px 0 12px" },
    `${live.length} running · ${runs.length} total`));

  if (!runs.length) {
    wrap.append(emptyState("◎", "No runs yet",
      "Launch a scenario to spin up a realm and watch the agents act.",
      el("button", { class: "btn primary", onclick: () => launchModal(packages) }, "Launch a run")));
    return wrap;
  }
  if (live.length) {
    wrap.append(el("div", { class: "eyebrow", style: "margin:20px 0 10px", text: "● Running now" }));
    wrap.append(el("div", { class: "grid cols" }, ...live.map(runCard)));
  }
  wrap.append(el("div", { class: "eyebrow", style: "margin:26px 0 10px", text: "Recent" }));
  wrap.append(el("div", { class: "grid cols" }, ...recent.map(runCard)));
  return wrap;
});

function runCard(r) {
  return el("a", { class: "card hover", href: `#/realm/${encodeURIComponent(r.realm_id)}` },
    el("div", { class: "card-top" },
      el("div", null,
        el("div", { class: "mono-micro", text: r.scenario }),
        el("h3", { text: r.realm_id })),
      stateChip(r.state)),
    el("div", { class: "card-foot" },
      el("span", { class: "mono-micro", text: money(r.spend) }),
      r.outcome && el("span", { class: "tag", text: trunc(r.outcome, 26) }),
      el("span", { class: "spacer" }),
      el("span", { text: ago(r.updated) })));
}
const trunc = (s, n) => (String(s).length > n ? String(s).slice(0, n - 1) + "…" : String(s));

/* ================= REALM DETAIL (the live feed) ================= */
route(/^\/realm\/(.+)$/, async (id) => {
  id = decodeURIComponent(id);
  // best-effort: learn the referee + each agent's message color from the scenario
  let referee = null, agentColors = {};
  try {
    const scen = id.replace(/-[0-9a-f]{6}$/, "");
    const det = await api(`/api/packages/${encodeURIComponent(scen)}`);
    referee = det.referee;
    agentColors = agentColorMap(det.agents);
  } catch { /* scenario may be gone */ }

  const feedScroll = el("div", { class: "feed-scroll" });
  // reader is "pinned" to the bottom when within this many px of it; new messages only auto-scroll
  // while pinned, so scrolling up to read history is never interrupted.
  const PIN_SLACK = 48;
  const atBottom = () =>
    feedScroll.scrollHeight - feedScroll.scrollTop - feedScroll.clientHeight < PIN_SLACK;
  // shown when new messages arrive while the reader is scrolled up; click jumps to the latest
  const jumpPill = el("button", {
    class: "jump-latest hidden",
    onclick: () => { feedScroll.scrollTop = feedScroll.scrollHeight; jumpPill.classList.add("hidden"); },
  }, "↓ New messages");
  feedScroll.addEventListener("scroll", () => { if (atBottom()) jumpPill.classList.add("hidden"); });
  // two views: "conversation" (just what the agents say — the observer view) and "full" (every
  // turn-cue, skill-read and tool call, for debugging). CSS hides system/activity lines in
  // conversation mode, so switching is instant and keeps scroll position.
  const feed = el("div", { class: "feed mode-conversation" });
  const modeBtn = (m, label, hint) => el("button", {
    class: "feed-mode-btn" + (m === "conversation" ? " active" : ""), title: hint,
    onclick: (e) => {
      feed.classList.toggle("mode-conversation", m === "conversation");
      feed.classList.toggle("mode-full", m !== "conversation");
      [...e.currentTarget.parentElement.children].forEach((b) =>
        b.classList.toggle("active", b === e.currentTarget));
      feedScroll.scrollTop = feedScroll.scrollHeight;
    },
  }, label);
  feed.append(
    el("div", { class: "feed-bar" },
      el("span", { class: "dots" }, el("i"), el("i"), el("i")),
      el("span", { class: "mono-micro", text: `commons · ${id}` }),
      el("span", { style: "flex:1" }),
      el("div", { class: "feed-mode" },
        modeBtn("conversation", "Conversation", "Just the agents' messages"),
        modeBtn("full", "Full log", "Every turn cue, skill read and tool call"))),
    feedScroll, jumpPill);
  const stats = el("div");
  const layout = el("div", { class: "realm-layout" }, feed, stats);

  const head = el("div");
  const banner = el("div");  // prominent outcome once the realm concludes
  const wrap = el("div", null,
    el("div", { class: "crumb" }, el("a", { href: "#/realms" }, "Realms"), "›",
      el("span", { class: "mono", text: id })),
    head, banner, layout);

  let seen = 0, lastState = null, lastOutcome = null;
  async function tick() {
    let status, tr;
    try { [status, tr] = await Promise.all([api(`/api/realms/${encodeURIComponent(id)}`),
      api(`/api/realms/${encodeURIComponent(id)}/transcript?limit=400`)]); }
    catch (e) { return; }
    // header (only rebuild on state change)
    if (status.state !== lastState) {
      lastState = status.state;
      clear(head); head.append(realmHead(id, status, referee));
    }
    // prominent outcome banner (only when it changes)
    if (status.outcome !== lastOutcome) {
      lastOutcome = status.outcome;
      clear(banner);
      if (status.outcome) banner.append(el("div", { class: "outcome-banner" },
        el("span", { class: "outcome-ic" }, "🏁"),
        el("div", null, el("div", { class: "mono-micro", text: "Outcome" }),
          el("div", { class: "outcome-text", text: status.outcome }))));
    }
    // side stats
    clear(stats); stats.append(realmStats(status, id));
    // feed: append only new lines
    const msgs = tr.messages || [];
    if (seen === 0 && !msgs.length) {
      feedScroll.append(el("div", { class: "feed-empty" }, "Waiting for the first message…"));
    }
    const firstLoad = seen === 0;
    if (firstLoad) clear(feedScroll);
    const channels = tr.channels || {};
    // capture pin state BEFORE appending — the new lines change scrollHeight
    const wasPinned = firstLoad || atBottom();
    for (const m of msgs.slice(seen))
      feedScroll.append(feedLine(m, referee, id, channels, agentColors));
    if (msgs.length > seen) {
      seen = msgs.length;
      if (wasPinned) feedScroll.scrollTop = feedScroll.scrollHeight;
      else jumpPill.classList.remove("hidden");  // reader is up in history — don't yank them down
    }
    refreshCapacity();
  }
  poll(tick, 2000);
  return wrap;
});

// Classify a message so the Conversation view can hide the noise:
//  operator = an injected operator message · system = turn cues / round announcements / kickoff
//  activity = a Hermes narration line (skill read, tool call, progress, interruption) · chat = the
//  actual thing an agent said. Conversation view shows only chat + operator.
function msgKind(m) {
  const s = m.sender || "", b = m.body || "";
  if (/^\s*\[operator/i.test(b)) return "operator";
  if (/(^|@)system/i.test(s)) return "system";
  if (/^[*\s>]*(📚|⚙️?|🛠️?|🔧|⏳|🔌|📖|🗂️?|↻)/u.test(b)) return "activity";
  if (/^[*\s>]*(Reading skill|mcp[_-]|Working\s*[—–-]|Interrupting current task|Operation interrupted)/i
    .test(b)) return "activity";
  return "chat";
}

function feedLine(m, referee, realmId, channels, agentColors) {
  const name = nameOf(m.sender, realmId);
  const kind = msgKind(m);
  const isRef = referee && name === referee && kind === "chat";
  const role = kind === "operator" ? "operator" : kind === "system" ? "system"
    : isRef ? "referee" : "player";
  // tag a private DM thread (any channel that isn't the commons) so it's clear it wasn't public
  const label = channels && channels[m.channel];
  const dm = label && label !== "commons" ? label : null;
  // an agent's messages render in its assigned color; system/operator/activity lines stay neutral
  const color = kind === "chat" && agentColors ? agentColors[name] : null;
  const tint = color ? `color:${color}` : "";
  return el("div", { class: `feed-line ${role} kind-${kind}${dm ? " kind-dm" : ""}` },
    el("div", { class: "who", style: tint,
      text: kind === "operator" ? "operator" : kind === "system" ? "system" : name }),
    el("div", { class: "msg" },
      dm && el("span", { class: "dm-tag", title: `private: ${dm}` }, `🔒 ${dm}`),
      el("span", { style: tint }, m.body || "")));
}

function realmHead(id, s, referee) {
  const live = s.active && ["running", "provisioning", "concluding"].includes(s.state);
  const actions = el("div", { class: "head-actions" });
  if (live) {
    actions.append(el("button", { class: "btn", onclick: () => injectModal(id) },
      el("span", { class: "ic" }, "✎"), "Inject message"));
    actions.append(el("button", {
      class: "btn danger", onclick: guard("Stopping…", async () => {
        try { await api(`/api/realms/${encodeURIComponent(id)}/stop`, { method: "POST" });
          ok("Stopping realm", id); } catch (e) { fail("Stop failed", e.message); }
      })
    }, "■ Stop"));
  }
  if (!live && s.config && s.config.agents) {
    actions.append(el("button", { class: "btn", onclick: () => rerunModal(id, s.config) },
      el("span", { class: "ic" }, "↻"), "Run again"));
  }
  actions.append(el("a", { class: "btn ghost", href: `/api/realms/${encodeURIComponent(id)}/report`,
    target: "_blank" }, "Report ↗"));
  return el("div", { class: "page-head", style: "margin-bottom:18px" },
    el("div", null,
      el("div", { class: "eyebrow", text: referee ? `refereed by ${referee}` : "realm" }),
      el("h1", { text: id, style: "font-size:22px" })),
    actions);
}

function realmStats(s, realmId) {
  const box = el("div");
  const gen = el("div", { class: "side-stat" },
    el("h4", null, "Status"),
    statRow("State", stateChip(s.state)),
    statRow("Messages", el("span", { class: "v", text: s.messages })),
    statRow("Total spend", el("span", { class: "v big", text: money(s.total_spend) })),
    statRow("Total tokens", el("span", { class: "v big", text: fmtTokens(s.total_tokens || 0) })));
  if (s.outcome) gen.append(statRow("Outcome", el("span", { class: "v", text: s.outcome })));
  box.append(gen);
  // What this run was launched with (ADR-003). The bound prose is already in the transcript, but
  // reading a value back out of finished prose is guesswork — and comparing two runs of one
  // scenario is the whole point of parameters. An empty string is shown as "(empty)" rather than
  // omitted, because "ran with it blank" and "not a parameter here" are different facts.
  const runParams = s.config && s.config.parameters;
  if (runParams && Object.keys(runParams).length) {
    const pb = el("div", { class: "side-stat" }, el("h4", null, "Parameters"));
    for (const [k, v] of Object.entries(runParams)) {
      pb.append(statRow(k, el("span", { class: v === "" ? "v dim" : "v", text: v === "" ? "(empty)" : v })));
    }
    box.append(pb);
  }
  const spend = Object.entries(s.spend || {});
  if (spend.length) {
    const sb = el("div", { class: "side-stat" }, el("h4", null, "Spend by agent"));
    spend.sort((a, b) => b[1] - a[1]).forEach(([a, v]) =>
      sb.append(statRow(agentLabel(a, realmId), el("span", { class: "v", text: money(v) }))));
    box.append(sb);
  }
  const tokens = Object.entries(s.tokens || {});
  if (tokens.length) {
    const tb = el("div", { class: "side-stat" }, el("h4", null, "Tokens by agent"));
    tokens.sort((a, b) => b[1] - a[1]).forEach(([a, v]) =>
      tb.append(statRow(agentLabel(a, realmId), el("span", { class: "v", text: fmtTokens(v) }))));
    box.append(tb);
  }
  const scores = Object.entries(s.scores || {});
  if (scores.length) {
    const ruled = s.outcome != null;
    const sc = el("div", { class: "side-stat" },
      el("h4", null, ruled ? "Scores (final ruling)" : "Scores"));
    scores.sort((a, b) => b[1] - a[1]).forEach(([a, v]) =>
      sc.append(statRow(agentLabel(a, realmId), el("span", { class: "v", text: v }))));
    // Once ruled, `scores` is the referee's verdict board. If the raw SCORE-event log disagrees
    // (a duplicate/dropped score write), say so plainly rather than quietly showing one number.
    if (s.score_discrepancy) {
      const raw = Object.entries(s.score_ledger || {})
        .sort((a, b) => b[1] - a[1]).map(([a, v]) => `${agentLabel(a, realmId)} ${v}`).join(", ");
      sc.append(el("div", { class: "hint",
        style: "color:var(--amber,#c90);margin-top:4px;font-size:12px",
        text: `⚠ raw score log differs from the ruling: ${raw}` }));
    }
    box.append(sc);
  }
  if ((s.violations || []).length) {
    const vb = el("div", { class: "side-stat" }, el("h4", null, "Violations"));
    s.violations.forEach((v) =>
      vb.append(statRow(agentLabel(v.agent, realmId),
        el("span", { class: "v", style: "color:var(--rose)", text: v.reason }))));
    box.append(vb);
  }
  const cfg = realmConfig(s.config, realmId);
  if (cfg) box.append(...cfg);
  return box;
}
const statRow = (k, v) => el("div", { class: "stat-row" }, el("span", { class: "k", text: k }), v);
// long values (a termination chain, a mechanics list) do not fit beside their label in the rail —
// stack them instead of letting the two wrap into each other
const statRowWide = (k, v) => el("div", { class: "stat-row stacked" },
  el("span", { class: "k", text: k }), v);

// The configuration the run ACTUALLY used. Not the scenario file: the platform resolves each
// agent's model from its tier, raises the turn floor for a slow pipeline, and lifts a
// too-tight budget cap — so a scenario asking for "large / 120s / $2" may well have run as
// a large-tier model / 240s / $25. Every question you ask a finished run ("was it mention-gated?
// which model was the referee on? why did the floor pass so fast?") is about the RESOLVED values.
function realmConfig(c, realmId) {
  if (!c || !c.agents) return null;               // a run from before this was captured
  const cards = [];
  const t = c.turns;

  const run = el("div", { class: "side-stat" }, el("h4", null, "Run configuration"));
  run.append(statRow("Provider", el("span", { class: "v mono", text: c.provider || "—" })));
  run.append(statRow("Turn-taking", el("span", { class: "v", title: t
        ? `${t.policy} · ${t.enforcement} · order ${t.order} · referee cue: ${t.referee_cue}`
        : "no turns — every agent may post at any time",
      text: t ? `On · ${t.policy}` : "Off · free-for-all" })));
  if (t) {
    run.append(statRow("Turn floor", el("span", { class: "v mono",
      title: "how long a silent floor-holder is given before the floor passes",
      text: `${t.silence_timeout_s}s` })));
  }
  run.append(statRow("Replies", el("span", { class: "v", title: c.free_response
      ? "agents receive every message in the room"
      : "a participant only answers when @mentioned"
        + (c.referee_sees_all ? " — the referee is exempt and sees everything" : ""),
    text: c.free_response ? "Free response" : "Mention-gated" })));
  run.append(statRow("Referee", el("span", { class: "v",
    text: c.referee ? `${c.referee}${c.referee_opens ? " · drives" : " · reactive"}` : "none" })));
  const term = (c.termination || []).map((x) => x.type.replace("referee_verdict", "verdict")
    + (x.limit ? ` ${x.limit}` : "")).join(" · ");
  run.append(statRowWide("Ends on", el("span", { class: "v", text: term || "—" })));
  const mech = (c.mechanics || []).map((m) => m.kind + (m.ruleset ? ` (${m.ruleset})` : "")).join(" · ");
  const env = c.environment || {};
  const flags = [
    mech || null,
    c.provide_tools ? "realm tools" : null,
    env.shared_folder ? "shared folder" : null,
    env.allow_side_channels ? "side channels" : null,
    env.network_egress ? `egress: ${env.network_egress}` : null,
  ].filter(Boolean).join(" · ");
  run.append(statRowWide("Mechanics & environment", el("span", { class: "v", text: flags || "—" })));
  cards.push(run);

  const models = el("div", { class: "side-stat" }, el("h4", null, "Models & budgets"));
  (c.agents || []).forEach((a) => {
    const who = el("span", { class: "who", text: a.id });
    if (a.role === "referee") who.append(el("span", { class: "ref", text: "REFEREE" }));
    const meta = [
      a.model_category ? `${a.model_category} tier` : null,
      a.budget_usd != null ? `$${a.budget_usd}` : null,
    ].filter(Boolean).join(" · ");
    const rhs = el("div", { class: "rhs" },
      el("div", { class: "model", text: `${a.model || "—"}${a.effort ? " · " + a.effort : ""}` }),
      el("div", { class: "meta", text: meta || "" }));
    models.append(el("div", { class: "agent-cfg",
      title: `skills: ${(a.skills || []).join(", ") || "—"}`
        + `\nbudget: ${a.budget_usd != null ? "$" + a.budget_usd : "none"} (${a.on_exhausted})`
        + (a.private_messaging && a.private_messaging.enabled
            ? `\nprivate messages: ${a.private_messaging.peers.length
                ? a.private_messaging.peers.join(", ") : "any peer"}`
              + (a.private_messaging.max_per_round
                  ? ` · max ${a.private_messaging.max_per_round}/round` : "")
            : "") },
      who, rhs));
  });
  models.append(el("p", { class: "hint", style: "margin:8px 0 0;font-size:11px;line-height:1.5",
    text: "Resolved at launch — the model tier, turn floor and budget cap are rewritten for the "
        + "active provider, so they can differ from the scenario file." }));
  cards.push(models);
  return cards;
}

// Running it again is TWO different things, and conflating them is how you "reproduce" a bug
// against code that no longer has it. So the choice is put in front of the user, with the
// consequence spelled out — never a bare "re-run" button that quietly picks one.
function rerunModal(id, cfg) {
  const pkg = cfg && cfg.package;
  const go = async (mode, close) => {
    try {
      const r = await api(`/api/realms/${encodeURIComponent(id)}/rerun?mode=${mode}`,
        { method: "POST" });
      close();
      ok(mode === "snapshot" ? "Replaying exact run" : "Running with latest", r.realm_id);
      location.hash = `#/realm/${r.realm_id}`;
    } catch (e) { fail("Could not start", e.message); }
  };
  const opt = (title, lines) => el("div", { class: "rerun-opt" },
    el("h4", { text: title }),
    el("ul", null, ...lines.map((l) => el("li", { text: l }))));

  modal({
    title: "Run this scenario again",
    body: el("div", null,
      opt("Replay this exact run", [
        "The same models, efforts and budgets — even if you have since switched provider.",
        "The scenario as it was THEN: personas, rubrics and rules are restored from a snapshot.",
        "Later edits to the scenario file are NOT picked up.",
        "Use this to reproduce a result.",
      ]),
      opt("Run with latest", [
        pkg ? `Reloads the scenario from ${pkg} — any edits you have made are picked up.`
            : "Reloads the scenario from its package — any edits you have made are picked up.",
        "Resolves models and budgets against the settings that are active NOW.",
        "This is a DIFFERENT run — use it to test a fix.",
      ]),
      el("p", { class: "hint", style: "margin:12px 0 0",
        text: "Either way this launches a new realm and spends tokens." })),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", { class: "btn", disabled: !pkg,
        title: pkg ? "" : "the scenario this ran from is no longer on record",
        onclick: guard("Starting…", (e) => go("latest", close)) }, "Run with latest"),
      el("button", { class: "btn primary",
        onclick: guard("Starting…", (e) => go("snapshot", close)) }, "↻ Replay exact run"),
    ],
  });
}

async function injectModal(id) {
  // recipient list = the realm's active agents (derived from who has posted)
  let agents = [];
  try {
    const tr = await api(`/api/realms/${encodeURIComponent(id)}/transcript?limit=400`);
    const seen = new Map();
    for (const m of tr.messages || []) {
      const s = m.sender || "";
      if (s.startsWith("@") && !/(^|@)system/i.test(s) && !seen.has(s)) seen.set(s, nameOf(s, id));
    }
    agents = [...seen].map(([mxid, name]) => ({ mxid, name })).sort((a, b) => a.name.localeCompare(b.name));
  } catch { /* fall back to all-only */ }
  const sel = el("select", null, el("option", { value: "" }, "All agents"),
    ...agents.map((a) => el("option", { value: a.mxid }, a.name)));
  const ta = el("textarea", { placeholder: "A message from the operator…", maxlength: 4000 });
  const note = el("p", { class: "inline-note", style: "margin-top:0" });
  const setNote = () => { note.textContent = sel.value
    ? "Posted to the shared commons (all agents can see it) but @mentioned/addressed only to the "
      + "chosen agent. Influence, not control — they may or may not act on it."
    : "Posted to the shared commons and @mentioned to every agent. Influence, not control — they "
      + "may or may not act on it."; };
  sel.onchange = setNote; setNote();
  modal({
    title: "Inject operator message",
    body: el("div", null,
      el("div", { class: "field" }, el("label", null, "To"), sel),
      note,
      el("div", { class: "field" }, ta)),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", {
        class: "btn primary", onclick: guard("Sending…", async () => {
          if (!ta.value.trim()) return fail("Message required", "Type something to send.");
          const body = { text: ta.value };
          if (sel.value) body.to = sel.value;
          try { await api(`/api/realms/${encodeURIComponent(id)}/msg`, { method: "POST", body });
            ok("Message injected", sel.value ? `to ${nameOf(sel.value, id)}` : "to all agents");
            close(); } catch (e) { fail("Inject failed", e.message); }
        })
      }, "Send")],
  });
}

/* ================= LAUNCH RUN ================= */
/* ---------- scenario parameters (ADR-003), client side ----------
   One parser, two readers: the browse-surface preview and the editor's live panel. Keeping them
   on the same function is what stops the two from disagreeing about the notation. */
function parsePlaceholders(text) {
  const out = [];
  if (typeof text !== "string" || !text.includes("${")) return out;
  text.replace(/\$\$\{|\$\{((?:\\.|[^\\}])*)\}/g, (m, body) => {
    if (body === undefined) return m;                       // $${ escape
    const parts = [];
    let buf = "";
    for (let i = 0; i < body.length; i++) {
      const c = body[i];
      if (c === "\\" && i + 1 < body.length) { buf += body[++i]; continue; }
      if (c === "," && parts.length < 2) { parts.push(buf); buf = ""; continue; }
      buf += c;
    }
    parts.push(buf);
    const name = parts[0].trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return m;
    const dflt = parts.length > 1 && parts[1] !== "" ? parts[1] : null;
    const desc = parts.length > 2 && parts[2].trim() !== "" ? parts[2].trim() : null;
    out.push({ name, default: dflt, description: desc });
    return m;
  });
  return out;
}

/* Substitute for DISPLAY. A scenario's stored prose keeps its placeholders — that is the source,
   and the editor shows it raw. Everywhere a reader is just browsing, raw syntax is noise: the card
   for param-relay read "A ONE-round ${category,fruit,What kind of word the players relay} relay
   for ${team_name,,A label...}". Use the default where there is one, and mark the rest with its
   own name so a reader can still see that something varies here. */
function paramPreview(text) {
  if (typeof text !== "string" || !text.includes("${")) return text;
  return text.replace(/\$\$\{|\$\{((?:\\.|[^\\}])*)\}/g, (m, body) => {
    if (body === undefined) return "${";
    const found = parsePlaceholders(m);
    if (!found.length) return m;
    return found[0].default !== null ? found[0].default : `\u00ab${found[0].name}\u00bb`;
  });
}

/* Every parameter in the draft being edited, in first-appearance order, with where each is used.
   Mirrors the server's scan (core/params.py) over the same prose fields — never ids, model refs,
   budgets or termination patterns. */
function draftParameters(S) {
  const seen = new Map();
  const add = (path, text) => {
    for (const ph of parsePlaceholders(text || "")) {
      let p = seen.get(ph.name);
      if (!p) { p = { name: ph.name, default: null, description: null, used: [] }; seen.set(ph.name, p); }
      if (ph.default !== null && p.default === null) p.default = ph.default;
      if (ph.description && !p.description) p.description = ph.description;
      if (!p.used.includes(path)) p.used.push(path);
    }
  };
  add("description", S.metadata.description);
  (S.spec.goals || []).forEach((g, i) => add(`goal ${i + 1}`, g));
  add("guidelines", S.spec.guidelines);
  add("restrictions", S.spec.restrictions);
  for (const a of S.agents || []) {
    const who = a.id || "agent";
    add(`${who}: description`, a.description);
    (a.goals || []).forEach((g, i) => add(`${who}: goal ${i + 1}`, g));
    (a.responsibilities || []).forEach((r, i) => add(`${who}: responsibility ${i + 1}`, r));
    add(`${who}: persona`, a.persona);
    add(`${who}: rubric`, a.rubric);
  }
  // spec.parameters overrides the inline default and description, so the panel must show the
  // EFFECTIVE value — otherwise the editor says one thing and the run does another. param-relay
  // reads ${seed_word,APPLE} in its prose while the manifest sets MANGO, and MANGO is what runs.
  const declared = (S.spec && S.spec.parameters) || {};
  for (const p of seen.values()) {
    const d = declared[p.name];
    if (!d) continue;
    if (d.default !== undefined && d.default !== null) {
      if (p.default !== null && String(d.default) !== p.default) p.overrides = p.default;
      p.default = String(d.default);
    }
    if (d.description) p.description = d.description;
    if (d.choices) p.choices = d.choices;
  }
  return [...seen.values()];
}

function launchModal(packages, preselect) {
  if (!packages.length) { fail("No scenarios", "Create a scenario first."); return; }
  const sel = el("select", null, ...packages.map((p) =>
    el("option", { value: p.path, selected: p.name === preselect }, `${p.title || p.name} (${p.agents ?? "?"} agents)`)));
  const nameByPath = Object.fromEntries(packages.map((p) => [p.path, p.name]));
  const rid = el("input", { type: "text", placeholder: "auto (scenario-name-xxxxxx)" });
  const turnsOn = el("input", { type: "checkbox" });
  const timeout = el("input", { type: "number", value: "90", min: "10", style: "max-width:110px" });
  const freeResp = el("input", { type: "checkbox" });

  // ---- scenario parameters (ADR-003). Built from the scenario, refreshed when it changes.
  const paramBox = el("div", { class: "param-box" });
  let inputs = {};            // name -> element
  let params = [];            // as returned by the API
  let consented = false;      // "Launch anyway" was pressed once

  function renderParams() {
    paramBox.innerHTML = "";
    inputs = {};
    consented = false;
    if (!params.length) return;
    const required = params.filter((p) => p.required).length;
    paramBox.append(el("div", { class: "param-head" },
      el("span", { class: "eyebrow" }, "Parameters"),
      el("span", { class: "hint" },
        `${params.length} for this scenario${required ? ` · ${required} with no default` : ""}`)));
    for (const p of params) {
      let input;
      if (p.choices && p.choices.length) {
        input = el("select", null, ...p.choices.map((c) =>
          el("option", { value: c, selected: c === p.default }, c)));
      } else if (p.multiline) {
        input = el("textarea", { rows: "3" });
        input.value = p.default || "";
      } else {
        const attrs = { type: p.type === "int" || p.type === "number" ? "number" : "text" };
        if (p.min !== null && p.min !== undefined) attrs.min = String(p.min);
        if (p.max !== null && p.max !== undefined) attrs.max = String(p.max);
        if (p.type === "int") attrs.step = "1";
        if (p.required) attrs.placeholder = "required — leave empty to run without it";
        input = el("input", attrs);
        input.value = p.default || "";
      }
      inputs[p.name] = input;
      const label = el("label", null, p.name,
        p.required ? el("span", { class: "req" }, " required") : null);
      const bits = [];
      if (p.description) bits.push(el("div", { class: "hint" }, p.description));
      // The known cost of letting the manifest win is an override the author cannot see in the
      // prose. Say it out loud, right where the value is chosen.
      if (p.overridden) {
        bits.push(el("div", { class: "hint warn" },
          `default ${JSON.stringify(p.default)} set in the manifest, overriding `
          + `${JSON.stringify(p.inline_default)} written inline`));
      }
      if (p.used_in && p.used_in.length) {
        const shown = p.used_in.slice(0, 3).join(", ");
        bits.push(el("div", { class: "hint dim" },
          `used in: ${shown}${p.used_in.length > 3 ? ` +${p.used_in.length - 3} more` : ""}`));
      }
      paramBox.append(el("div", { class: "field param" }, label, input, ...bits));
    }
  }

  async function loadParams() {
    const name = nameByPath[sel.value];
    params = [];
    paramBox.innerHTML = "";
    if (!name) { return; }
    try {
      const r = await api(`/api/packages/${encodeURIComponent(name)}/parameters`);
      params = r.parameters || [];
    } catch (e) {
      paramBox.append(el("div", { class: "hint warn" }, `parameters: ${e.message}`));
      return;
    }
    renderParams();
  }
  sel.onchange = loadParams;

  modal({
    title: "Launch a run",
    body: el("div", null,
      el("div", { class: "field" }, el("label", null, "Scenario"), sel),
      paramBox,
      el("div", { class: "field" }, el("label", null, "Realm id ",
        el("span", { class: "hint" }, "optional")), rid),
      el("div", { class: "field" },
        el("label", { class: "check" }, turnsOn, "Force turn-taking (one agent at a time)")),
      el("div", { class: "field", style: "margin-left:26px" },
        el("label", { class: "hint" }, "Silence timeout (s)"), timeout),
      el("div", { class: "field" },
        el("label", { class: "check" }, freeResp, "Free-response (don't force replies to @mentions)"))),
    actions: (close) => {
      const btn = el("button", {
        class: "btn primary", onclick: guard("Launching…", async () => {
          const body = { package: sel.value, free_response: freeResp.checked };
          if (rid.value.trim()) body.realm_id = rid.value.trim();
          if (turnsOn.checked) body.turns = { enabled: true, silence_timeout_s: Number(timeout.value) || 90 };
          const values = {};
          for (const [name, input] of Object.entries(inputs)) {
            const v = (input.value ?? "").trim();
            if (v !== "") values[name] = v;
          }
          if (Object.keys(values).length) body.parameters = values;
          // Warn once, in place, before spending money on prose with holes in it.
          const empty = params.filter((p) => p.required && !values[p.name]);
          if (empty.length && !consented) {
            consented = true;
            paramBox.prepend(el("div", { class: "param-warn" },
              el("strong", null, `${empty.length} parameter${empty.length > 1 ? "s" : ""} will be empty: `),
              empty.map((p) => p.name).join(", "),
              el("div", { class: "hint" }, "Press Launch anyway to continue.")));
            // guard() snapshots the button's children up front and restores them in its
            // `finally`, so relabelling inline is silently undone — the banner said "press
            // Launch anyway" while the button still read "Launch". Apply after that restore.
            setTimeout(() => btn.replaceChildren("Launch anyway"), 0);
            return;
          }
          if (empty.length) body.allow_missing_parameters = true;
          try {
            const r = await api("/api/realms", { method: "POST", body });
            close(); ok("Realm launched", r.realm_id);
            location.hash = `#/realm/${encodeURIComponent(r.realm_id)}`;
          } catch (e) { fail("Launch failed", e.message); }
        })
      }, el("span", { class: "ic" }, "▶"), "Launch");
      return [el("button", { class: "btn ghost", onclick: close }, "Cancel"), btn];
    },
  });
  loadParams();
}

/* ================= SCENARIO EDITOR ================= */
const EGRESS = ["none", "model_only", "allowlist", "open"];
const ROLES = ["participant", "referee"];
const MODEL_CATEGORIES = ["small", "medium", "large"];
const ON_EXHAUSTED = ["starve", "starve_then_kill", "kill"];
const CUES = ["round", "turn", "none"];
const TERM_TYPES = ["manual", "message", "duration", "referee_verdict", "stall", "budget_exhausted"];

// Plain-language explanations shown by the ⓘ next to each property.
const INFO = {
  parameters: "Any prose in this scenario can carry a placeholder, and each one becomes a field on the launch form so it can be set per run without editing the scenario.\n\n${name}  -  asked for at launch; no default, so you are warned before running with it empty\n${name,default}  -  pre-filled with default\n${name,default,description}  -  the description is shown under the field\n${name,,description}  -  a description with no default\n$${name}  -  a literal ${name}, not a placeholder\n\nWorks in: this description, goals, guidelines, restrictions, and every agent's description, goals, responsibilities, persona and rubric. NOT in ids, model names, budgets, or termination patterns - a termination pattern is a regular expression, where ${x} already means something else.\n\nAdd spec.parameters in the JSON to give one a picker (choices), a number range, or a textarea. A default set there overrides the one written inline.",
  name: "The scenario's display name. Lowercased and dashed to form its id (the folder name and "
    + "the prefix on every realm launched from it).",
  category: "A high-level grouping used to organize and filter scenarios (e.g. Games, Debate). "
    + "Free-form — reuse an existing one or coin a new one.",
  author: "Who created this scenario. Shown for provenance; optional.",
  description: "A one-line summary shown on the scenario card and detail page.",
  tags: "Free-form keywords for search and discovery. Press Enter to add each.",
  modelCategory: "The capability tier this agent asks for: small, medium, or large. The active "
    + "model pipeline (set on the Settings page) maps each tier to a concrete model + reasoning "
    + "effort, so scenarios stay provider-agnostic — switch pipelines without editing any agent.",
  goals: "The realm's objectives, quoted to every agent at kickoff — what success looks like.",
  guidelines: "Shared conduct rules quoted to all agents at the start: how they should behave.",
  restrictions: "Rules that are 'law' — forbidden but technically possible. The referee penalizes "
    + "violations rather than the system blocking them (vs. 'physics', which is impossible).",
  egress: "What the agents' containers can reach on the network. none = offline; model_only = only "
    + "the model proxy; allowlist = proxy plus named hosts; open = full internet.",
  visibility: "How much agents know about each other. full = names + roles; anonymous = opaque "
    + "handles; hidden = they discover peers only through interaction.",
  sharedFolder: "Give agents a shared /realm/shared directory to read and write files — a surface "
    + "for both collaboration and sabotage.",
  sideChannels: "Let agents open private direct-message rooms with each other, not just the public "
    + "commons.",
  requireMention: "On: an agent only responds when @mentioned (orderly; avoids ack storms). Off: "
    + "agents reply by relevance (free-for-all).",
  refereeOpens: "The referee drives the realm — it posts first and the engine cues it to move things "
    + "forward each round. Off: a reactive referee just watches and judges.",
  provideTools: "Expose the realm's MCP tools (sealed submit / tally, etc.) to agents. Turn off for "
    + "scenarios where those tools would only confuse them.",
  stallNudge: "If the realm goes quiet, the system posts a gentle nudge to restart activity.",
  turns: "Physics one-at-a-time turns: only the current floor-holder (plus the referee + system) may "
    + "post; out-of-turn messages are blocked. Off: all agents act in parallel.",
  silenceTimeout: "How long to wait for the floor-holder to post before skipping it (seconds). "
    + "Mini-models can be slow to respond, so keep this generous.",
  refereeCue: "When the engine nudges the referee. round = each completed round; turn = every turn; "
    + "none = the referee checks turn state on its own.",
  minRounds: "Require this many full rounds before the referee may end the realm (0 = no minimum).",
  retire: "Drop a participant from the rotation after this many consecutive fully-silent turns "
    + "(0 = never; skip but keep). For crashed, stuck, or eliminated agents.",
  termType: "How the realm ends. manual = operator stop only; message = a matching message is posted; "
    + "duration = a wall-clock limit; referee_verdict = the referee declares the end; "
    + "stall = no agent has spoken for a set idle time (a stuck session); "
    + "budget_exhausted = agent budgets run out.",
  termPattern: "The text that ends the realm when a message contains it (e.g. GAME OVER).",
  termChannel: "Which channel to watch for the pattern (default: commons).",
  termLimit: "Wall-clock limit before the realm ends, e.g. 30m or 2h.",
  termStall: "End the realm when NO agent has posted for this long — a deterministic catch for a "
    + "stuck session (e.g. a player stops responding and everyone else waits). e.g. 5m.",
  agentId: "The agent's stable identifier (lowercase) — used in its realm handle and folder.",
  agentName: "A friendly name shown in the UI and to other agents.",
  role: "participant = a normal agent. referee = a privileged host/judge with a rubric, read-all "
    + "powers, and (when turn-based) the ability to eliminate participants.",
  provider: "The model provider, routed through the proxy (e.g. azure).",
  model: "The exact model or deployment name (e.g. gpt-5.4-mini).",
  apiKeyRef: "Which stored key (by name) the proxy uses for this agent. Keys live encrypted in the "
    + "keystore and never enter the container.",
  inputPrice: "What the model charges per 1,000,000 input (prompt) tokens, in USD — e.g. 0.15. Used "
    + "to track spend against the budget.",
  outputPrice: "What the model charges per 1,000,000 output (completion) tokens, in USD — e.g. 0.60.",
  budget: "Hard USD spend cap for this agent. Requires the token prices above so spend can be "
    + "measured and enforced.",
  onExhausted: "What happens when the budget runs out. starve = model calls start failing; "
    + "starve_then_kill = starve, then stop after the grace period; kill = stop immediately.",
  gracePeriod: "For starve_then_kill: how long to keep the agent alive after its budget is spent, "
    + "e.g. 5m.",
  skills: "SKILL.md briefs that give the agent its role and capabilities. Click a skill to read it.",
  persona: "The agent's private character brief (persona.md). Only this agent sees it.",
  rubric: "The referee's private judging criteria — how it scores or decides. Only the referee sees "
    + "it.",
  agentGoals: "The agent's private objectives. Only this agent sees them.",
};

function blankState() {
  return {
    metadata: { name: "", description: "", author: "", category: "", tags: [] },
    spec: {
      goals: [], guidelines: "", restrictions: "", parameters: {},
      environment: { network_egress: "model_only",
        shared_folder: false, require_mention: true, allow_side_channels: false },
      referee_opens: false, provide_tools: true, stall_nudge: false, turns: null,
      termination: [{ type: "manual" }],
    },
    agents: [],
  };
}
function detailToState(d) {
  const e = d.environment || {};
  return {
    metadata: { name: d.title || d.name, description: d.description || "", author: d.author || "",
      category: d.category || "", tags: d.tags || [] },
    spec: {
      goals: d.goals || [], guidelines: d.guidelines || "", restrictions: d.restrictions || "",
      // Carried verbatim: the editor has no UI for these (choices, types, manifest defaults are
      // JSON-level), so it must hand back exactly what it was given rather than dropping them.
      parameters: d.parameters || {},
      environment: { network_egress: e.network_egress || "model_only",
        shared_folder: !!e.shared_folder,
        require_mention: e.require_mention !== false, allow_side_channels: !!e.allow_side_channels },
      referee_opens: !!d.referee_opens, provide_tools: d.provide_tools !== false,
      stall_nudge: !!d.stall_nudge,
      turns: d.turns ? { silence_timeout_s: d.turns.silence_timeout_s ?? 90,
        referee_cue: d.turns.referee_cue || "round",
        min_rounds_before_verdict: d.turns.min_rounds_before_verdict ?? 0,
        retire_after_misses: d.turns.retire_after_misses ?? 0 } : null,
      termination: (d.termination || []).map((t) => ({ ...t })),
    },
    agents: (d.agents || []).map((a) => {
      const br = a.budget_ref || {};
      return {
        id: a.id, name: a.name || a.id, role: a.role || "participant",
        // the capability tier resolved by the active pipeline; model_ref is an optional exact
        // override that round-trips but isn't edited in the basic form
        model_category: a.model_category || "medium",
        model_ref: a.model_ref || null,
        budget: { max_usd: br.max_usd ?? null, on_exhausted: br.on_exhausted || "starve_then_kill",
          grace_period: br.grace_period ?? null },
        private_messaging: { enabled: !!(a.private_messaging || {}).enabled,
          include_referee: !!(a.private_messaging || {}).include_referee },
        skills: a.skills || [], persona: a.persona || "", rubric: a.rubric || "", goals: a.goals || [],
        color: a.color || null,
      };
    }),
  };
}

route(/^\/scenarios\/(new)$/, () => editorPage(null, false));
route(/^\/scenarios\/edit\/(.+)$/, (name) => editorPage(decodeURIComponent(name), false));
route(/^\/scenarios\/clone\/(.+)$/, (name) => editorPage(decodeURIComponent(name), true));

async function editorPage(name, clone) {
  const [{ skills }, settings, { packages }] = await Promise.all([
    api("/api/skills"), api("/api/settings"), api("/api/packages")]);
  const keyRefs = settings.api_key_refs || [];
  const categories = [...new Set(packages.map((p) => p.category).filter(Boolean))].sort();
  // Three modes: new (blank, POST), clone (load -> rename "X copy" -> POST), edit (load -> PUT
  // under the same name). Editing a bundled example writes to the user dir, so it shadows the
  // read-only original rather than mutating examples/.
  let S, isNew, srcName = name, shadowsExample = false;
  if (name) {
    const detail = await api(`/api/packages/${encodeURIComponent(name)}`);
    const entry = packages.find((p) => p.name === name);
    S = detailToState(detail);
    if (clone) {
      S.metadata.name = S.metadata.name + " copy"; isNew = true; srcName = null;
    } else {
      isNew = false; shadowsExample = !(entry && entry.editable);
    }
  } else { S = blankState(); isNew = true; }

  const heading = isNew ? (name ? "Clone scenario" : "New scenario") : `Edit · ${srcName}`;
  const sub = shadowsExample
    ? "Editing a bundled example — saving creates your own copy that shadows the original (the "
      + "bundled template is never changed; delete your copy to get it back)."
    : "Everything an agent knows is set here — after launch, agents are black boxes.";
  const form = el("div");
  const wrap = el("div", null,
    el("div", { class: "crumb" }, el("a", { href: "#/scenarios" }, "Scenarios"), "›",
      el("span", { text: isNew ? (name ? "Clone" : "New") : "Edit" })),
    pageHead("Authoring", heading, sub,
      [el("button", { class: "btn ghost", onclick: () => { location.hash = "#/scenarios"; } }, "Cancel"),
       el("button", { class: "btn primary", onclick: guard("Saving…", save) },
         el("span", { class: "ic" }, "✔"), "Save scenario")]),
    form);
  form.append(overviewPanel(S, categories), rulesPanel(S), parametersPanel(S),
    envPanel(S), turnsPanel(S),
    terminationPanel(S), rosterPanel(S, skills, keyRefs));

  async function save() {
    if (!S.metadata.name.trim()) return fail("Name required", "Give the scenario a name.");
    if (!S.agents.length) return fail("Add an agent", "A scenario needs at least one agent.");
    // the UI keeps shared_folder as a plain bool; the schema wants a {enabled} object
    const body = JSON.parse(JSON.stringify(S));
    body.spec.environment.shared_folder = { enabled: !!body.spec.environment.shared_folder };
    try {
      if (isNew) await api("/api/packages", { method: "POST", body });
      else await api(`/api/packages/${encodeURIComponent(srcName)}`, { method: "PUT", body });
      ok("Scenario saved", S.metadata.name); location.hash = "#/scenarios";
    } catch (e) { fail("Save failed", e.message); }
  }
  return wrap;
}

/* ----- info icon: click the ⓘ next to a label to read what a property does ----- */
let _infoPop = null;
function closeInfoPop() {
  if (_infoPop) { _infoPop.el.remove(); document.removeEventListener("mousedown", _infoPop.onDoc, true);
    _infoPop = null; }
}
function toggleInfoPop(anchor, text) {
  const sameAnchor = _infoPop && _infoPop.anchor === anchor;
  closeInfoPop();
  if (sameAnchor) return;  // clicking the open icon again closes it
  const pop = el("div", { class: "info-pop", text });
  document.body.append(pop);
  const r = anchor.getBoundingClientRect();
  pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + "px";
  pop.style.top = (r.bottom + 6 + pop.offsetHeight > window.innerHeight
    ? Math.max(8, r.top - pop.offsetHeight - 6) : r.bottom + 6) + "px";
  const onDoc = (e) => { if (!pop.contains(e.target) && e.target !== anchor) closeInfoPop(); };
  document.addEventListener("mousedown", onDoc, true);
  _infoPop = { el: pop, anchor, onDoc };
}
function infoIcon(text) {
  return el("span", { class: "info-ic", role: "button", tabindex: "0", "aria-label": "What is this?",
    onclick: (e) => { e.preventDefault(); e.stopPropagation(); toggleInfoPop(e.currentTarget, text); },
    onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault();
      toggleInfoPop(e.currentTarget, text); } } }, "i");
}
function fieldLabel(text, opts = {}) {
  return el("label", null, text,
    opts.hint && el("span", { class: "hint", text: opts.hint }),
    opts.info && infoIcon(opts.info));
}

/* ----- editor panels ----- */
function textField(label, obj, key, opts = {}) {
  const inp = el(opts.area ? "textarea" : "input",
    { type: opts.type || "text", value: obj[key] ?? "", placeholder: opts.ph || "",
      maxlength: opts.maxlength, min: opts.min, max: opts.max, step: opts.step,
      class: opts.mono ? "mono" : "", oninput: (e) => {
        obj[key] = opts.type === "number" ? Number(e.target.value) : e.target.value;
        if (opts.on) opts.on(obj[key]);  // let callers react (e.g. live-update the agent header)
      } });
  if (opts.area && opts.rows) inp.rows = opts.rows;
  return el("div", { class: "field", style: opts.style || "" },
    fieldLabel(label, opts), inp);
}
function selectField(label, obj, key, options, opts = {}) {
  const sel = el("select", { onchange: (e) => { obj[key] = e.target.value; opts.onchange && opts.onchange(); } },
    ...options.map((o) => el("option", { value: o, selected: obj[key] === o }, o)));
  return el("div", { class: "field", style: opts.style || "" },
    fieldLabel(label, opts), sel);
}
// Per-agent message color: a native swatch to pin an explicit color, plus "Auto" to clear it back
// to the roster-order default (a null color, which the server/feed auto-assign).
function colorField(a) {
  const note = el("span", { class: "inline-note", text: a.color ? "" : "auto (by roster order)" });
  const swatch = el("input", { type: "color", class: "color-swatch", value: a.color || "#9aa0a6",
    oninput: (e) => { a.color = e.target.value; note.textContent = ""; } });
  const autoBtn = el("button", { type: "button", class: "btn xs", onclick: () => {
    a.color = null; swatch.value = "#9aa0a6"; note.textContent = "auto (by roster order)"; } }, "Auto");
  return el("div", { class: "field" },
    fieldLabel("Message color", { info: "The color this agent's messages render in on the realm "
      + "page. Leave on Auto for a distinct color assigned automatically by roster order." }),
    el("div", { style: "display:flex;gap:8px;align-items:center" }, swatch, note, autoBtn));
}
function checkField(label, obj, key, opts = {}) {
  return el("div", { class: "field", style: "display:flex;align-items:center;flex-wrap:wrap" },
    el("label", { class: "check" },
      el("input", { type: "checkbox", checked: !!obj[key],
        onchange: (e) => { obj[key] = e.target.checked; opts.onchange && opts.onchange(); } }), label),
    opts.info && infoIcon(opts.info));
}
// USD price per 1,000,000 tokens (how model pricing is quoted) <-> the schema's per-token float.
// Per-token is capped at 1 in the schema, so per-1M is capped at 1,000,000.
function priceField(label, model, key, ph, info) {
  const inp = el("input", { type: "number", step: "any", min: "0", max: "1000000", placeholder: ph,
    value: model[key] != null ? +(model[key] * 1e6).toFixed(6) : "",
    oninput: (e) => { const v = e.target.value.trim();
      model[key] = v === "" ? null : Number(v) / 1e6; } });
  return el("div", { class: "field" }, fieldLabel(label, { info, hint: "USD" }), inp);
}
function listField(label, arr, ph, info, itemMax, maxItems) {
  const list = el("div", { class: "pill-list", style: "margin-bottom:8px" });
  const draw = () => {
    clear(list);
    arr.forEach((v, i) => list.append(el("span", { class: "skill-pill" }, v,
      el("span", { style: "cursor:pointer;color:var(--faint)", onclick: () => { arr.splice(i, 1); draw(); } }, " ✕"))));
    if (!arr.length) list.append(el("span", { class: "inline-note", text: "none yet" }));
  };
  draw();
  const inp = el("input", { type: "text", placeholder: ph || "Type and press Enter", maxlength: itemMax,
    onkeydown: (e) => { if (e.key === "Enter" && e.target.value.trim()) { e.preventDefault();
      if (maxItems && arr.length >= maxItems) { fail("Too many", `Up to ${maxItems} allowed.`); return; }
      arr.push(e.target.value.trim()); e.target.value = ""; draw(); } } });
  return el("div", { class: "field" }, fieldLabel(label, { info }), list, inp);
}

function overviewPanel(S, categories = []) {
  // Category is a free-typed field with existing values suggested (datalist) so a project can
  // reuse a category or coin a new one.
  const dlId = "cat-suggestions";
  const catInput = el("input", { type: "text", value: S.metadata.category ?? "", list: dlId,
    maxlength: 60, placeholder: "e.g. Games", oninput: (e) => { S.metadata.category = e.target.value; } });
  const catField = el("div", { class: "field" },
    fieldLabel("Category", { hint: "for grouping & filtering", info: INFO.category }),
    catInput, el("datalist", { id: dlId }, ...categories.map((c) => el("option", { value: c }))));
  return el("div", { class: "panel" }, el("h2", null, "Overview"),
    el("p", { class: "panel-sub" }, "Its name becomes the scenario id."),
    el("div", { class: "row" },
      textField("Name", S.metadata, "name", { ph: "sealed-auction", info: INFO.name, maxlength: 120 }),
      catField,
      textField("Author", S.metadata, "author", { ph: "optional", info: INFO.author, maxlength: 120 })),
    textField("Description", S.metadata, "description",
      { area: true, rows: 2, ph: "One line on what this is.", info: INFO.description, maxlength: 2000 }),
    listField("Tags", S.metadata.tags, "Add a tag", INFO.tags, 40, 30));
}
function rulesPanel(S) {
  return el("div", { class: "panel" }, el("h2", null, "Rules & goals"),
    el("p", { class: "panel-sub" }, "Shared context every agent is born with."),
    listField("Goals", S.spec.goals, "A goal for the realm", INFO.goals, 1000, 50),
    textField("Guidelines", S.spec, "guidelines", { area: true, rows: 3, info: INFO.guidelines,
      maxlength: 50000, ph: "How agents should behave. Quoted to everyone at kickoff." }),
    textField("Restrictions", S.spec, "restrictions", { area: true, rows: 2, info: INFO.restrictions,
      maxlength: 50000, ph: "Rules that are law (forbidden-but-possible, referee-penalized)." }));
}
function parametersPanel(S) {
  // Live, because this IS the documentation: an author discovers the feature by typing `${` and
  // watching a field appear. A static help box would be read once and never again.
  const body = el("div");
  const panel = el("div", { class: "panel" },
    el("h2", null, "Parameters", infoIcon(INFO.parameters)),
    el("p", { class: "panel-sub" },
      "Placeholders in this scenario's prose. Each becomes a field on the launch form, so one "
      + "scenario can be run many ways without editing it."),
    body);

  function refresh() {
    const found = draftParameters(S);
    body.innerHTML = "";
    if (!found.length) {
      body.append(el("div", { class: "param-empty" },
        el("div", null, "None yet. Write a placeholder into any prose field to make one:"),
        el("code", { class: "param-eg" }, "Reach ${target,10,Points needed to win} points"),
        el("div", { class: "hint" },
          "The name is required; the default and description are optional. "
          + "Click the i above for the full syntax.")));
      return;
    }
    const required = found.filter((p) => p.default === null).length;
    body.append(el("div", { class: "hint", style: "margin-bottom:10px" },
      `${found.length} parameter${found.length > 1 ? "s" : ""}`
      + (required ? ` · ${required} with no default, so the launcher will warn` : "")));
    for (const p of found) {
      body.append(el("div", { class: "param-row" },
        el("div", null,
          el("code", { class: "param-name" }, p.name),
          p.default === null
            ? el("span", { class: "req" }, " no default")
            : el("span", { class: "hint" }, ` = ${p.default}`)),
        p.overrides && el("div", { class: "hint warn" },
          `set in spec.parameters, overriding ${JSON.stringify(p.overrides)} written inline`),
        p.description && el("div", { class: "hint" }, p.description),
        p.choices && el("div", { class: "hint dim" }, `choices: ${p.choices.join(" | ")}`),
        el("div", { class: "hint dim" }, `used in: ${p.used.join(", ")}`)));
    }
  }
  refresh();
  // The form mutates state in place rather than re-rendering (which would steal focus mid-word),
  // so recompute on input, debounced.
  let timer = null;
  panel.addEventListener("bearpit:draft-changed", refresh);
  document.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => { if (panel.isConnected) refresh(); }, 250);
  });
  return panel;
}

function envPanel(S) {
  const e = S.spec.environment;
  return el("div", { class: "panel" }, el("h2", null, "Environment"),
    el("p", { class: "panel-sub" }, "The physics of the realm — enforced at the four boundaries."),
    el("div", { class: "row" },
      selectField("Network egress", e, "network_egress", EGRESS, { info: INFO.egress }),),
    el("div", { class: "row" },
      el("div", { class: "field" },
        checkField("Shared folder (agents can read/write files)", e, "shared_folder", { info: INFO.sharedFolder })),
      el("div", { class: "field" },
        checkField("Allow side-channels (private DMs)", e, "allow_side_channels", { info: INFO.sideChannels }))),
    checkField("Require @mention to reply (off = free-response by relevance)", e, "require_mention",
      { info: INFO.requireMention }),
    el("div", { class: "row" },
      el("div", { class: "field" },
        checkField("Referee opens the realm (drives it)", S.spec, "referee_opens", { info: INFO.refereeOpens })),
      el("div", { class: "field" },
        checkField("Provide realm tools (MCP) to agents", S.spec, "provide_tools", { info: INFO.provideTools }))),
    checkField("Nudge on stall", S.spec, "stall_nudge", { info: INFO.stallNudge }));
}
function turnsPanel(S) {
  const body = el("div");
  const draw = () => {
    clear(body);
    if (!S.spec.turns) { body.append(el("p", { class: "inline-note",
      text: "Free-for-all: all agents act in parallel. Turn off to sequence them one at a time." })); return; }
    const t = S.spec.turns;
    body.append(el("div", { class: "row" },
      textField("Silence timeout (s)", t, "silence_timeout_s",
        { type: "number", info: INFO.silenceTimeout, min: 1, max: 86400 }),
      selectField("Referee cue", t, "referee_cue", CUES, { info: INFO.refereeCue })),
      el("div", { class: "row" },
        textField("Min rounds before verdict", t, "min_rounds_before_verdict",
          { type: "number", info: INFO.minRounds, min: 0, max: 10000 }),
        textField("Retire after N misses", t, "retire_after_misses",
          { type: "number", hint: "0 = never", info: INFO.retire, min: 0, max: 1000 })));
  };
  const toggle = el("label", { class: "check" },
    el("input", { type: "checkbox", checked: !!S.spec.turns,
      onchange: (e) => { S.spec.turns = e.target.checked
        ? { silence_timeout_s: 90, referee_cue: "round", min_rounds_before_verdict: 0, retire_after_misses: 0 }
        : null; draw(); } }), "Turn-based (one agent at a time — physics)");
  draw();
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", null, "Turns"),
      el("span", { style: "display:inline-flex;align-items:center" }, toggle, infoIcon(INFO.turns))),
    body);
}
function terminationPanel(S) {
  const list = el("div");
  const draw = () => {
    clear(list);
    S.spec.termination.forEach((t, i) => {
      const row = el("div", { class: "agent-block" });
      const body = el("div", { class: "body" });
      const fields = () => {
        clear(body);
        body.append(selectField("Type", t, "type", TERM_TYPES, { onchange: fields, info: INFO.termType }));
        if (t.type === "message") body.append(el("div", { class: "row" },
          textField("Pattern (text that ends it)", t, "pattern",
            { ph: "GAME OVER", mono: true, info: INFO.termPattern, maxlength: 500 }),
          textField("Channel", t, "channel", { ph: "commons", info: INFO.termChannel, maxlength: 100 })));
        if (t.type === "duration") body.append(textField("Limit", t, "limit",
          { ph: "30m or 2h", mono: true, info: INFO.termLimit, maxlength: 20 }));
        if (t.type === "stall") body.append(textField("Max idle time", t, "limit",
          { ph: "5m", mono: true, info: INFO.termStall, maxlength: 20 }));
      };
      fields();
      row.append(el("summary", null,
        el("span", { class: "agent-avatar" }, "⏹"),
        el("b", { text: t.type }),
        el("span", { class: "disclose" }, "›"),
        el("button", { class: "btn sm danger", style: "margin-left:8px",
          onclick: (ev) => { ev.preventDefault(); S.spec.termination.splice(i, 1); draw(); } }, "remove")),
        body);
      row.setAttribute("open", "");
      list.append(row);
    });
    if (!S.spec.termination.length) list.append(el("p", { class: "inline-note",
      text: "No end condition — the realm runs until stopped." }));
  };
  draw();
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", null, "Termination"),
      el("button", { class: "btn sm", onclick: () => { S.spec.termination.push({ type: "message",
        content_match: "GAME OVER" }); draw(); } }, "＋ Add condition")),
    el("p", { class: "panel-sub" }, "When the realm ends."), list);
}
function rosterPanel(S, skills, keyRefs) {
  const list = el("div");
  const draw = () => {
    clear(list);
    // referee first for display parity with the rest of the app
    S.agents.forEach((a, i) => list.append(agentBlock(S, a, i, skills, keyRefs, draw)));
    if (!S.agents.length) list.append(el("p", { class: "inline-note",
      text: "No agents yet — add at least one." }));
  };
  const addAgent = (role) => {
    // a new agent picks a capability tier (small/medium/large); the active pipeline resolves it to
    // a concrete model + costs at launch, so no per-agent model config is needed here.
    S.agents.push({ id: "", name: "", role,
      model_category: role === "referee" ? "large" : "medium", model_ref: null,
      budget: { max_usd: 2.0, on_exhausted: "starve_then_kill", grace_period: "5m" },
      private_messaging: { enabled: false, include_referee: false },
      skills: role === "referee" ? ["builtin:referee-basics"] : ["builtin:agent-basics"],
      persona: "", rubric: "", goals: [] });
    draw();
  };
  draw();
  return el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", null, "Roster"),
      el("div", { style: "display:flex;gap:8px" },
        el("button", { class: "btn sm", onclick: () => addAgent("referee") }, "＋ Referee"),
        el("button", { class: "btn sm primary", onclick: () => addAgent("participant") }, "＋ Agent"))),
    el("p", { class: "panel-sub" }, "The agents that inhabit the realm. Each is configured only here."),
    list);
}
// The agent's private-messaging permission: a main checkbox + a cross-linked "include referee"
// sub-option (checking the sub implies the main; unchecking the main clears the sub — matching the
// schema's include_referee-requires-enabled rule).
function privateMsgControl(a) {
  const pm = a.private_messaging || (a.private_messaging = { enabled: false, include_referee: false });
  const refBox = el("input", { type: "checkbox", checked: !!pm.include_referee, disabled: !pm.enabled });
  const enBox = el("input", { type: "checkbox", checked: !!pm.enabled });
  const sub = el("label", { class: "check", style: `margin-left:26px;opacity:${pm.enabled ? 1 : 0.45}` },
    refBox, "…including the referee");
  enBox.onchange = () => {
    pm.enabled = enBox.checked; refBox.disabled = !enBox.checked;
    if (!enBox.checked) { pm.include_referee = false; refBox.checked = false; }
    sub.style.opacity = enBox.checked ? 1 : 0.45;
  };
  refBox.onchange = () => {
    pm.include_referee = refBox.checked;
    if (refBox.checked && !enBox.checked) { pm.enabled = true; enBox.checked = true;
      refBox.disabled = false; sub.style.opacity = 1; }
  };
  return el("div", { class: "field" },
    el("label", { class: "check" }, enBox, "Allow private messages to other agents",
      infoIcon("Give this agent private DM channels with peers. A room is pre-created for each "
        + "allowed pair; messages are private between the two agents but the operator can observe "
        + "them (platform-brokered, so they're captured in the log).")),
    sub);
}

function agentBlock(S, a, i, skills, keyRefs, redraw) {
  const isRef = a.role === "referee";
  const skillWrap = el("div", { class: "pill-list", style: "margin-bottom:8px" });
  const drawSkills = () => {
    clear(skillWrap);
    a.skills.forEach((s, si) => skillWrap.append(el("span", { class: `skill-pill ${s.split(":")[0]}` },
      el("span", { onclick: () => showSkill(...s.split(":")) }, s),
      el("span", { style: "cursor:pointer;color:var(--faint)", onclick: () => { a.skills.splice(si, 1); drawSkills(); } }, " ✕"))));
    if (!a.skills.length) skillWrap.append(el("span", { class: "inline-note", text: "no skills" }));
  };
  drawSkills();
  const skillPicker = el("select", null,
    el("option", { value: "" }, "add skill…"),
    ...skills.map((s) => el("option", { value: `${s.source}:${s.ref}` }, `${s.source}:${s.ref}`)));
  skillPicker.onchange = (e) => { if (e.target.value && !a.skills.includes(e.target.value)) {
    a.skills.push(e.target.value); drawSkills(); } e.target.value = ""; };

  // the summary's avatar + name mirror the Id/Display-name fields as you type
  const avatar = el("span", { class: `agent-avatar ${isRef ? "ref" : ""}` }, (a.name || a.id || "?").slice(0, 2));
  const nameEl = el("b", { text: a.name || a.id || "unnamed" });
  const refreshHead = () => {
    avatar.textContent = (a.name || a.id || "?").slice(0, 2);
    nameEl.textContent = a.name || a.id || "unnamed";
  };
  const body = el("div", { class: "body" },
    el("div", { class: "row" },
      textField("Id", a, "id",
        { ph: "vela", mono: true, hint: "lowercase", info: INFO.agentId, on: refreshHead, maxlength: 64 }),
      textField("Display name", a, "name",
        { ph: "Vela", info: INFO.agentName, on: refreshHead, maxlength: 100 }),
      selectField("Role", a, "role", ROLES, { onchange: redraw, info: INFO.role })),
    el("div", { class: "row" },
      selectField("Model category", a, "model_category", MODEL_CATEGORIES,
        { info: INFO.modelCategory }),
      colorField(a),
      el("div", { class: "field" }, fieldLabel("Resolves via", {}),
        el("div", { class: "inline-note", style: "padding-top:8px",
          text: "the active pipeline (Settings → Model pipeline)" }))),
    el("div", { class: "row" },
      textField("Budget (USD)", a.budget, "max_usd",
        { type: "number", info: INFO.budget, min: 0, max: 1000000 }),
      selectField("On exhausted", a.budget, "on_exhausted", ON_EXHAUSTED, { info: INFO.onExhausted }),
      textField("Grace period", a.budget, "grace_period", { ph: "5m", info: INFO.gracePeriod, maxlength: 20 })),
    privateMsgControl(a),
    el("div", { class: "field" }, fieldLabel("Skills", { info: INFO.skills }), skillWrap, skillPicker),
    textField("Persona", a, "persona", { area: true, rows: 3, info: INFO.persona, maxlength: 50000,
      ph: "The agent's private character brief (persona.md)." }),
    isRef && textField("Rubric", a, "rubric", { area: true, rows: 3, info: INFO.rubric, maxlength: 50000,
      ph: "The referee's private judging rubric." }),
    listField("Goals", a.goals, "A private goal", INFO.agentGoals, 1000, 50));

  const block = el("details", { class: "agent-block" },
    el("summary", null,
      avatar,
      el("div", null, nameEl,
        el("span", { class: `chip mini ${isRef ? "role-referee" : ""}`, style: "margin-left:8px" }, a.role)),
      el("span", { class: "disclose" }, "›"),
      el("button", { class: "btn sm danger", style: "margin-left:auto",
        onclick: (e) => { e.preventDefault(); S.agents.splice(i, 1); redraw(); } }, "✕")),
    body);
  if (!a.id) block.setAttribute("open", "");
  return block;
}

/* ================= SCENARIOS ================= */
route(/^\/scenarios$/, async () => {
  const { packages } = await api("/api/packages");
  const wrap = el("div", null,
    pageHead("Authoring", "Scenarios", "Design a project, its rules, and a roster of agents.",
      [el("button", { class: "btn ghost", onclick: () => importModal() }, el("span", { class: "ic" }, "↥"), "Import"),
       el("button", { class: "btn ghost", onclick: () => { location.hash = "#/scenarios/assist-new"; } },
         el("span", { class: "ic" }, "✎"), "Create with assistant"),
       el("button", { class: "btn primary", onclick: () => { location.hash = "#/scenarios/new"; } },
         el("span", { class: "ic" }, "＋"), "New scenario")]));
  if (!packages.length) {
    wrap.append(emptyState("◈", "No scenarios yet", "Create one from scratch or import a folder / .zip.",
      el("button", { class: "btn primary", onclick: () => { location.hash = "#/scenarios/new"; } }, "New scenario")));
    return wrap;
  }
  const categories = [...new Set(packages.map((p) => p.category).filter(Boolean))].sort();
  const grid = el("div", { class: "grid cols" });
  let q = "", cat = "";
  const apply = () => {
    const ql = q.trim().toLowerCase();
    const shown = packages.filter((p) =>
      (!cat || p.category === cat) &&
      (!ql || (p.title || p.name).toLowerCase().includes(ql) || p.name.toLowerCase().includes(ql)
        || (p.tags || []).some((t) => t.toLowerCase().includes(ql))));
    clear(grid);
    if (shown.length) shown.forEach((p) => grid.append(scenarioCard(p)));
    else grid.append(el("div", { class: "inline-note", style: "padding:34px 4px",
      text: "No scenarios match your filters." }));
    count.textContent = `${shown.length} of ${packages.length}`;
  };
  const search = el("input", { type: "text", placeholder: "Search by name…", style: "max-width:260px",
    oninput: (e) => { q = e.target.value; apply(); } });
  const catSel = el("select", { style: "max-width:190px",
    onchange: (e) => { cat = e.target.value; apply(); } },
    el("option", { value: "" }, "All categories"),
    ...categories.map((c) => el("option", { value: c }, c)));
  const count = el("span", { class: "mono-micro" });
  const toolbar = el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap" },
    search, categories.length ? catSel : null, el("span", { class: "spacer", style: "flex:1" }), count);
  wrap.append(toolbar, grid);
  apply();
  return wrap;
});

function scenarioCard(p) {
  // Only badge the user's own scenarios — bundled ones are the default, so labelling every card
  // "example" is just noise. A "custom" chip marks the exceptions worth spotting.
  const badge = p.editable
    ? el("span", { class: "chip mini", style: "color:var(--teal)", text: "custom" })
    : null;
  // a button inside a clickable card must not also trigger the card's navigation
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  const go = (verb) => stop(() => { location.hash = `#/scenarios/${verb}/${encodeURIComponent(p.name)}`; });
  const actions = el("div", { class: "card-foot" },
    el("button", { class: "btn sm teal", onclick: stop(() => launchRun(p.name)) }, "▶ Run"),
    el("button", { class: "btn sm ghost", onclick: go("edit") }, "Edit"),
    el("button", { class: "btn sm ghost", onclick: go("clone") }, "Clone"),
    el("span", { class: "spacer" }),
    el("button", { class: "btn sm ghost", title: "Export .zip",
      onclick: stop(() => { window.location = `/api/packages/${encodeURIComponent(p.name)}/export`; }) }, "↧"),
    p.editable && el("button", { class: "btn sm danger", title: "Delete",
      onclick: stop(() => confirmDelete(p)) }, "✕"));
  return el("div", { class: "card hover", style: "cursor:pointer",
    onclick: () => { location.hash = `#/scenarios/view/${encodeURIComponent(p.name)}`; } },
    el("div", { class: "card-top" },
      el("div", null, el("h3", { text: p.title || p.name }),
        el("div", { class: "mono-micro", style: "margin-top:2px", text: p.name })),
      badge),
    el("p", { class: "desc", text: paramPreview(p.description) || "No description." }),
    el("div", { class: "card-foot", style: "margin-top:10px" },
      el("span", { class: "mono-micro", text: `${p.agents} agents` }),
      p.category && el("span", { class: "chip mini", style: "color:var(--iris)", text: p.category }),
      ...(p.tags || []).slice(0, 3).map((t) => el("span", { class: "tag", text: t }))),
    actions);
}

/* ---- read-only scenario detail: full config + roster, with run/clone actions ---- */
route(/^\/scenarios\/view\/(.+)$/, async (name) => {
  name = decodeURIComponent(name);
  const [{ packages }, d] = await Promise.all([api("/api/packages"),
    api(`/api/packages/${encodeURIComponent(name)}`)]);
  const entry = packages.find((p) => p.name === name) || { name, editable: false };
  const editable = !!entry.editable;

  const goto = (verb) => { location.hash = `#/scenarios/${verb}/${encodeURIComponent(name)}`; };
  const actions = [
    el("button", { class: "btn teal", onclick: () => launchRun(name) },
      el("span", { class: "ic" }, "▶"), "Run"),
    el("button", { class: "btn", onclick: () => goto("edit") }, "Edit"),
    el("button", { class: "btn ghost", onclick: () => goto("assist-edit") },
      el("span", { class: "ic" }, "✎"), "Edit with assistant"),
    el("button", { class: "btn ghost", onclick: () => goto("clone") }, "Clone"),
    el("button", { class: "btn ghost", title: "Export .zip",
      onclick: () => { window.location = `/api/packages/${encodeURIComponent(name)}/export`; } }, "Export ↧"),
    editable && el("button", { class: "btn danger",
      onclick: () => confirmDelete({ name, editable }) }, "Delete"),
  ].filter(Boolean);

  const wrap = el("div", null,
    el("div", { class: "crumb" }, el("a", { href: "#/scenarios" }, "Scenarios"), "›",
      el("span", { class: "mono", text: name })),
    pageHead(editable ? "custom scenario" : "bundled example", d.title || name,
      d.description || "", actions));

  // overview: goals + tags
  const over = el("div", { class: "panel" }, el("h2", null, "Overview"));
  if (d.category) over.append(statRow("Category", el("span", { class: "v", text: d.category })));
  if (d.author) over.append(statRow("Author", el("span", { class: "v", text: d.author })));
  if ((d.tags || []).length) over.append(el("div", { class: "pill-list", style: "margin-top:10px" },
    ...d.tags.map((t) => el("span", { class: "tag", text: t }))));
  if ((d.goals || []).length) {
    over.append(el("div", { class: "mono-micro", style: "margin:14px 0 8px", text: "Goals" }));
    over.append(el("ul", { style: "margin:0;padding-left:18px;color:var(--dim)" },
      ...d.goals.map((g) => el("li", { text: g }))));
  }
  if (d.guidelines) over.append(labelBlock("Guidelines", d.guidelines));
  if (d.restrictions) over.append(labelBlock("Restrictions", d.restrictions));
  wrap.append(over);

  // setup: environment / turns / termination
  wrap.append(setupPanel(d));

  // roster: full agent config (referee already first from the API)
  const roster = el("div", { class: "panel" },
    el("div", { class: "panel-head" }, el("h2", null, "Roster"),
      el("span", { class: "mono-micro", text: `${(d.agents || []).length} agents` })));
  (d.agents || []).forEach((a) => roster.append(agentDetailCard(a)));
  wrap.append(roster);
  return wrap;
});

function labelBlock(label, text) {
  return el("div", { style: "margin-top:14px" },
    el("div", { class: "mono-micro", style: "margin-bottom:6px", text: label }),
    el("div", { style: "color:var(--dim);white-space:pre-wrap", text: text }));
}

function setupPanel(d) {
  const e = d.environment || {};
  const chips = el("div", { class: "pill-list", style: "margin-bottom:14px" },
    el("span", { class: "chip" }, "egress: " + (e.network_egress || "?")),
    e.shared_folder && el("span", { class: "chip" }, "shared folder"),
    e.require_mention && el("span", { class: "chip" }, "@mention required"),
    e.allow_side_channels && el("span", { class: "chip" }, "side-channels"),
    d.referee_opens && el("span", { class: "chip role-referee" }, "referee-driven"),
    d.provide_tools === false && el("span", { class: "chip" }, "no realm tools"));
  const p = el("div", { class: "panel" }, el("h2", null, "Setup"), chips);
  // turns
  if (d.turns) {
    p.append(statRow("Turns", el("span", { class: "v", text: "one-at-a-time (physics)" })));
    p.append(statRow("Silence timeout", el("span", { class: "v", text: `${d.turns.silence_timeout_s}s` })));
    p.append(statRow("Referee cue", el("span", { class: "v", text: d.turns.referee_cue })));
    if (d.turns.retire_after_misses)
      p.append(statRow("Retire after misses", el("span", { class: "v", text: d.turns.retire_after_misses })));
  } else {
    p.append(statRow("Turns", el("span", { class: "v", text: "free-for-all (parallel)" })));
  }
  // termination
  const terms = (d.termination || []).map((t) =>
    t.type + (t.content_match ? `: "${t.content_match}"` : t.limit ? `: ${t.limit}` : "")).join(", ");
  p.append(statRow("Ends when", el("span", { class: "v", text: terms || "manual only" })));
  return p;
}

function agentDetailCard(a) {
  const isRef = a.role === "referee";
  const br = a.budget_ref || {};
  const tier = a.model_category || a.model || "medium";
  const body = el("div", { class: "body" },
    el("div", { class: "pill-list", style: "margin-bottom:12px" },
      el("span", { class: "chip" }, `${tier} model`),
      (br.max_usd != null) && el("span", { class: "chip" }, `budget $${br.max_usd}`),
      br.on_exhausted && el("span", { class: "chip" }, br.on_exhausted)));
  if ((a.skills || []).length) {
    body.append(el("div", { class: "mono-micro", style: "margin-bottom:6px", text: "Skills" }));
    body.append(el("div", { class: "pill-list", style: "margin-bottom:12px" },
      ...a.skills.map((s) => skillPill(s, () => showSkill(...s.split(":"))))));
  }
  if (a.persona) body.append(labelBlock("Persona", a.persona));
  if (isRef && a.rubric) body.append(labelBlock("Rubric", a.rubric));
  if ((a.goals || []).length) {
    body.append(el("div", { class: "mono-micro", style: "margin:14px 0 6px", text: "Goals" }));
    body.append(el("ul", { style: "margin:0;padding-left:18px;color:var(--dim)" },
      ...a.goals.map((g) => el("li", { text: g }))));
  }
  return el("details", { class: "agent-block", open: true },
    el("summary", null,
      el("span", { class: `agent-avatar ${isRef ? "ref" : ""}` }, (a.name || a.id).slice(0, 2)),
      el("div", null, el("b", { text: a.name || a.id }),
        el("span", { class: `chip mini ${isRef ? "role-referee" : ""}`, style: "margin-left:8px" }, a.role)),
      el("span", { class: "mono-micro", style: "margin-left:auto", text: `${tier} model` }),
      el("span", { class: "disclose", style: "margin-left:12px" }, "›")),
    body);
}

async function launchRun(name) {
  try { const { packages } = await api("/api/packages"); launchModal(packages, name); }
  catch (e) { fail("Couldn't load scenarios", e.message); }
}
function confirmDelete(p) {
  modal({
    title: "Delete scenario?",
    body: el("p", null, "Delete ", el("b", { text: p.name }), "? This removes your saved copy. ",
      "If it shadowed a bundled example, the example reappears."),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", {
        class: "btn danger", onclick: guard("Deleting…", async () => {
          try { await api(`/api/packages/${encodeURIComponent(p.name)}`, { method: "DELETE" });
            ok("Scenario deleted", p.name); close(); render(); } catch (e) { fail("Delete failed", e.message); }
        })
      }, "Delete")],
  });
}

function importModal() {
  const folder = el("input", { type: "file", multiple: true });
  folder.setAttribute("webkitdirectory", "");
  const zip = el("input", { type: "file", accept: ".zip" });
  const doImport = async (fd, url, close) => {
    try { const r = await api(url, { method: "POST", body: fd });
      ok("Imported", r.name); close(); location.hash = "#/scenarios"; render(); }
    catch (e) { fail("Import failed", e.message); }
  };
  modal({
    title: "Import scenario",
    body: el("div", null,
      el("div", { class: "field" }, el("label", null, "From a .zip file"),
        zip, el("div", { class: "hint", style: "margin:6px 0 0", text: "A scenario folder zipped at its root." })),
      el("hr", { class: "divide" }),
      el("div", { class: "field" }, el("label", null, "From a folder"),
        folder, el("div", { class: "hint", style: "margin:6px 0 0", text: "Pick the scenario's project folder." }))),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", {
        class: "btn primary", onclick: guard("Importing…", async () => {
          if (zip.files.length) { const fd = new FormData(); fd.append("file", zip.files[0]);
            return doImport(fd, "/api/packages/import-zip", close); }
          if (folder.files.length) { const fd = new FormData();
            for (const f of folder.files) fd.append("files", f, f.webkitRelativePath || f.name);
            return doImport(fd, "/api/packages/import", close); }
          fail("Pick a file", "Choose a .zip or a folder to import.");
        })
      }, "Import")],
  });
}

/* ================= GUIDED AUTHORING (Scribe, #73) ================= */
// Two entry points, no standalone chat page: the wizard (#/scenarios/assist-new) runs a guided
// Q&A -> draft -> approve flow, and assist-edit (#/scenarios/assist-edit/<name>) pairs a chat
// column with a live draft preview saved through the normal editor PUT. The model never writes:
// approve/save are deterministic platform calls.

function scribeToolPill(name) { return el("span", { class: "scribe-tool" }, "⚙ " + name); }

function safeParse(text) { try { return JSON.parse(text); } catch { return null; } }

// Stream one Scribe turn (NDJSON) into per-kind handlers; resolves when the stream closes.
async function scribeStream(sessionId, text, on) {
  const res = await fetch("/api/scribe/message", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, text }),
  });
  if (!res.ok || !res.body) throw new Error("stream failed (" + res.status + ")");
  const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      const ev = safeParse(line);
      if (ev && on[ev.kind]) on[ev.kind](ev);
    }
  }
}

// Convert a Scribe draft (a full Project manifest) into the exact body the scenario editor PUTs:
// {metadata, spec, agents} — the only per-field difference is agent skills, which the editor
// sends as "source:ref" strings rather than {source, ref} objects.
function draftToEditorBody(spec) {
  return {
    metadata: spec.metadata || {},
    spec: spec.spec || {},
    agents: (spec.agents || []).map((a) => ({ ...a,
      skills: (a.skills || []).map((s) =>
        typeof s === "string" ? s : `${s.source || "builtin"}:${s.ref}`) })),
  };
}

/* ----- draft card: a friendly rendering of a manifest (wizard draft / edit preview) ----- */
function scribeAgentCard(a) {
  const isRef = (a.role || "participant") === "referee";
  const folds = [];
  if (a.persona) folds.push(["Persona", a.persona]);
  if (a.rubric) folds.push(["Rubric", a.rubric]);
  return el("div", { class: "scribe-agent" },
    el("div", { class: "scribe-agent-head" },
      el("span", { class: `agent-avatar ${isRef ? "ref" : ""}` }, (a.name || a.id || "?").slice(0, 2)),
      el("b", { text: a.name || a.id }),
      el("span", { class: `chip mini ${isRef ? "role-referee" : ""}` }, a.role || "participant"),
      el("span", { class: "mono-micro", style: "margin-left:auto",
        text: (a.model_category || "medium") + " model" })),
    (a.goals || []).length ? el("ul", { class: "scribe-agent-goals" },
      ...a.goals.map((g) => el("li", { text: g }))) : null,
    ...folds.map(([label, text]) => el("details", { class: "scribe-agent-more" },
      el("summary", null, label),
      el("div", { class: "scribe-agent-text", text }))));
}

function scribeDraftCard(spec, opts = {}) {
  const md = spec.metadata || {}, sp = spec.spec || {}, env = sp.environment || {};
  const sf = env.shared_folder;
  const flags = [
    env.require_mention === false ? "free-response" : "mention-gated",
    (sf === true || (sf && sf.enabled)) && "shared folder",
    env.allow_side_channels && "side-channels",
    env.network_egress && env.network_egress !== "model_only" && ("egress: " + env.network_egress),
  ].filter(Boolean).join(" · ");
  const terms = (sp.termination || []).map((t) => {
    const needle = t.pattern || t.content_match;
    return t.type + (needle ? `: "${needle}"` : t.limit ? `: ${t.limit}` : "");
  }).join(", ") || "manual only";
  const mech = (sp.mechanics || []).map((m) => m.kind).join(", ") || "—";
  const kv = (k, v) => el("div", { class: "scribe-draft-kv" },
    el("div", { class: "mono-micro", text: k }), el("div", { class: "v", text: v }));
  return el("div", { class: "scribe-draft" },
    el("div", { class: "scribe-draft-head" },
      el("h3", { text: md.name || "(unnamed)" }),
      el("span", { class: "chip mini", style: "color:var(--teal)" }, "✓ validates"),
      opts.modified && el("span", { class: "chip mini", style: "color:var(--amber)" }, "modified")),
    md.description && el("div", { class: "scribe-draft-desc", text: md.description }),
    el("div", { class: "scribe-draft-grid" },
      kv("Turns", sp.turns ? (sp.turns.policy || "one-at-a-time") : "free-for-all (parallel)"),
      kv("Ends when", terms),
      kv("Mechanics", mech),
      kv("Environment", flags)),
    el("div", { class: "scribe-agents" }, ...(spec.agents || []).map(scribeAgentCard)));
}

/* ----- shared chat column for both guided flows ----- */
// Returns {msgs, inputRow, question, focus, scrollDown}. `opts.onDraft(spec)` decides what a
// draft event does (wizard: inline card + approve; edit: refresh the side preview); with
// `opts.skipChip` every model question carries the persistent "Skip — build it all" chip.
function scribeChat(sessionId, opts) {
  const msgs = el("div", { class: "scribe-msgs" });
  const input = el("textarea", { class: "scribe-input", rows: 2,
    placeholder: opts.placeholder || "Reply…" });
  const send = el("button", { class: "btn primary" }, "Send");
  const scrollDown = () => { msgs.scrollTop = msgs.scrollHeight; };
  const bubble = (cls, ...kids) => {
    const b = el("div", { class: "scribe-msg " + cls }, ...kids);
    msgs.append(b); scrollDown(); return b;
  };

  function chipsRow(options, withSkip) {
    if (!(options || []).length && !withSkip) return null;
    const row = el("div", { class: "scribe-chips" });
    // the model sometimes offers its own skip option — drop it so the persistent chip isn't doubled
    (options || []).filter((o) => !/skip\s*[—-]|build it all/i.test(o)).forEach((o) => row.append(
      el("button", { class: "scribe-chip", onclick: () => submit(o) }, o)));
    if (withSkip) row.append(el("button", { class: "scribe-chip skip",
      onclick: () => submit("Skip — build it all") }, "Skip — build it all"));
    return row;
  }

  function question(text, options, withSkip) {
    bubble("assistant", el("div", { class: "scribe-body" }, text), chipsRow(options, withSkip));
  }

  // Re-render a resumed thread (#75): the server's visible history as plain chat bubbles.
  function replay(history) {
    (history || []).forEach((m) => bubble(m.role === "user" ? "user" : "assistant",
      el("div", { class: "scribe-body" }, m.text)));
  }

  async function submit(text) {
    text = (text || "").trim();
    if (!text || send.disabled) return;
    input.value = "";
    bubble("user", el("div", { class: "scribe-body" }, text));
    send.disabled = true; input.disabled = true;
    const toolsRow = el("div", { class: "scribe-tools" });
    const body = el("div", { class: "scribe-body" }, el("span", { class: "scribe-typing" }, "…"));
    const pending = bubble("assistant", toolsRow, body);
    let prose = "", done = false;
    const clearPending = () => {  // turn ended on a question/draft — drop the empty typing bubble
      if (prose) return;                              // streamed prose stays as its own bubble
      if (toolsRow.childElementCount) body.remove();  // keep the tool pills
      else pending.remove();
    };
    try {
      await scribeStream(sessionId, text, {
        tool_call: (ev) => { toolsRow.append(scribeToolPill(ev.name)); scrollDown(); },
        notice: (ev) => { msgs.insertBefore(
          el("div", { class: "scribe-notice", text: ev.text }), pending); scrollDown(); },
        text: (ev) => { prose += ev.text || ""; body.textContent = prose; scrollDown(); },
        final: (ev) => { done = true; prose = ev.text || prose;
          body.textContent = prose || "(no reply)"; scrollDown(); },
        question: (ev) => { done = true; clearPending();
          question(ev.text, safeParse(ev.name) || [], opts.skipChip); },
        draft: (ev) => { done = true; clearPending();
          const spec = safeParse(ev.text); if (spec) opts.onDraft(spec); },
        error: (ev) => { done = true; body.textContent = "";
          pending.append(el("div", { class: "scribe-err" }, "⚠ " + ev.text)); scrollDown(); },
      });
      if (!done && !prose) body.textContent = "(no reply)";
    } catch (e) {
      body.textContent = "";
      pending.append(el("div", { class: "scribe-err" }, "⚠ " + e.message));
    } finally {
      send.disabled = false; input.disabled = false; input.focus(); scrollDown();
    }
  }

  send.addEventListener("click", () => submit(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(input.value); }
  });
  return {
    msgs,
    inputRow: el("div", { class: "scribe-inputrow" }, input, send),
    question,
    replay,
    focus: () => input.focus(),
    scrollDown,
  };
}

/* ----- wizard: guided create ----- */
route(/^\/scenarios\/assist-new$/, async () => {
  const sess = await api("/api/scribe/session", { method: "POST", body: { mode: "create" } });
  let draftWrap = null;  // the current draft card — a re-proposal replaces it

  const chat = scribeChat(sess.session_id, {
    placeholder: "Describe your scenario, or answer the question…",
    skipChip: true,
    onDraft: (spec) => {
      if (draftWrap) draftWrap.remove();
      draftWrap = el("div", { class: "scribe-msg assistant scribe-draft-wrap" },
        scribeDraftCard(spec),
        el("div", { class: "scribe-actions" },
          el("button", { class: "btn primary", onclick: guard("Creating…", approve) },
            "✓ Approve & create"),
          el("button", { class: "btn ghost", onclick: () => chat.focus() }, "Request changes")));
      chat.msgs.append(draftWrap); chat.scrollDown();
    },
  });

  async function approve() {
    try {
      const r = await api("/api/scribe/approve", {
        method: "POST", body: { session_id: sess.session_id } });
      ok("Scenario created", r.name);
      location.hash = "#/scenarios/edit/" + encodeURIComponent(r.name);  // the existing editor
    } catch (e) { fail("Approve failed", e.message); }
  }

  chat.question(sess.opening.text, sess.opening.options, false);  // canned opening — no model call
  return el("div", { class: "scribe-page" },
    el("div", { class: "crumb" }, el("a", { href: "#/scenarios" }, "Scenarios"), "›",
      el("span", { text: "Create with assistant" })),
    pageHead("Authoring", "Create with assistant",
      "Describe it — Scribe asks a few questions, drafts the scenario, and you approve."),
    chat.msgs,
    chat.inputRow);
});

/* ----- assist-edit: chat + live preview, saved through the normal editor PUT ----- */
route(/^\/scenarios\/assist-edit\/(.+)$/, async (name) => {
  name = decodeURIComponent(name);
  const sess = await api("/api/scribe/session", {
    method: "POST", body: { mode: "edit", scenario: name } });
  const preview = el("div", { class: "scribe-preview" }, scribeDraftCard(sess.package));
  let draft = null;  // the latest proposed spec; Save is disabled until one arrives
  const saveBtn = el("button", { class: "btn primary", disabled: true,
    onclick: guard("Saving…", save) }, "Save changes");

  async function save() {
    if (!draft) return;
    try {
      await api(`/api/packages/${encodeURIComponent(name)}`,
        { method: "PUT", body: draftToEditorBody(draft) });
      ok("Scenario saved", name);
      location.hash = "#/scenarios/view/" + encodeURIComponent(name);
    } catch (e) { fail("Save failed", e.message); }
  }

  const chat = scribeChat(sess.session_id, {
    placeholder: "Describe a change…",
    skipChip: false,
    onDraft: (spec) => {
      draft = spec;
      clear(preview);
      preview.append(scribeDraftCard(spec, { modified: true }));
      saveBtn.disabled = false;
    },
  });

  // Per-scenario history controls (#75): start fresh, or collapse the thread to a summary now.
  const clearBtn = el("button", { class: "btn ghost", onclick: guard("Clearing…", async () => {
    if (!confirm(`Delete the saved assistant conversation for '${name}'? Scribe forgets ` +
        "everything discussed about this scenario.")) return;
    try {
      await api(`/api/scribe/history/${encodeURIComponent(name)}`, { method: "DELETE" });
      ok("History cleared", name);
      render();  // re-mount: the old session is gone server-side, so start a fresh one
    } catch (e) { fail("Clear failed", e.message); }
  }) }, "Clear history");
  const sumBtn = el("button", { class: "btn ghost", onclick: guard("Summarizing…", async () => {
    try {
      const r = await api(`/api/scribe/history/${encodeURIComponent(name)}/summarize`,
        { method: "POST" });
      clear(chat.msgs);
      chat.replay(r.history);
      chat.question(sess.opening.text, [], false);
      ok("History summarized", name);
    } catch (e) { fail("Summarize failed", e.message); }
  }) }, "Summarize");

  chat.replay(sess.history);  // the resumed thread (persists across visits + restarts)
  chat.question(sess.opening.text, sess.opening.options, false);  // canned opening
  return el("div", { class: "scribe-page wide" },
    el("div", { class: "crumb" }, el("a", { href: "#/scenarios" }, "Scenarios"), "›",
      el("a", { class: "mono", href: "#/scenarios/view/" + encodeURIComponent(name) }, name), "›",
      el("span", { text: "Assistant" })),
    pageHead("Authoring", `Edit · ${name}`,
      "Ask for changes — the preview updates as Scribe re-proposes; nothing is written until you save.",
      [clearBtn, sumBtn,
       el("button", { class: "btn ghost", onclick: () => {
          location.hash = "#/scenarios/view/" + encodeURIComponent(name); } }, "Cancel"),
       saveBtn]),
    el("div", { class: "scribe-split" },
      el("div", { class: "scribe-chatcol" }, chat.msgs, chat.inputRow),
      preview));
});

/* ================= SKILLS ================= */
route(/^\/skills$/, async () => {
  const { skills } = await api("/api/skills");
  const wrap = el("div", null,
    pageHead("Capabilities", "Skills",
      "SKILL.md briefs that give an agent a role or capability. Edit any of them, or add your own.",
      [el("button", { class: "btn ghost", onclick: () => importSkillModal() },
        el("span", { class: "ic" }, "↥"), "Import"),
       el("button", { class: "btn primary", onclick: () => editSkillModal() },
        el("span", { class: "ic" }, "＋"), "New skill")]));
  if (!skills.length) {
    wrap.append(emptyState("✦", "No skills yet", "Add one or import from GitHub.",
      el("button", { class: "btn primary", onclick: () => editSkillModal() }, "New skill")));
    return wrap;
  }
  const categories = [...new Set(skills.map((s) => s.category).filter(Boolean))].sort();
  const grid = el("div", { class: "grid cols" });
  let q = "", cat = "";
  const apply = () => {
    const ql = q.trim().toLowerCase();
    const shown = skills.filter((s) =>
      (!cat || s.category === cat) &&
      (!ql || s.ref.toLowerCase().includes(ql) || (s.description || "").toLowerCase().includes(ql)));
    clear(grid);
    if (shown.length) shown.forEach((s) => grid.append(skillCard(s)));
    else grid.append(el("div", { class: "inline-note", style: "padding:34px 4px",
      text: "No skills match your filters." }));
    count.textContent = `${shown.length} of ${skills.length}`;
  };
  const search = el("input", { type: "text", placeholder: "Search by name…", style: "max-width:260px",
    oninput: (e) => { q = e.target.value; apply(); } });
  const catSel = el("select", { style: "max-width:190px",
    onchange: (e) => { cat = e.target.value; apply(); } },
    el("option", { value: "" }, "All categories"),
    ...categories.map((c) => el("option", { value: c }, c)));
  const count = el("span", { class: "mono-micro" });
  const toolbar = el("div", { style: "display:flex;gap:10px;align-items:center;margin-bottom:18px;flex-wrap:wrap" },
    search, categories.length ? catSel : null, el("span", { style: "flex:1" }), count);
  wrap.append(toolbar, grid);
  apply();
  return wrap;
});

function skillCard(s) {
  const stop = (fn) => (e) => { e.stopPropagation(); fn(); };
  const foot = el("div", { class: "card-foot" },
    el("button", { class: "btn sm ghost", onclick: stop(() => editSkillModal(s)) }, "Edit"),
    (s.files || 1) > 1 && el("span", { class: "mono-micro", text: `${s.files} files` }),
    el("span", { class: "spacer" }),
    el("button", { class: "btn sm ghost", title: "Export .zip",
      onclick: stop(() => { window.location =
        `/api/skills/${s.source}/${encodeURIComponent(s.ref)}/export`; }) }, "↧"),
    s.deletable && el("button", { class: "btn sm danger", title: "Delete",
      onclick: stop(() => confirmDeleteSkill(s)) }, "✕"));
  return el("div", { class: "card hover", style: "cursor:pointer",
    onclick: () => showSkill(s.source, s.ref) },
    el("div", { class: "card-top" },
      el("h3", { text: s.ref }),
      s.category && el("span", { class: "chip mini", style: "color:var(--iris)", text: s.category })),
    el("p", { class: "desc", text: paramPreview(s.description) || "No description." }), foot);
}

function confirmDeleteSkill(s) {
  modal({
    title: "Delete skill?",
    body: el("p", null, "Delete ", el("b", { text: s.ref }),
      "? Your copy is removed; if it overrides a seed skill, the seed reappears."),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", { class: "btn danger", onclick: guard("Deleting…", async () => {
        try { await api(`/api/skills/${encodeURIComponent(s.ref)}`, { method: "DELETE" });
          ok("Skill deleted", s.ref); close(); render(); } catch (e) { fail("Delete failed", e.message); }
      }) }, "Delete")],
  });
}

function editSkillModal(existing) {
  const nameI = el("input", { type: "text", value: existing ? existing.ref : "", class: "mono",
    maxlength: 64, placeholder: "my-skill", disabled: !!existing });
  const catI = el("input", { type: "text", value: existing ? (existing.category || "") : "",
    maxlength: 60, placeholder: "e.g. Referee" });
  const contentI = el("textarea", { class: "mono", rows: 14, maxlength: 50000,
    placeholder: "---\nname: my-skill\ndescription: what it does\n---\n\nGuidance for the agent…" });
  if (existing) api(`/api/skills/${existing.source}/${encodeURIComponent(existing.ref)}`)
    .then((s) => { contentI.value = s.content; }).catch(() => {});
  const isSeed = existing && existing.source === "builtin";
  modal({
    title: existing ? `Edit · ${existing.ref}` : "New skill", wide: true,
    body: el("div", null,
      isSeed && el("p", { class: "inline-note", style: "margin-top:0", text:
        "Editing a seed skill saves your own copy that overrides it — the original is kept, and "
        + "deleting your copy restores it." }),
      el("div", { class: "row" },
        el("div", { class: "field" }, el("label", null, "Name ",
          el("span", { class: "hint" }, "lowercase-with-dashes")), nameI),
        el("div", { class: "field" }, el("label", null, "Category ",
          el("span", { class: "hint" }, "for grouping & filtering")), catI)),
      el("div", { class: "field" }, el("label", null, "SKILL.md content ",
        el("span", { class: "hint" }, "frontmatter added if you omit it")), contentI)),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", { class: "btn primary", onclick: guard("Saving…", async () => {
        if (!nameI.value.trim() || !contentI.value.trim()) return fail("Name + content required");
        try { await api("/api/skills", { method: "POST",
          body: { name: nameI.value, content: contentI.value, category: catI.value } });
          ok("Skill saved", nameI.value); close(); render(); } catch (e) { fail("Save failed", e.message); }
      }) }, "Save skill")],
  });
}

function importSkillModal() {
  const fileI = el("input", { type: "file", accept: ".md,.zip,text/markdown" });
  const folderI = el("input", { type: "file", multiple: true });
  folderI.setAttribute("webkitdirectory", "");
  const url = el("input", { type: "url", class: "mono",
    placeholder: "https://github.com/org/repo/blob/main/skills/x/SKILL.md" });
  const run = async (fn, close) => {
    try { const r = await fn(); ok("Skill imported", r.ref); close(); render(); }
    catch (e) { fail("Import failed", e.message); }
  };
  modal({
    title: "Import skill", wide: true,
    body: el("div", null,
      el("p", { class: "inline-note", style: "margin-top:0", text:
        "An Agent Skill is a folder with a SKILL.md plus optional scripts / references / assets. "
        + "Import a single SKILL.md, a .zip, a whole folder, or from GitHub." }),
      el("div", { class: "field" },
        el("label", null, "File ", el("span", { class: "hint" }, "SKILL.md or a .zip")), fileI),
      el("hr", { class: "divide" }),
      el("div", { class: "field" }, el("label", null, "Folder ",
        el("span", { class: "hint" }, "the skill directory")), folderI),
      el("hr", { class: "divide" }),
      el("div", { class: "field" }, el("label", null, "GitHub URL ",
        el("span", { class: "hint" }, "single SKILL.md")), url)),
    actions: (close) => [
      el("button", { class: "btn ghost", onclick: close }, "Cancel"),
      el("button", { class: "btn primary", onclick: guard("Importing…", async () => {
        if (fileI.files.length) {
          const f = fileI.files[0];
          const zip = f.name.toLowerCase().endsWith(".zip");
          const fd = new FormData(); fd.append("file", f);
          return run(() => api(zip ? "/api/skills/import-zip" : "/api/skills/import-file",
            { method: "POST", body: fd }), close);
        }
        if (folderI.files.length) {
          const fd = new FormData();
          for (const f of folderI.files) fd.append("files", f, f.webkitRelativePath || f.name);
          return run(() => api("/api/skills/import-folder", { method: "POST", body: fd }), close);
        }
        if (url.value.trim()) {
          return run(() => api("/api/skills/import-gh", { method: "POST", body: { url: url.value } }), close);
        }
        fail("Pick a source", "Choose a file, a folder, or a GitHub URL.");
      }) }, "Import")],
  });
}

/* ================= HISTORY ================= */
route(/^\/history$/, async () => {
  const { runs } = await api("/api/runs");
  const wrap = el("div", null,
    pageHead("Records", "History", "Every realm that has run, with its outcome and spend."));
  if (!runs.length) { wrap.append(emptyState("≡", "No runs yet", "Launched realms will appear here.")); return wrap; }
  const scenarios = [...new Set(runs.map((r) => r.scenario))].sort();
  const filter = el("select", { style: "max-width:220px",
    onchange: (e) => draw(e.target.value) },
    el("option", { value: "" }, `All scenarios (${runs.length})`),
    ...scenarios.map((s) => el("option", { value: s }, s)));
  wrap.append(el("div", { class: "head-actions", style: "margin-bottom:16px" }, filter));
  const tblWrap = el("div", { class: "tbl-wrap" });
  wrap.append(tblWrap);
  const draw = (scen) => {
    const rows = scen ? runs.filter((r) => r.scenario === scen) : runs;
    clear(tblWrap);
    const tb = el("tbody");
    rows.forEach((r) => tb.append(el("tr", { onclick: () => { location.hash = `#/realm/${encodeURIComponent(r.realm_id)}`; } },
      el("td", null, el("span", { class: "mono", text: r.realm_id })),
      el("td", null, el("span", { class: "mono-micro", text: r.scenario })),
      el("td", null, stateChip(r.state)),
      el("td", null, el("span", { text: r.outcome ? trunc(r.outcome, 40) : "—", style: r.outcome ? "" : "color:var(--faint)" })),
      el("td", { class: "mono" }, money(r.spend)),
      el("td", null, el("span", { class: "inline-note", text: ago(r.updated) })))));
    tblWrap.append(el("table", { class: "tbl" },
      el("thead", null, el("tr", null,
        ...["Realm", "Scenario", "State", "Outcome", "Spend", "When"].map((h) => el("th", { text: h })))),
      tb));
  };
  draw("");
  return wrap;
});

/* ================= SETTINGS ================= */
route(/^\/settings$/, async () => {
  const s = await api("/api/settings");
  const wrap = el("div", null,
    pageHead("Configuration", "Settings", "Platform-level configuration and library status."));
  wrap.append(el("div", { class: "grid", style: "grid-template-columns:repeat(auto-fit,minmax(230px,1fr));margin-bottom:20px" },
    statCard("Version", s.version, "Bearpit build"),
    statCard("Realm capacity", `${s.active} / ${s.capacity}`, "running / max concurrent"),
    statCard("Scenarios", s.scenarios, "available to run"),
    statCard("Skills", `${s.skills_builtin + s.skills_custom}`, `${s.skills_builtin} built-in · ${s.skills_custom} custom`)));

  wrap.append(modelPipelinePanel(s));

  const keys = el("div", { class: "panel" }, el("h2", null, "API key references"),
    el("p", { class: "panel-sub" },
      "BYOK handles agents reference by name. Keys live encrypted in the keystore — never shown here, never in a container."));
  if ((s.api_key_refs || []).length) {
    keys.append(el("div", { class: "pill-list" },
      ...s.api_key_refs.map((k) => el("span", { class: "skill-pill" }, el("span", { class: "src" }, "key"), k))));
  } else {
    keys.append(el("p", { class: "inline-note", text: "No keys stored." }));
  }
  keys.append(el("p", { class: "inline-note", style: "margin-top:14px" },
    "Add a key from the CLI: "), el("span", { class: "kbd", text: "pit keys add <ref>" }));
  wrap.append(keys);

  wrap.append(el("div", { class: "panel" }, el("h2", null, "Storage"),
    el("div", { class: "stat-row" }, el("span", { class: "k" }, "Scenarios dir"),
      el("span", { class: "v", text: s.scenarios_dir })),
    el("div", { class: "stat-row" }, el("span", { class: "k" }, "Examples dir"),
      el("span", { class: "v", text: s.examples_dir }))));
  return wrap;
});
function statCard(label, value, sub) {
  return el("div", { class: "card" },
    el("div", { class: "mono-micro", text: label }),
    el("div", { style: "font-size:28px;font-weight:640;margin:8px 0 2px;letter-spacing:-.02em", text: value }),
    el("div", { class: "inline-note", text: sub }));
}
function modelPipelinePanel(s) {
  const providers = s.model_providers || [];
  const panel = el("div", { class: "panel" }, el("h2", null, "Model pipeline"),
    el("p", { class: "panel-sub" },
      "Which AI backend ALL scenarios run on. Switching applies at launch time — no scenario is edited, so it's an instant, reversible toggle."));
  const status = el("p", { class: "inline-note", style: "margin:10px 0" });
  const renderStatus = (cur) => {
    const p = providers.find((x) => x.name === cur);
    status.textContent = p ? `Active: ${p.label} — ${p.description}` : `Active: ${cur}`;
  };
  const sel = el("select");
  for (const p of providers) {
    const label = p.label + (p.ready ? "" : ` (needs key '${p.needs_key_ref}')`);
    sel.append(el("option", { value: p.name }, label));
  }
  sel.value = s.model_provider;
  renderStatus(s.model_provider);
  const save = el("button", { class: "btn" }, "Apply pipeline");
  save.onclick = guard("Applying…", async () => {
    const name = sel.value;
    const chosen = providers.find((x) => x.name === name);
    if (chosen && !chosen.ready &&
        !confirm(`${chosen.label} isn't configured yet — its keystore handle '${chosen.needs_key_ref}' is missing, so runs will fail until you add it. Switch anyway?`)) {
      return;
    }
    try {
      const r = await api("/api/settings/provider", { method: "PUT", body: { model_provider: name } });
      renderStatus(r.model_provider);
      toast("Pipeline switched", `All scenarios will now run on ${chosen ? chosen.label : r.model_provider}.`, "ok");
    } catch (err) { toast("Switch failed", err.message, "err"); }
  });
  panel.append(el("div", { class: "row", style: "gap:10px;align-items:center;flex-wrap:wrap" }, sel, save), status);
  // Every unconfigured provider shows its OWN setup line — the profile carries the text, so a
  // contributed pipeline explains itself without the UI knowing anything about it.
  for (const p of providers.filter((x) => !x.ready && x.setup_hint)) {
    const line = el("p", { class: "inline-note", style: "margin-top:6px" },
      el("strong", { text: `${p.label} setup: ` }));
    // backticked spans render as commands; everything else is prose
    for (const [i, part] of String(p.setup_hint).split("`").entries()) {
      if (!part) continue;
      line.append(i % 2 ? el("span", { class: "kbd", text: part }) : document.createTextNode(part));
    }
    panel.append(line);
  }
  panel.append(categoryTables(providers));
  return panel;
}

// Editable per-provider tables: each maps small/medium/large -> {model, effort, costs, context}.
// Agents reference only the category; this is where a category becomes a concrete model.
function categoryTables(providers) {
  const EFFORTS = ["", "low", "medium", "high", "xhigh", "max"];
  const num = (v) => (v === "" || v == null ? null : Number(v));
  // a local editable deep-copy of the config, sent whole on save
  const cfg = {};
  for (const p of providers) {
    cfg[p.name] = { label: p.label, description: p.description, api_key_ref: p.api_key_ref,
      // policy fields are round-tripped verbatim: the table below only edits categories, and
      // dropping these on save would silently disable a pipeline's budget/turn floors
      setup_hint: p.setup_hint, flat_rate: p.flat_rate,
      min_budget_usd: p.min_budget_usd, min_turn_seconds: p.min_turn_seconds,
      categories: JSON.parse(JSON.stringify(p.categories || {})) };
  }
  const wrap = el("div", { style: "margin-top:22px" },
    el("h3", { style: "margin:0 0 2px;font-size:15px", text: "Category → model" }),
    el("p", { class: "panel-sub",
      text: "For each provider, map small / medium / large to a model, reasoning effort, per-1M-token prices, and context window. Prices meter your budgets." }));
  const grid6 = "display:grid;grid-template-columns:64px 1.5fr 96px 92px 92px 96px;gap:6px 8px;align-items:center;margin-top:8px";
  const inp = (e, key, type, ph) => el("input", { type, value: e[key] ?? "", placeholder: ph || "",
    style: "width:100%;padding:5px 7px;font-size:12.5px",
    oninput: (ev) => { e[key] = type === "number" ? num(ev.target.value) : ev.target.value; } });
  const price = (e, key) => {  // stored per-token; edited per-1M-tokens
    const box = el("input", { type: "number", step: "0.01", style: "width:100%;padding:5px 7px;font-size:12.5px",
      value: e[key] != null ? +(e[key] * 1e6).toFixed(6) : "" });
    box.oninput = () => { const v = num(box.value); e[key] = v == null ? null : v / 1e6; };
    return box;
  };
  const effortSel = (e) => { const s = el("select", { style: "width:100%;padding:5px;font-size:12.5px" },
    ...EFFORTS.map((x) => el("option", { value: x, selected: (e.effort || "") === x }, x || "—")));
    s.onchange = () => { e.effort = s.value || null; }; return s; };
  for (const p of providers) {
    const prov = cfg[p.name];
    const card = el("div", { class: "card", style: "margin-top:10px;padding:12px 14px" },
      el("div", { style: "font-weight:640", text: p.label }),
      el("div", { class: "mono-micro", style: "margin-bottom:4px",
        text: `key: ${p.api_key_ref || "—"}${p.ready ? "" : " · handle missing"}` }));
    const grid = el("div", { style: grid6 });
    ["Tier", "Model", "Effort", "In $/1M", "Out $/1M", "Context"].forEach((h) =>
      grid.append(el("div", { class: "mono-micro", text: h })));
    for (const cat of ["small", "medium", "large"]) {
      const e = prov.categories[cat] = prov.categories[cat] || {};
      grid.append(el("span", { class: "chip mini", text: cat }));
      grid.append(inp(e, "model", "text", "model id"));
      grid.append(effortSel(e));
      grid.append(price(e, "input_cost_per_token"));
      grid.append(price(e, "output_cost_per_token"));
      grid.append(inp(e, "context_length", "number", "128000"));
    }
    card.append(grid);
    wrap.append(card);
  }
  const save = el("button", { class: "btn primary", style: "margin-top:12px" }, "Save model tables");
  save.onclick = guard("Saving…", async () => {
    try {
      await api("/api/settings/model-config", { method: "PUT", body: { providers: cfg } });
      toast("Model tables saved", "Category → model mappings updated for all scenarios.", "ok");
    } catch (err) { toast("Save failed", err.message, "err"); }
  });
  wrap.append(save);
  return wrap;
}
