// Headless UI test with jsdom (not part of the Python suite; needs `npm i jsdom`):  node tests/test_ui.js <path to jsdom dir>
const path = require("path"), fs = require("fs");
const { JSDOM } = require(require.resolve("jsdom", { paths: [process.argv[2] || "."] }));
const html = fs.readFileSync(path.join(__dirname, "..", "docs", "index.html"), "utf8");
const now = Math.floor(Date.now() / 1000);
const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true, url: "https://example.test/" });
const w = dom.window;
w.scrollTo = () => {}; w.fetch = () => Promise.reject(new Error("offline"));
w.matchMedia = () => ({ matches: false, addEventListener() {} });
Object.defineProperty(w.navigator, "serviceWorker", { value: { register: () => Promise.resolve({}), addEventListener() {} } });
w.localStorage.setItem("cfg", JSON.stringify({ server: "x", cmd: "c", status: "s" }));
const body = html.match(/<script>([\s\S]*)<\/script>/)[1];
w.eval(body + `\n;globalThis.__t = { get status() { return status; }, set status(v) { status = v; }, render, show, setDir, get dir() { return dir; }, pickHome, resetDay, dayChanged, get outbox() { return outbox; } };`);
const t = w.__t, $ = (id) => w.document.getElementById(id);
let fails = 0; const ok = (name, c) => { console.log((c ? "ok   " : "FAIL ") + name); if (!c) fails++; };

t.status = { t: now, watch: false, vapid: "x", defaults: { arrive: "9:30", leave_work: "5:30" },
  today: { arrive: "9:30", leave_work: "5:30", off: false, changed: false }, tomorrow: { arrive: "9:30", leave_work: "5:30", off: false, changed: false },
  morning: null, home: { t: now - 60, for: "5:20", best: "Leave desk 5:19 → 5:26 train → 9 at 5:32", now: "Leave now: walking is fastest, home ~5:52.",
    notes: [], rows: [["5:26", "5:19", "9 5:32 · wait 1m · 78%", "5:44"], ["5:30", "5:24", "walk home", "5:58"]] },
  learned: [{ k: "walk_to_bus_stop", v: 2.4, n: 3, p: 2 }, { k: "bus_to_platform", v: 2, n: 0, p: 2 }, { k: "desk_to_kendall", v: 5.1, n: 2, p: 4 }, { k: "red_nb", v: 10.8, n: 4 }] };
t.render();

// nav: 4 equal tabs, settings only behind the gear
ok("4 nav tabs, no Settings tab", w.document.querySelectorAll("nav button").length === 4 && ![...w.document.querySelectorAll("nav button")].some((b) => /settings/i.test(b.textContent)));
t.show("settings"); ok("gear opens Settings", !$("settings").classList.contains("hidden") && $("gearBtn").getAttribute("aria-pressed") === "true");
$("gearBtn").click(); ok("gear again returns", $("settings").classList.contains("hidden") && !$("now").classList.contains("hidden"));

// way home: the plan for 5:30 by default, the leave-now plan for an hour, then back
t.setDir("home"); ok("slider moved", $("segM").parentNode.dataset.pos === "1" && $("nowBody").classList.contains("in-right"));
ok("shows plan for 5:30", /Plan for 5:30/.test($("nowBody").innerHTML) && /wait 1m/.test($("nowBody").innerHTML));
t.status = { ...t.status, home_now: { t: now - 120, for: null, best: "NOW-PLAN", now: "Leave now: walking is fastest.", notes: [], rows: [["5:10", "5:03", "walk home", "5:30"]] } };
t.render(); ok("leave-now plan shown after pressing", /NOW-PLAN/.test($("nowBody").innerHTML) && /Leaving now/.test($("nowBody").innerHTML));
t.status = { ...t.status, home_now: null }; t.render(); ok("…and remembered when later statuses lack it", /NOW-PLAN/.test($("nowBody").innerHTML));
$("backBtn").click(); ok("back to the 5:30 plan on tap", /Plan for 5:30/.test($("nowBody").innerHTML));
w.localStorage.setItem("homeNow", JSON.stringify({ t: now - 3700, best: "OLD", rows: [["a", "b", "c", "d"]], notes: [] })); w.localStorage.setItem("homeDismiss", "0");
t.render(); ok("leave-now plan expires after an hour", !/OLD/.test($("nowBody").innerHTML));

// days: compact chips, autosave, optimistic reset
t.show("days"); const card = () => w.document.querySelector('#daysBody [data-w="today"]');
ok("days chips show pretty times", /9:30/.test(card().innerHTML) && /5:30/.test(card().innerHTML) && card().querySelectorAll(".tchip").length === 2);
ok("no Save button", !/Save today/.test($("daysBody").innerHTML));
const inp = card().querySelector('[data-f="leave"]'); inp.value = "18:15"; inp.dispatchEvent(new w.Event("change", { bubbles: true }));
ok("edit shows instantly + marks changed", /6:15/.test(card().innerHTML) && card().querySelector(".tchip.chg") && !card().querySelector("[data-reset]").hidden);
t.resetDay("today"); ok("reset reverts the display immediately", /5:30/.test(card().innerHTML) && !card().querySelector(".tchip.chg") && card().querySelector("[data-reset]").hidden);
ok("reset queued a command", t.outbox.some((m) => m === "today default"));

// learned rows
t.show("learned"); const rows = w.document.querySelectorAll(".lrow");
ok("learned shows icon rows with values", rows.length === 4 && /2\.4/.test($("learnedBody").innerHTML) && /🚪🚏/.test($("learnedBody").innerHTML));
ok("learned shows delta vs guess and default state", /▲ 0\.4 vs guess/.test($("learnedBody").innerHTML) && w.document.querySelectorAll(".lrow.dflt").length === 1);
t.status = { ...t.status, learned: [{ k: "walk_to_bus_stop", v: 2.7, n: 4, p: 2 }] }; t.render(); ok("value updates as data arrives", /2\.7/.test($("learnedBody").innerHTML) && w.document.querySelector(".lrow.fresh"));
console.log(fails ? `${fails} FAILED` : "UI OK"); process.exit(fails ? 1 : 0);
