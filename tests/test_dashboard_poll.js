// Plain-node tests for the now-playing poll's response ordering in
// static/js/dashboard-page.js. Two in-flight polls can resolve out of order (a
// response only has to outlive the 15s interval), and the handlers used to
// apply whichever landed LAST: stale data over newer, a late failure counted
// against a healthy feed - and, worst, a slow success resolving after the 401
// branch had stopped both timers un-dimmed the card and cleared the Stale
// pill on a poller that will never poll again. Same sequence-number fix, for
// the same reason, as profile-page.js/admin-page.js (an AbortController
// REJECTS the superseded promise, which here would count toward the stale
// threshold).
//
// Unlike tests/test_dashboard_page.js (which requires the module with an
// empty DOM so the poll IIFE bails), this file pre-fills the DOM stub so the
// IIFE wires up, captures poll() through a VisibilityPoll stub, and drives
// fetch by hand. Run with: node tests/test_dashboard_poll.js
const assert = require('assert');

const MODULE_PATH = require.resolve('../static/js/dashboard-page.js');

function makeClassList() {
  const names = new Set();
  return {
    names,
    toggleCalls: [],
    add(n) { names.add(n); },
    remove(n) { names.delete(n); },
    toggle(n, on) {
      this.toggleCalls.push([n, on]);
      if (on) names.add(n); else names.delete(n);
    },
    contains(n) { return names.has(n); },
  };
}

function makeEl(id) {
  return {
    id,
    textContent: '',
    style: {},
    classList: makeClassList(),
    setAttribute() {},
    getAttribute() { return null; },
    removeAttribute() {},
    appendChild() {},
  };
}

/* Load a FRESH copy of the module (its seq counter and `playing` anchor are
 * module state) against a DOM stub carrying the now-playing card, a
 * VisibilityPoll stub that captures the callbacks instead of scheduling
 * them, and a fetch stub whose settlement the test controls. */
function freshPage() {
  const elements = {};
  for (const id of ['nowPlayingCard', 'nowPlayingPanel', 'nowPlayingName',
                    'nowPlayingArtists', 'nowPlayingState', 'nowPlayingCover',
                    'nowPlayingCoverLink', 'friendsListening', 'friendsListeningChips',
                    'friendsListeningMore']) {
    elements[id] = makeEl(id);
  }

  const handles = [];
  global.window = {
    VisibilityPoll: {
      start(fn, _intervalMs) {
        const handle = { fn, stopped: 0, stop() { this.stopped += 1; } };
        handles.push(handle);
        return handle;
      },
    },
  };

  const pendingFetches = [];
  global.fetch = () => new Promise((resolve, reject) => {
    pendingFetches.push({ resolve, reject });
  });

  global.document = {
    body: { addEventListener() {} },
    addEventListener() {},
    getElementById(id) { return elements[id] || null; },
    querySelector() { return null; },
    createElement(tag) { return Object.assign(makeEl(''), { tag }); },
    createTextNode(text) { return { text }; },
  };

  delete require.cache[MODULE_PATH];
  require(MODULE_PATH);

  assert.strictEqual(handles.length, 2, 'the poll and the progress tick must both be scheduled');
  return {
    elements,
    poll: handles[0].fn,          //< scheduled first (see the IIFE's last lines)
    pollHandle: handles[0],
    tickHandle: handles[1],
    pendingFetches,
  };
}

function okResponse(payload) {
  return { status: 200, ok: true, json: () => Promise.resolve(payload) };
}

function nowPlaying(name) {
  return { nowPlaying: { name, isPaused: false, positionMs: 0, durationMs: 1000 },
           friends: [], friendsMoreCount: 0 };
}

//< drains the whole microtask chain (.then -> json() -> .then), not just one hop
function settle() { return new Promise((resolve) => setImmediate(resolve)); }

const tests = [];
function run(name, fn) { tests.push([name, fn]); }

run('a superseded response does not overwrite the newer one', async () => {
  const page = freshPage();

  page.poll();   //< A - will resolve LAST despite being sent first
  page.poll();   //< B
  page.pendingFetches[1].resolve(okResponse(nowPlaying('Newer')));
  await settle();
  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer');

  page.pendingFetches[0].resolve(okResponse(nowPlaying('Older')));
  await settle();

  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer',
                     'the stale response must not win over the one that superseded it');
});

run('a body that arrives after a newer poll does not overwrite it', async () => {
  /* The guard the first test cannot reach: fetch resolves on HEADERS, and
   * resp.json() awaits the BODY - a slow body read spans the next 15s tick,
   * so a response can pass the first check as the latest and still be
   * superseded by the time its payload exists. Pins the re-check after
   * json(). */
  const page = freshPage();
  let releaseBody;
  const slowBody = new Promise((resolve) => { releaseBody = resolve; });

  page.poll();   //< A: headers arrive while it is still the latest...
  page.pendingFetches[0].resolve({ status: 200, ok: true, json: () => slowBody });
  await settle();   //< ...so it passes the first check and starts on the body

  page.poll();   //< B: fires mid-download and completes whole
  page.pendingFetches[1].resolve(okResponse(nowPlaying('Newer')));
  await settle();
  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer');

  releaseBody(nowPlaying('Older'));   //< A's body finally lands
  await settle();

  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer',
                     'a response must be re-checked after its body read');
});

run('a 401 acts while it is still the newest word', async () => {
  /* The rule is "act unless something NEWER already acted" - not "act only if
   * no newer request is in flight". An earlier draft used the latter, and it
   * is what made the sustained-latency cases below freeze: when every response
   * outlives the next tick, every response is superseded-in-flight forever, so
   * an expired session never stopped the poll. A 401 is evidence about the
   * SESSION, which both in-flight requests share, so the first one to speak
   * gets to stop the poller. */
  const page = freshPage();

  page.poll();   //< A - comes back 401 first
  page.poll();   //< B - still in flight, same expired session
  page.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  assert.strictEqual(page.pollHandle.stopped, 1, 'the 401 stops the poll');
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.ok(page.elements.nowPlayingCard.classList.contains('is-stale'));
});

run('an accepted 401 stays terminal when a newer response succeeds', async () => {
  const page = freshPage();

  page.poll();   //< A - accepted session expiry
  page.poll();   //< B - already in flight when A speaks
  page.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  page.pendingFetches[1].resolve(okResponse(nowPlaying('Late')));
  await settle();

  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.ok(page.elements.nowPlayingCard.classList.contains('is-stale'));
  assert.notStrictEqual(page.elements.nowPlayingName.textContent, 'Late');
  assert.ok(page.elements.friendsListening.classList.contains('is-stale'));
  assert.strictEqual(page.elements.nowPlayingState.textContent, 'Stale');
});

run('a deferred body cannot revive a poll after an accepted 401', async () => {
  const page = freshPage();
  let releaseBody;
  const body = new Promise((resolve) => { releaseBody = resolve; });

  page.poll();   //< A - body read is pending
  page.pendingFetches[0].resolve({ status: 200, ok: true, json: () => body });
  await settle();

  page.poll();   //< B - accepted expiry while A waits on its body
  page.pendingFetches[1].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  releaseBody(nowPlaying('Late body'));
  await settle();

  assert.ok(page.elements.nowPlayingCard.classList.contains('is-stale'));
  assert.notStrictEqual(page.elements.nowPlayingName.textContent, 'Late body');
  assert.ok(page.elements.friendsListening.classList.contains('is-stale'));
  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
});

run('a late failure cannot claim or stop an already terminal poll', async () => {
  const page = freshPage();

  page.poll();   //< A - accepted session expiry
  page.poll();   //< B, C, D - failures after A has stopped the poll
  page.poll();
  page.poll();
  page.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();
  for (const pending of page.pendingFetches.slice(1)) pending.reject(new Error('late failure'));
  await settle();

  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.strictEqual(page.elements.nowPlayingCard.classList.toggleCalls
    .filter(([name, on]) => name === 'is-stale' && on).length, 1);
});

run('a repeated 401 does not stop the terminal handles twice', async () => {
  const page = freshPage();

  page.poll();
  page.poll();
  page.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();
  page.pendingFetches[1].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  const pendingBefore = page.pendingFetches.length;
  page.poll();

  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.strictEqual(page.pendingFetches.length, pendingBefore);
});

run('a response superseded by an APPLIED newer one still stands down', async () => {
  /* The other half of the rule, and what keeps the out-of-order protection:
   * once a newer response has actually been applied, an older one must not
   * speak - not even a 401. */
  const page = freshPage();

  page.poll();   //< A - 401, but it arrives last
  page.poll();   //< B - succeeds first and applies
  page.pendingFetches[1].resolve(okResponse(nowPlaying('Newer')));
  await settle();
  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Newer');

  page.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  assert.strictEqual(page.pollHandle.stopped, 0,
                     'an older 401 cannot undo a newer response that already landed');
  assert.ok(!page.elements.nowPlayingCard.classList.contains('is-stale'));
});

/* VisibilityPoll.start is a bare setInterval with no in-flight guard, so on a
 * connection where /api/now-playing consistently takes longer than the 15s
 * interval, EVERY response is superseded before it settles. Guarding on "is
 * this still the last request issued" dropped all of them: no render, no
 * failure counted, no Stale pill, and an expired session that never stopped
 * the poll - the frozen-card defect this block exists to prevent, on exactly
 * the connections that need it most. Each case below drives poll N+1 before
 * settling poll N, ten times over. */
async function runSustainedLatency(settleOne) {
  const page = freshPage();
  page.poll();
  for (let i = 2; i <= 10; i++) {
    page.poll();
    settleOne(page.pendingFetches[i - 2], i - 1);
    await settle();
  }
  return page;
}

async function runSustainedExpiredSession() {
  const page = freshPage();
  for (let i = 0; i < 10; i++) page.poll();
  for (const pending of page.pendingFetches) {
    pending.resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
    await settle();
  }
  return page;
}

run('a feed slower than the poll interval still renders', async () => {
  const page = await runSustainedLatency((f, n) => f.resolve(okResponse(nowPlaying('Track' + n))));

  assert.strictEqual(page.elements.nowPlayingName.textContent, 'Track9',
                     'every response was superseded in flight, so none was ever applied');
});

run('a feed slower than the poll interval still goes Stale', async () => {
  const page = await runSustainedLatency((f) => f.reject(new Error('dropped')));

  assert.ok(page.elements.nowPlayingCard.classList.contains('is-stale'),
            'sustained failures under latency must still reach the stale threshold');
  assert.strictEqual(page.elements.nowPlayingState.textContent, 'Stale');
});

run('an expired session under latency still stops the poll', async () => {
  const page = await runSustainedExpiredSession();

  assert.ok(page.pollHandle.stopped > 0, 'the 401 branch must be reachable under latency');
  assert.ok(page.tickHandle.stopped > 0);
});

run('a slow success cannot un-freeze the card after the 401 stop', async () => {
  const page = freshPage();
  const card = page.elements.nowPlayingCard;

  page.poll();   //< A - still in flight when the session expires
  page.poll();   //< B - meets the expired session
  page.pendingFetches[1].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();
  assert.strictEqual(page.pollHandle.stopped, 1);
  assert.strictEqual(page.tickHandle.stopped, 1);
  assert.ok(card.classList.contains('is-stale'), 'the 401 freezes the card');
  assert.strictEqual(page.elements.nowPlayingState.textContent, 'Stale');

  page.pendingFetches[0].resolve(okResponse(nowPlaying('Late')));
  await settle();

  assert.ok(card.classList.contains('is-stale'),
            'nothing will ever poll again, so the late success must not present the card as live');
  assert.notStrictEqual(page.elements.nowPlayingName.textContent, 'Late',
                        'the late payload must not render either');
});

run('superseded failures do not count toward the stale threshold', async () => {
  const page = freshPage();
  const card = page.elements.nowPlayingCard;

  page.poll();   //< A, B, C - three requests the network will drop, slowly
  page.poll();
  page.poll();
  page.poll();   //< D - the live one, and it succeeds
  page.pendingFetches[3].resolve(okResponse(nowPlaying('Healthy')));
  await settle();
  assert.ok(!card.classList.contains('is-stale'));

  for (const i of [0, 1, 2]) page.pendingFetches[i].reject(new Error('dropped'));
  await settle();

  assert.ok(!card.classList.contains('is-stale'),
            'three STALE failures against a healthy feed must not mark it out of date');
});

(async () => {
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      console.error(`FAIL - ${name}`);
      console.error(err);
      process.exit(1);
    }
  }
  console.log('All dashboard-poll tests done.');
})();
