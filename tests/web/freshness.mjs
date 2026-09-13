// Exercise the PWA's freshness decision core and its fetch deadline (server/web/app.js) — the machinery
// that decides whether the dashboard is allowed to look live. Driven by tests/test_pwa_freshness.py so it
// runs inside the normal suite; also runnable bare:
//   node tests/web/freshness.mjs
//
// WHY THIS EXISTS. 2026-09-13: the app sat on frozen readings behind a green "live" dot, and the operator
// reasonably read them as current — which skewed a power-regression question for a fortnight. Three
// separate things had to be true for that lie to hold, and each is asserted below:
//   1. fetch had no deadline, so a hung connection never rejected and the catch that sets "down" never ran;
//   2. freshness came from the server-computed `age_s` INSIDE the payload, which freezes with the data;
//   3. polls stacked every 5s against a ~6-connection cap, wedging recovery long after the fault cleared.
import { readFileSync } from "node:fs";
const APP = new URL("../../server/web/app.js", import.meta.url);
const src = readFileSync(APP, "utf8");

// Pull the real declarations out of the shipped file so this tests what ships, not a copy. The scanner
// skips comments and string literals, because several of these declarations carry trailing `//` notes and
// the naive "first ; or matching }" both land in the wrong place on them.
function scan(from, stopAtSemicolon) {
  let depth = 0, quote = null, line = false, block = false;
  for (let k = from; k < src.length; k++) {
    const c = src[k], n = src[k + 1];
    if (line) { if (c === "\n") line = false; continue; }
    if (block) { if (c === "*" && n === "/") { block = false; k++; } continue; }
    if (quote) { if (c === "\\") k++; else if (c === quote) quote = null; continue; }
    if (c === "/" && n === "/") { line = true; k++; continue; }
    if (c === "/" && n === "*") { block = true; k++; continue; }
    if (c === '"' || c === "'" || c === "`") { quote = c; continue; }
    if ("([{".includes(c)) depth++;
    else if (")]}".includes(c)) { if (--depth === 0 && !stopAtSemicolon) return k; }
    else if (c === ";" && depth === 0 && stopAtSemicolon) return k;
  }
  throw new Error("unterminated declaration");
}
const grabConst = (name) => {
  const i = src.indexOf(`const ${name} =`);
  if (i < 0) throw new Error(`missing const ${name}`);
  return src.slice(i, scan(i, true) + 1);
};
const grabFn = (name) => {
  const i = src.indexOf(`async function ${name}(`);
  if (i < 0) throw new Error(`missing ${name}`);
  const parenEnd = scan(src.indexOf("(", i), false);   // step over the (possibly destructuring) params
  return src.slice(i, scan(src.indexOf("{", parenEnd), false) + 1);
};

const mod = await import("data:text/javascript," + encodeURIComponent(`
  ${grabConst("STALE_AFTER_S")}
  ${grabConst("dataAge")}
  ${grabConst("isStale")}
  ${grabConst("statusDot")}
  ${grabConst("FETCH_TIMEOUT_MS")}
  ${grabConst("POLL_TIMEOUT_MS")}
  ${grabConst("ADMIN_TIMEOUT_MS")}
  ${grabFn("fetchJSON")}
  ${grabFn("getJSON")}
  export { STALE_AFTER_S, dataAge, isStale, statusDot, getJSON,
           FETCH_TIMEOUT_MS, POLL_TIMEOUT_MS, ADMIN_TIMEOUT_MS };`));
const { STALE_AFTER_S, dataAge, isStale, statusDot, getJSON,
        FETCH_TIMEOUT_MS, POLL_TIMEOUT_MS, ADMIN_TIMEOUT_MS } = mod;

let fails = 0;
const ok = (name, cond, extra = "") => { if (!cond) { fails++; console.log(`FAIL ${name} ${extra}`); }
  else console.log(`ok   ${name}`); };

// ── dataAge ─────────────────────────────────────────────────────────────────
const T = 1_700_000_000_000;
ok("no successful fetch yet → null", dataAge(null, T) === null);
ok("age in seconds", dataAge(T - 45_000, T) === 45);
ok("age is never negative (clock skew)", dataAge(T + 5_000, T) === 0);

// ── isStale ─────────────────────────────────────────────────────────────────
ok("null age is not stale", isStale(null) === false);
ok("fresh", isStale(5) === false);
ok("one slow poll does not trip it", isStale(12) === false);
ok("exactly at threshold is not yet stale", isStale(STALE_AFTER_S) === false);
ok("past threshold", isStale(STALE_AFTER_S + 0.1) === true);
ok("threshold is several polls, not one", STALE_AFTER_S >= 15, `STALE_AFTER_S=${STALE_AFTER_S}`);

// ── statusDot — the regression that started all this ────────────────────────
ok("initial state is neutral", statusDot("init", false) === "");
ok("healthy is live", statusDot("live", false) === "live");
// THE BUG: last attempt succeeded, data has since aged (throttled/suspended tab). Must NOT be green.
ok("aged data is never live", statusDot("live", true) === "stale");
ok("failing requests are red", statusDot("down", true) === "down");
ok("failing outranks merely-aged", statusDot("down", false) === "down");
ok("no state renders green unless genuinely fresh",
   ["init", "live", "down"].every((s) => statusDot(s, true) !== "live"));

// ── deadlines are ordered sensibly ──────────────────────────────────────────
ok("poll deadline is the tightest", POLL_TIMEOUT_MS < FETCH_TIMEOUT_MS);
ok("admin writes get the most room", ADMIN_TIMEOUT_MS > FETCH_TIMEOUT_MS);
// The poll deadline also bounds RECOVERY: the in-flight guard skips ticks while a request is outstanding,
// so a slack deadline would leave the app sulking after the server was already back.
ok("poll deadline bounds recovery to a few ticks", POLL_TIMEOUT_MS <= 10_000, `${POLL_TIMEOUT_MS}ms`);

// ── getJSON actually aborts a hung connection ───────────────────────────────
const realFetch = globalThis.fetch;
let sawAbort = false;

globalThis.fetch = (_u, opts = {}) => new Promise((_res, rej) => {
  // a hung socket: never resolves, never rejects on its own
  opts.signal?.addEventListener("abort", () => {
    sawAbort = true;
    const e = new Error("aborted"); e.name = "AbortError"; rej(e);
  });
});
const t0 = Date.now();
let threw = null;
try { await getJSON("/api/v1/sensors", { timeoutMs: 300 }); } catch (e) { threw = e; }
const elapsed = Date.now() - t0;
ok("a hung request REJECTS instead of hanging forever", threw !== null);
ok("the abort signal fired", sawAbort);
ok("it rejects at the deadline", elapsed >= 250 && elapsed < 2000, `${elapsed}ms`);
ok("the error names the timeout", threw && /timeout/i.test(threw.message), threw && threw.message);

// A body that never finishes streaming must also be caught — headers arriving is not success. Per spec,
// aborting a fetch tears down the response stream too, so a real Response.json() rejects with AbortError;
// the stub models that. What this actually pins down is that fetchJSON reads the body BEFORE clearing the
// deadline: refactor it to return the Response and parse at the call site, and the timer is already gone
// when the body stalls, so nothing ever aborts and this fails.
globalThis.fetch = (_u, opts = {}) => Promise.resolve({
  ok: true,
  status: 200,
  json: () => new Promise((_res, rej) => {
    if (opts.signal?.aborted) { const e = new Error("aborted"); e.name = "AbortError"; return rej(e); }
    opts.signal?.addEventListener("abort", () => {
      const e = new Error("aborted"); e.name = "AbortError"; rej(e);
    });
  }),
});
let bodyThrew = null;
const tb = Date.now();
try { await getJSON("/api/v1/sensors", { timeoutMs: 300 }); } catch (e) { bodyThrew = e; }
ok("a hung BODY read is bounded too", bodyThrew !== null, bodyThrew && bodyThrew.message);
ok("the body deadline is the same deadline", Date.now() - tb < 2000, `${Date.now() - tb}ms`);

// non-OK status still surfaces as an error
globalThis.fetch = () => Promise.resolve({ ok: false, status: 503, json: () => Promise.resolve({}) });
let statusThrew = null;
try { await getJSON("/api/v1/sensors", { timeoutMs: 300 }); } catch (e) { statusThrew = e; }
ok("HTTP errors still throw", statusThrew !== null && /503/.test(statusThrew.message));

// the happy path still returns parsed JSON
globalThis.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ sensors: [1] }) });
const good = await getJSON("/api/v1/sensors", { timeoutMs: 300 });
ok("healthy responses parse", good && Array.isArray(good.sensors) && good.sensors[0] === 1);

globalThis.fetch = realFetch;

console.log(fails === 0 ? "\nall freshness checks passed" : `\n${fails} FAILED`);
process.exit(fails === 0 ? 0 : 1);
