/* Smoke tests for the Bearpit console's REAL JavaScript.
 *
 * `node --check` proves app.js parses. It does not prove that `tick` can see the variable it
 * reads, or that a click handler asks the right endpoint — three bugs of exactly that shape
 * shipped past a green test suite in one day and were found by a human clicking the page.
 *
 * So: build a DOM small enough to read, evaluate app.js in it with node:vm, and drive the real
 * functions. No npm, no package.json, no jsdom — the console is deliberately dependency-free
 * (the realm CSP blocks external hosts) and its tests should be too.
 *
 * Run:  node tests/ui_smoke.mjs        (exit 0 = all checks pass)
 * Each check prints one line:  CHECK <name> PASS|FAIL <detail>
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP_JS = join(HERE, "..", "src", "bearpit", "gatekeeper", "static", "app.js");

/* ============================ a DOM, in about 150 lines ============================
 * Only what app.js's el() builder and its render functions actually touch. Anything missing
 * throws a TypeError inside the code under test, which is a failure with a stack — the right
 * outcome for "the console started using a DOM API this harness doesn't model". */

const ELEMENT_NODE = 1;
const TEXT_NODE = 3;

const textNode = (data) => ({ nodeType: TEXT_NODE, data: String(data), parentNode: null });

/** Rendered text of a node and everything under it — what a reader would see. */
function textOf(node) {
  if (!node) return "";
  if (node.nodeType === TEXT_NODE) return node.data;
  return (node.childNodes || []).map(textOf).join("");
}

/** Parse one compound selector: "tag", ".cls", "#id", "tag.cls", ".a.b". */
function parseCompound(sel) {
  const m = /^([a-zA-Z][\w-]*)?((?:[.#][\w-]+)*)$/.exec(String(sel).trim());
  if (!m || (!m[1] && !m[2])) throw new Error(`ui_smoke: unsupported selector "${sel}"`);
  const bits = m[2] ? m[2].match(/[.#][\w-]+/g) : [];
  return {
    tag: m[1] ? m[1].toUpperCase() : null,
    id: (bits.find((b) => b[0] === "#") || "").slice(1) || null,
    classes: bits.filter((b) => b[0] === ".").map((b) => b.slice(1)),
  };
}

function matchesCompound(node, c) {
  if (!node || node.nodeType !== ELEMENT_NODE) return false;
  if (c.tag && node.tagName !== c.tag) return false;
  if (c.id && node.id !== c.id) return false;
  return c.classes.every((k) => node.classList.contains(k));
}

function descendants(node, out = []) {
  for (const kid of node.childNodes || []) {
    if (kid.nodeType === ELEMENT_NODE) { out.push(kid); descendants(kid, out); }
  }
  return out;
}

/** querySelectorAll over descendant chains ("#view", ".nav-links a") — every form app.js uses. */
function queryAll(root, selector) {
  let scopes = [root], found = [];
  for (const step of String(selector).trim().split(/\s+/).map(parseCompound)) {
    found = [];
    for (const scope of scopes) {
      for (const d of descendants(scope)) if (matchesCompound(d, step)) found.push(d);
    }
    scopes = found;
  }
  return found;
}

function createElement(tag) {
  const node = {
    nodeType: ELEMENT_NODE,
    tagName: String(tag).toUpperCase(),
    childNodes: [],
    parentNode: null,
    id: "",
    className: "",
    innerHTML: "",
    value: "",
    hidden: false,
    disabled: false,
    checked: false,
    selected: false,
    rows: 0,
    scrollTop: 0, scrollHeight: 0, clientHeight: 0, offsetWidth: 0, offsetHeight: 0,
    dataset: {},
    style: {},
    attributes: {},
    listeners: {},
  };

  const classes = () => node.className.split(/\s+/).filter(Boolean);
  node.classList = {
    add(...cs) { const a = classes(); for (const c of cs) if (!a.includes(c)) a.push(c);
      node.className = a.join(" "); },
    remove(...cs) { node.className = classes().filter((c) => !cs.includes(c)).join(" "); },
    contains(c) { return classes().includes(c); },
    toggle(c, force) {
      const on = force === undefined ? !node.classList.contains(c) : !!force;
      if (on) node.classList.add(c); else node.classList.remove(c);
      return on;
    },
  };

  const adopt = (kid) => {
    const n = kid && typeof kid === "object" && kid.nodeType ? kid : textNode(kid);
    if (n.parentNode) n.parentNode.removeChild(n);
    n.parentNode = node;
    return n;
  };
  node.append = (...kids) => {
    for (const kid of kids) if (kid != null && kid !== false) node.childNodes.push(adopt(kid));
  };
  node.appendChild = (kid) => { node.append(kid); return kid; };
  node.removeChild = (kid) => {
    const i = node.childNodes.indexOf(kid);
    if (i >= 0) node.childNodes.splice(i, 1);
    kid.parentNode = null;
    return kid;
  };
  node.replaceChildren = (...kids) => { node.childNodes = []; node.append(...kids); };
  node.insertBefore = (fresh, ref) => {
    const at = node.childNodes.indexOf(ref);
    const n = adopt(fresh);
    if (at < 0) node.childNodes.push(n); else node.childNodes.splice(at, 0, n);
    return n;
  };
  node.remove = () => { if (node.parentNode) node.parentNode.removeChild(node); };

  node.setAttribute = (k, v) => {
    node.attributes[k] = String(v);
    if (k === "id") node.id = String(v);
    if (k === "class") node.className = String(v);
  };
  node.getAttribute = (k) => (k in node.attributes ? node.attributes[k] : null);
  node.hasAttribute = (k) => k in node.attributes;
  node.removeAttribute = (k) => { delete node.attributes[k]; };

  node.addEventListener = (type, fn) => { (node.listeners[type] ||= []).push(fn); };
  node.removeEventListener = (type, fn) => {
    node.listeners[type] = (node.listeners[type] || []).filter((f) => f !== fn);
  };
  node.dispatchEvent = (ev) => { fire(node, ev && ev.type); return true; };
  node.click = () => fire(node, "click");

  node.querySelector = (s) => queryAll(node, s)[0] || null;
  node.querySelectorAll = (s) => queryAll(node, s);
  node.closest = (s) => {
    const c = parseCompound(s);
    for (let n = node; n; n = n.parentNode) if (matchesCompound(n, c)) return n;
    return null;
  };
  node.contains = (other) => other === node || descendants(node).includes(other);
  node.getBoundingClientRect = () => ({ top: 0, left: 0, bottom: 0, right: 0, width: 0, height: 0 });
  node.focus = () => {};
  node.blur = () => {};
  node.scrollIntoView = () => {};

  Object.defineProperty(node, "textContent", {
    get: () => textOf(node),
    set: (v) => { node.childNodes = []; node.append(textNode(v)); },
  });
  Object.defineProperty(node, "children", {
    get: () => node.childNodes.filter((k) => k.nodeType === ELEMENT_NODE),
  });
  Object.defineProperty(node, "firstChild", { get: () => node.childNodes[0] || null });
  Object.defineProperty(node, "parentElement", { get: () => node.parentNode || null });
  return node;
}

/** Invoke a node's handlers for `type` — both addEventListener ones and an `onclick`-style prop. */
function fire(node, type) {
  const ev = {
    type, target: node, currentTarget: node, key: "",
    preventDefault() {}, stopPropagation() {},
  };
  const fns = [...(node.listeners[type] || [])];
  if (typeof node["on" + type] === "function") fns.push(node["on" + type]);
  if (!fns.length) throw new Error(`ui_smoke: nothing listens for "${type}" on <${node.tagName}>`);
  for (const fn of fns) fn(ev);
  return ev;
}

/* ============================ the world app.js loads into ============================ */

function jsonResponse(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    headers: { get: (k) => (String(k).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => data,
    text: async () => JSON.stringify(data),
  };
}

/** The page shell app.js expects to find — the ids and nav links from index.html. */
function buildShell(document) {
  const mk = (tag, id, cls) => {
    const n = document.createElement(tag);
    if (id) n.setAttribute("id", id);
    if (cls) n.className = cls;
    return n;
  };
  const shell = mk("div", "shell");
  const nav = mk("aside", "nav");
  const links = mk("nav", null, "nav-links");
  for (const route of ["home", "realms", "scenarios", "skills", "history", "settings"]) {
    const a = document.createElement("a");
    a.setAttribute("href", route === "home" ? "#/" : `#/${route}`);
    a.dataset.route = route;
    links.append(a);
  }
  nav.append(links, mk("span", "cap-dot", "cap-dot"), mk("span", "cap-label", "mono-micro"));
  const main = mk("main", "main");
  const view = mk("div", "view");
  main.append(view);
  shell.append(nav, main);
  const toasts = mk("div", "toasts");
  const modalRoot = mk("div", "modal-root");
  document.body.append(shell, toasts, modalRoot);
  return { shell, view, main, toasts, modalRoot };
}

/**
 * A fresh browser-ish world per check, so a modal (or a poll) from one cannot colour another.
 * `routes` is [{match: RegExp, body: any}] for the fetch stub; every URL asked for is recorded.
 */
function makeWorld(routes = []) {
  const fetchLog = [];
  const unmatched = [];
  const timers = new Map();
  let timerSeq = 0;

  const html = createElement("html");
  const body = createElement("body");
  html.append(body);
  const document = {
    nodeType: 9,
    documentElement: html,
    body,
    activeElement: null,
    listeners: {},
    createElement: (t) => createElement(t),
    createTextNode: (t) => textNode(t),
    getElementById: (id) => queryAll(html, `#${id}`)[0] || null,
    querySelector: (s) => queryAll(html, s)[0] || null,
    querySelectorAll: (s) => queryAll(html, s),
    addEventListener: (t, fn) => { (document.listeners[t] ||= []).push(fn); },
    removeEventListener: (t, fn) => {
      document.listeners[t] = (document.listeners[t] || []).filter((f) => f !== fn);
    },
  };
  const parts = buildShell(document);

  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
    clear: () => { store.clear(); },
  };

  const location = { hash: "", href: "http://127.0.0.1:8000/", pathname: "/", search: "" };
  const windowListeners = {};
  const window = {
    document, location, localStorage,
    innerWidth: 1440, innerHeight: 900, scrollX: 0, scrollY: 0,
    scrollTo: () => {},
    getComputedStyle: () => ({}),
    addEventListener: (t, fn) => { (windowListeners[t] ||= []).push(fn); },
    removeEventListener: (t, fn) => {
      windowListeners[t] = (windowListeners[t] || []).filter((f) => f !== fn);
    },
  };

  const fetchStub = async (path) => {
    const url = String(path);
    fetchLog.push(url);
    for (const r of routes) if (r.match.test(url)) return jsonResponse(r.body);
    unmatched.push(url);
    return jsonResponse({ detail: `ui_smoke: no fixture for ${url}` }, 404);
  };

  // Timers are recorded, never fired. app.js uses them for exactly two things — the toast
  // fade-out and the 2 s realm re-poll — and firing either would make the harness
  // time-dependent for no extra signal. The first poll tick is driven explicitly instead.
  const setTimeoutStub = (fn, ms) => { timers.set(++timerSeq, { fn, ms }); return timerSeq; };
  const clearTimer = (id) => { timers.delete(id); };

  const sandbox = {
    console,
    document, window, location, localStorage,
    fetch: fetchStub,
    setTimeout: setTimeoutStub, clearTimeout: clearTimer,
    setInterval: setTimeoutStub, clearInterval: clearTimer,
    FormData, TextDecoder, URLSearchParams, AbortController,
    confirm: () => true,
    alert: () => {},
    requestAnimationFrame: (fn) => setTimeoutStub(fn, 0),
  };
  vm.createContext(sandbox);
  return { sandbox, document, window, location, ...parts, fetchLog, unmatched, timers,
    windowListeners };
}

/** Evaluate app.js in `world`. Top-level `function`s land on the global by themselves; the
 *  script-scoped `const`s do not, so an epilogue hands out the one the checks need. */
function loadApp(world) {
  const src = readFileSync(APP_JS, "utf8");
  vm.runInContext(`${src}\n;globalThis.__ROUTES = ROUTES;\n`, world.sandbox, { filename: APP_JS });
  return world;
}

const boot = (routes = []) => loadApp(makeWorld(routes));

/** Let every already-resolved promise settle. All fixtures resolve immediately, so one macrotask
 *  turn drains the whole microtask chain a render awaits. */
const flush = async (turns = 3) => {
  for (let i = 0; i < turns; i++) await new Promise((r) => setImmediate(r));
};

/* ============================ assertions ============================ */

const trunc = (s, n = 500) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

function assert(cond, message) { if (!cond) throw new Error(message); }

function assertIncludes(haystack, needle, what) {
  const hay = String(haystack);
  if (!hay.includes(needle)) {
    throw new Error(`${what}: expected the rendered text to contain ${JSON.stringify(needle)}, `
      + `but it was ${JSON.stringify(trunc(hay))}`);
  }
}

function assertNoLibraryFetch(world, what) {
  const hits = world.fetchLog.filter((u) => u.includes("/api/skills/"));
  assert(hits.length === 0, `${what}: expected NO request to the skills library, but app.js asked `
    + `for ${JSON.stringify(hits)} (a local skill is not in the library — this is the bug)`);
}

/* ============================ fixtures ============================ */

const REALM_ID = "demo-realm-a1b2c3";
const INTEGRITY_DETAIL = "2 round(s) completed with no participant message: R1, R2.";
const OUTCOME = "vela wins 2-1 on the referee's ruling";
const FIRST_MESSAGE = "I open with a raise.";

const REALM_STATUS = {
  realm_id: REALM_ID, state: "archived", active: false, error: null,
  outcome: OUTCOME,
  integrity: [{ code: "rounds_without_participation", detail: INTEGRITY_DETAIL }],
  messages: 2, events: 14,
  spend: { vela: 0.41, nash: 0.22 }, total_spend: 0.63,
  tokens: { vela: 12000, nash: 8000 }, total_tokens: 20000,
  scores: { vela: 2, nash: 1 }, score_ledger: { vela: 2, nash: 1 }, score_discrepancy: false,
  violations: [{ agent: "nash", reason: "spoke out of turn" }],
  config: {
    package: "demo-realm", provider: "anthropic", free_response: false,
    referee: "arbiter", referee_opens: true, provide_tools: true,
    parameters: { stakes: "high", note: "" },
    turns: { policy: "round_robin", enforcement: "physics", order: ["vela", "nash"],
      referee_cue: "your turn", silence_timeout_s: 120 },
    termination: [{ type: "referee_verdict" }, { type: "max_messages", limit: 200 }],
    mechanics: [{ kind: "poker", ruleset: "holdem" }],
    environment: { network_egress: "none", shared_folder: true, require_mention: true },
    agents: [
      { id: "arbiter", role: "referee", model: "claude-opus", model_category: "large",
        budget_usd: 5, on_exhausted: "starve", skills: ["builtin:referee-basics"], tools: [] },
      { id: "vela", role: "participant", model: "claude-sonnet", model_category: "medium",
        budget_usd: 2, on_exhausted: "starve_then_kill", skills: ["local:pot-odds"],
        tools: ["web_fetch"] },
      { id: "nash", role: "participant", model: "claude-sonnet", model_category: "medium",
        budget_usd: 2, on_exhausted: "starve_then_kill", skills: [], tools: [] },
    ],
  },
};

const TRANSCRIPT = {
  channels: { "!commons:realm.local": "commons" },
  messages: [
    { ts: 1, channel: "!commons:realm.local", sender: `@${REALM_ID}-vela:realm.local`,
      body: FIRST_MESSAGE },
    { ts: 2, channel: "!commons:realm.local", sender: `@${REALM_ID}-arbiter:realm.local`,
      body: "Round 1 to vela." },
  ],
};

const REALM_ROUTES = [
  { match: /^\/api\/settings$/, body: { active: 1, capacity: 4 } },
  { match: /^\/api\/packages\/[^/?]+$/,
    body: { referee: "arbiter", agents: [{ id: "arbiter" }, { id: "vela" }, { id: "nash" }] } },
  { match: /^\/api\/realms\/[^/?]+\/transcript/, body: TRANSCRIPT },
  { match: /^\/api\/realms\/[^/?]+\/outputs$/,
    body: { outputs: [{ path: "hand-history.md", bytes: 2048, available: true }] } },
  { match: /^\/api\/realms\/[^/?]+$/, body: REALM_STATUS },
];

const LOCAL_SKILL_TEXT = "# Pricing a hand\n\nbody text";

const PREVIEW_AGENT = {
  id: "vela", name: "Vela", role: "participant", model_category: "medium",
  budget_ref: { max_usd: 2, on_exhausted: "starve_then_kill" },
  skills: ["builtin:competitor", "local:pot-odds"],
  persona: "Patient, ruthless at the river.",
  goals: ["End with the biggest stack."],
};

const editorAgent = () => ({
  id: "vela", name: "Vela", role: "participant", model_category: "medium", color: null,
  budget: { max_usd: 2, on_exhausted: "starve_then_kill", grace_period: "5m" },
  private_messaging: { enabled: false, include_referee: false },
  skills: ["builtin:agent-basics", "local:pot-odds"],
  local_skills: { "pot-odds": "LOCAL-SKILL-BODY" },
  tools: ["web_fetch"],
  persona: "Patient.", rubric: "", goals: ["Win."],
});

const LIBRARY_SKILLS = [
  { source: "builtin", ref: "agent-basics" },
  { source: "local", ref: "pot-odds" },
];
const INSTALLED_TOOLS = [{ name: "web_fetch", label: "Web fetch", ready: true, risk: "standard",
  description: "Fetch a URL", api_key_ref: "web", cost_per_call_usd: 0 }];

/** The one element in `root` that is a skill pill for `ref` and has a click handler. */
function findSkillPill(root, ref) {
  const pills = descendants(root).filter((n) =>
    n.classList.contains("skill-pill") && textOf(n).includes(ref) && (n.listeners.click || []).length);
  assert(pills.length === 1,
    `expected exactly one clickable skill pill for "${ref}", found ${pills.length}`);
  return pills[0];
}

/* ============================ the checks ============================ */

const CHECKS = [
  {
    // Bugs of every shape start here: if app.js cannot even be evaluated, nothing below means
    // anything. A name that has moved or been renamed is a failure, never a skip.
    name: "load_smoke",
    async run() {
      const world = boot();
      const required = ["el", "showSkill", "agentDetailCard", "agentBlock", "render", "modal"];
      const missing = required.filter((n) => typeof world.sandbox[n] !== "function");
      assert(!missing.length, `app.js no longer defines ${missing.join(", ")} as top-level `
        + "functions — the checks below drive them by name");
      const realmRoute = world.sandbox.__ROUTES.find(([re]) => re.test(`/realm/${REALM_ID}`));
      assert(realmRoute, "no route matches /realm/<id> — the realm view's entry point has moved");
      // loading must not render: the only top-level side effects are two window listeners
      assert(Object.keys(world.windowListeners).sort().join(",") === "hashchange,load",
        `loading app.js registered ${JSON.stringify(Object.keys(world.windowListeners))} on `
        + "window; expected exactly hashchange + load");
      assert(world.fetchLog.length === 0,
        `loading app.js called the API: ${JSON.stringify(world.fetchLog)}`);
      assert(textOf(world.view) === "", "loading app.js rendered into #view");
      return `${required.length} functions + ${world.sandbox.__ROUTES.length} routes defined`;
    },
  },
  {
    // Bug 3, half one: a local skill's text travels with the page that shows it. Asking the
    // platform library for it 404s, because a local skill by definition is not in the library.
    name: "local_skill_opens_without_touching_the_library",
    async run() {
      const world = boot();
      await world.sandbox.showSkill("local", "pot-odds", LOCAL_SKILL_TEXT);
      assertNoLibraryFetch(world, "showSkill with preloaded text");
      assertIncludes(textOf(world.modalRoot), "body text", "the skill modal");
      return "modal rendered from the preloaded text, 0 API calls";
    },
  },
  {
    // …and half two: the fallback is still there for callers that hold no text (the library page).
    name: "local_skill_without_text_still_asks_the_library",
    async run() {
      const world = boot([{ match: /^\/api\/skills\/local\/pot-odds$/,
        body: { content: "FROM-THE-LIBRARY", files: ["SKILL.md"] } }]);
      await world.sandbox.showSkill("local", "pot-odds");
      assert(world.fetchLog.includes("/api/skills/local/pot-odds"),
        "showSkill with no preloaded text must fall back to the library, but it fetched "
        + JSON.stringify(world.fetchLog));
      assertIncludes(textOf(world.modalRoot), "FROM-THE-LIBRARY", "the skill modal");
      return "fell back to GET /api/skills/local/pot-odds";
    },
  },
  {
    // THE check for bug 3: serialize_project put the text in `skill_contents`, and nothing in
    // app.js read it — the pill still called the library and the reader got "Couldn't load skill".
    // Syntax checks and the Python suite were both green.
    name: "scenario_preview_hands_a_local_skill_its_text",
    async run() {
      const world = boot();
      const card = world.sandbox.agentDetailCard(PREVIEW_AGENT,
        { "local:pot-odds": "PRELOADED-MARKER" });
      const pill = findSkillPill(card, "pot-odds");
      fire(pill, "click");
      await flush();
      assertIncludes(textOf(world.modalRoot), "PRELOADED-MARKER", "the skill modal");
      assertNoLibraryFetch(world, "clicking a local skill pill in the scenario preview");
      return "the pill opened its own text; 0 API calls";
    },
  },
  {
    // THE check for bugs 1 and 2: drive the real #/realm/<id> view through the real router and
    // let its first poll tick complete. A mis-scoped identifier read by `tick` throws here, where
    // `node --check` and the Python suite both see nothing at all.
    name: "realm_view_renders_a_full_tick",
    async run() {
      const world = boot(REALM_ROUTES);
      // The view schedules itself with poll(fn, 2000); keep the first tick, drop the timer, so a
      // throw inside it is awaited rather than lost to an unhandled rejection.
      let ticked = null;
      world.sandbox.poll = (fn) => { ticked = (async () => fn())(); };

      world.location.hash = `#/realm/${REALM_ID}`;
      await world.sandbox.render();
      assert(ticked, "the realm view never called poll() — its entry point or scheduling changed");
      await ticked;          // a ReferenceError inside tick surfaces HERE
      await flush();

      const rendered = textOf(world.view);
      assert(!rendered.includes("Something went wrong"),
        `the realm route threw before its first tick: ${trunc(rendered)}`);
      assertIncludes(rendered, INTEGRITY_DETAIL, "the realm view's integrity banner");
      assertIncludes(rendered, OUTCOME, "the realm view's outcome banner");
      assertIncludes(rendered, FIRST_MESSAGE, "the realm view's feed");
      assert(!world.unmatched.length,
        `the realm view called endpoints with no fixture: ${JSON.stringify(world.unmatched)}`);
      return `${world.fetchLog.length} API calls, ${rendered.length} chars rendered`;
    },
  },
  {
    // The other side of bug 3's fix: the editor edits a local skill's text in the agent that
    // carries it, so the block must render its editor from `local_skills` and not go shopping.
    name: "editor_agent_block_renders_local_skills",
    async run() {
      const world = boot();
      const agent = editorAgent();
      const S = { agents: [agent] };
      const block = world.sandbox.agentBlock(S, agent, 0, LIBRARY_SKILLS, [], () => {},
        INSTALLED_TOOLS);
      await flush();
      assertIncludes(textOf(block), "Local skills", "the editor's agent block");
      const box = descendants(block).find((n) => n.tagName === "TEXTAREA"
        && n.value === "LOCAL-SKILL-BODY");
      assert(box, "no textarea holds the agent's local skill text; the block's textareas held "
        + JSON.stringify(descendants(block).filter((n) => n.tagName === "TEXTAREA")
          .map((n) => trunc(String(n.value), 60))));
      assertNoLibraryFetch(world, "an agent block whose local skill text is already in hand");
      return "local skill editor prefilled from the agent, 0 API calls";
    },
  },
];

/* ============================ runner ============================ */

const oneLine = (s) => trunc(String(s).replace(/\s*\n\s*/g, " ⏎ "), 600);

const failures = [];
for (const check of CHECKS) {
  let status = "PASS", detail = "";
  try {
    detail = (await check.run()) || "ok";
  } catch (err) {
    status = "FAIL";
    detail = err && err.message ? err.message : String(err);
    failures.push([check.name, err]);
  }
  console.log(`CHECK ${check.name} ${status} ${oneLine(detail)}`);
}
console.log(`SUMMARY ${CHECKS.length} checks, ${failures.length} failed`);

if (failures.length) {
  console.error("");
  for (const [name, err] of failures) {
    console.error(`--- ${name} ---`);
    console.error(err && err.stack ? err.stack : String(err));
    console.error("");
  }
  process.exit(1);
}
