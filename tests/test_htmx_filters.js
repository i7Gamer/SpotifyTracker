// Plain-node unit test for static/js/htmx-filters.js - the filter logic shared
// by every htmx-driven list page (/history and the three Top lists).
//
// These assertions started life in tests/test_history_page.js and moved here
// with the code when the Top lists became the second caller: the point of one
// shared helper is that both pages agree on "is this date range worth a
// request", so the test that pins that answer belongs to the helper, not to a
// page.
//
// No test framework/dependency - run with:
//   node tests/test_htmx_filters.js
const assert = require('assert');
const {
  rangeProblem,
  pruneEmptyParams,
  isNativeModifierClick,
  hidesTrendBuckets,
  syncFullPlaysFilter,
  showRangeError,
  syncCustomRange,
  failureUi,
  onSwapFailure,
  rememberFocusBeforeSwap,
  restoreFocusAfterSwap,
  requestPage,
  validateFormRequest,
  retryForm,
  RANGE_OK,
  RANGE_INCOMPLETE,
  RANGE_INVERTED,
} = require('../static/js/htmx-filters.js');

const RESULTS_ID = 'results';
const FORM_ID = 'filters';

function makeFilterDate(value) {
  const attrs = {};
  return {
    value,
    style: {},
    setAttribute(name, val) { attrs[name] = val; },
    removeAttribute(name) { delete attrs[name]; },
  };
}

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

// --- rangeProblem ------------------------------------------------------------

run('a named interval never needs dates', () => {
  assert.strictEqual(rangeProblem('week', '', ''), RANGE_OK);
  assert.strictEqual(rangeProblem('all time', '', ''), RANGE_OK);
});

run('an empty-string All Time is a named interval too', () => {
  // Every page now spells All Time "all time" (the Top pages moved off "" when
  // their default window became per-user - see charts.py's _topListFilters), but
  // "" still arrives from links made before that and still means All Time. It
  // must reach the same answer, or those URLs block every request.
  assert.strictEqual(rangeProblem('', '', ''), RANGE_OK);
});

run('a named interval ignores dates left over from a custom range', () => {
  // The controls keep their values while disabled, so a stale pair must not
  // make an otherwise fine "Last Week" look broken.
  assert.strictEqual(rangeProblem('week', '2026-05-01', '2026-01-01'), RANGE_OK);
});

run('a complete, ordered custom range is fine', () => {
  assert.strictEqual(rangeProblem('custom', '2026-01-01', '2026-05-01'), RANGE_OK);
});

run('the same day at both ends is a valid range, not an inversion', () => {
  assert.strictEqual(rangeProblem('custom', '2026-01-01', '2026-01-01'), RANGE_OK);
});

run('a half-entered custom range is incomplete rather than wrong', () => {
  // What every keystroke of typing a date looks like: no error shown, but no
  // request sent either.
  assert.strictEqual(rangeProblem('custom', '2026-01-01', ''), RANGE_INCOMPLETE);
  assert.strictEqual(rangeProblem('custom', '', '2026-01-01'), RANGE_INCOMPLETE);
  assert.strictEqual(rangeProblem('custom', '', ''), RANGE_INCOMPLETE);
});

run('start after end is reported as inverted', () => {
  assert.strictEqual(rangeProblem('custom', '2026-05-01', '2026-01-01'), RANGE_INVERTED);
});

run('incomplete and inverted are distinguishable', () => {
  // Both block the request; only one gets an error message, so collapsing them
  // to a single falsy "not ok" would either shout at every keystroke or stay
  // silent on a genuinely wrong range.
  assert.notStrictEqual(RANGE_INCOMPLETE, RANGE_INVERTED);
  assert.notStrictEqual(RANGE_INCOMPLETE, RANGE_OK);
  assert.notStrictEqual(RANGE_INVERTED, RANGE_OK);
});

// --- pruneEmptyParams --------------------------------------------------------

run('untouched controls are dropped from the request', () => {
  const result = pruneEmptyParams({ q: '', interval: 'week', tag: '', sort: '' });
  assert.deepStrictEqual(Object.keys(result).sort(), ['interval']);
});

run('a set value survives', () => {
  const result = pruneEmptyParams({ q: 'radiohead', interval: 'week', tag: 'chill' });
  assert.strictEqual(result.q, 'radiohead');
  assert.strictEqual(result.tag, 'chill');
  assert.strictEqual(result.interval, 'week');
});

run('a whitespace-only search counts as untouched and is dropped', () => {
  // Relocated from the retired tests/test_top_list.js, which pinned this for
  // applyTopListFilterParams: both pages' hand-written param code tested
  // `searchQuery.trim()`, so letting spaces through here would be a regression
  // - `?q=%20%20` in the address bar and a LIKE '% %' across all history.
  assert.deepStrictEqual(pruneEmptyParams({ q: '   ' }), {});
  assert.deepStrictEqual(pruneEmptyParams({ q: '\t\n' }), {});
});

run('a real query keeps the spaces the user typed around it', () => {
  // Blank-ness decides whether to send it; it does not rewrite the value.
  const result = pruneEmptyParams({ q: ' daft punk ' });
  assert.strictEqual(result.q, ' daft punk ');
});

run('"0" is kept - it is falsy but it is a value', () => {
  // The Top lists' "Full plays only" opt-out is exactly this: fullOnly=0 must
  // reach the route, because its absence means the OPPOSITE (the default is on).
  const result = pruneEmptyParams({ fullOnly: '0' });
  assert.strictEqual(result.fullOnly, '0');
});

run('the object is mutated in place, as htmx requires', () => {
  // htmx reads evt.detail.parameters back off the same object after the event,
  // so returning a fresh copy would prune nothing.
  const params = { q: '', interval: 'week' };
  const returned = pruneEmptyParams(params);
  assert.strictEqual(returned, params);
  assert.strictEqual('q' in params, false);
});

run('an already-clean object is left alone', () => {
  const result = pruneEmptyParams({ interval: 'week' });
  assert.deepStrictEqual(result, { interval: 'week' });
});

// --- shared page wiring ------------------------------------------------------

run('requestPage preserves filters and delegates URL replacement to htmx', () => {
  const results = { id: RESULTS_ID };
  const calls = [];
  global.window = { location: { pathname: '/history', search: '?q=liquid&page=9' } };
  global.document = { getElementById(id) { return id === RESULTS_ID ? results : null; } };
  global.htmx = { ajax(method, url, options) { calls.push({ method, url, options }); } };

  requestPage(2, RESULTS_ID);

  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].method, 'GET');
  const url = new URL(calls[0].url, 'http://localhost');
  assert.strictEqual(url.pathname, '/history');
  assert.strictEqual(url.searchParams.get('q'), 'liquid');
  assert.strictEqual(url.searchParams.get('page'), '2');
  assert.strictEqual(calls[0].options.source, results);
  assert.strictEqual(calls[0].options.replace, calls[0].url);
});

run('validateFormRequest ignores requests from unrelated elements', () => {
  const evt = {
    detail: { elt: { id: 'paginationLink' }, parameters: {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  validateFormRequest(evt, FORM_ID);
  assert.strictEqual(evt.prevented, 0);
});

run('validateFormRequest vetoes incomplete ranges before pruning', () => {
  const parameters = { q: '' };
  const evt = {
    detail: { elt: { id: FORM_ID }, parameters },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  global.document = {
    getElementById(id) {
      return {
        interval: { value: 'custom' },
        startDate: makeFilterDate('2026-01-01'),
        endDate: makeFilterDate(''),
        dateError: { textContent: '', style: {} },
      }[id] || null;
    },
  };
  validateFormRequest(evt, FORM_ID);
  assert.strictEqual(evt.prevented, 1);
  assert.deepStrictEqual(parameters, { q: '' });
});

run('validateFormRequest prunes a valid form request', () => {
  const parameters = { q: '', tag: 'chill' };
  const evt = {
    detail: { elt: { id: FORM_ID }, parameters },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  global.document = {
    getElementById(id) {
      return {
        interval: { value: 'week' },
        startDate: makeFilterDate(''),
        endDate: makeFilterDate(''),
        dateError: { textContent: '', style: {} },
      }[id] || null;
    },
  };
  validateFormRequest(evt, FORM_ID);
  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(parameters, { tag: 'chill' });
});

run('retryForm serializes the current form against its target', () => {
  const form = {
    id: FORM_ID,
    getAttribute(name) { return name === 'hx-get' ? '/history' : null; },
  };
  const calls = [];
  global.document = { getElementById(id) { return id === FORM_ID ? form : null; } };
  global.htmx = { ajax(method, url, options) { calls.push({ method, url, options }); } };

  retryForm(FORM_ID, RESULTS_ID);

  assert.deepStrictEqual(calls, [{
    method: 'GET',
    url: '/history',
    options: { source: form, target: '#' + RESULTS_ID, swap: 'innerHTML' },
  }]);
});

// --- syncFullPlaysFilter -----------------------------------------------------
// The "Full plays only" checkbox cannot be a plain form field: an unchecked box
// is not serialized at all, and an ABSENT fullOnly means the DEFAULT, which is
// ON. It therefore drives a hidden field carrying the explicit "1"/"0" the
// routes read - get that backwards and unticking the box turns the filter on.
//
// These two assertions moved here from tests/test_top_list_page.js with the
// code. /history renders the same partial now
// (templates/_full_plays_toggle.html), and this file is the only script all
// four pages that render it actually load. A copy left in top-list.js would
// still satisfy tests/test_inline_handler_targets.py - it scans every file in
// static/js regardless of which page loads which - and fail silently in the
// browser on /history.

function installCheckbox(checked, hiddenValue) {
  const elements = { fullPlaysOnly: { checked }, fullOnlyValue: { value: hiddenValue } };
  global.document = { getElementById(id) { return elements[id] || null; } };
  return elements.fullOnlyValue;
}

run('ticking "full plays only" writes the explicit 1 the route reads', () => {
  const hidden = installCheckbox(true, '0');

  syncFullPlaysFilter();

  assert.strictEqual(hidden.value, '1');
});

run('unticking it writes an explicit 0, because absent would mean ON', () => {
  const hidden = installCheckbox(false, '1');

  syncFullPlaysFilter();

  assert.strictEqual(hidden.value, '0', 'an unchecked box must not read as the default');
});

// --- isNativeModifierClick ---------------------------------------------------
// The regression this exists for: every delegated pagination handler hx-boost
// replaced exempted all four modifiers, and htmx's boost exempts only two.

run('shift-click and alt-click belong to the browser', () => {
  // htmx would otherwise swallow these - shift opens a new window, alt
  // downloads, and boost has no opinion about either.
  assert.strictEqual(isNativeModifierClick({ shiftKey: true }), true);
  assert.strictEqual(isNativeModifierClick({ altKey: true }), true);
});

run('an unmodified click is htmx\'s to handle', () => {
  assert.strictEqual(isNativeModifierClick({}), false);
});

run('ctrl and meta are not claimed here - htmx already passes those through', () => {
  // Listing them would read like this function is what makes new-tab work,
  // and someone would later "fix" htmx's own check by deleting it.
  assert.strictEqual(isNativeModifierClick({ ctrlKey: true }), false);
  assert.strictEqual(isNativeModifierClick({ metaKey: true }), false);
});

run('a missing event is not a modifier click', () => {
  assert.strictEqual(isNativeModifierClick(null), false);
  assert.strictEqual(isNativeModifierClick(undefined), false);
});

// --- hidesTrendBuckets -------------------------------------------------------
// /charts and /genres each carried this rule in a different spelling, one with
// a comment saying it mirrored the other. One spelling, one test.

run('a single-day view hides the Trend buckets control', () => {
  // Those views are bucketed by hour server-side, so the control is a no-op.
  assert.strictEqual(hidesTrendBuckets('today'), true);
  assert.strictEqual(hidesTrendBuckets('day'), true);
});

run('every multi-day interval keeps it', () => {
  ['week', 'month', 'year', '5years', 'all time', 'custom', ''].forEach((interval) => {
    assert.strictEqual(hidesTrendBuckets(interval), false, interval);
  });
});

// --- showRangeError / syncCustomRange's aria-invalid half --------------------
// FOLLOW-UP (2026-09-02 review): /history got role="alert" + aria-describedby
// (templates/history.html) + aria-invalid from this shared helper for its
// #dateError. The three Top pages share this file's showRangeError/
// syncCustomRange and templates/_page_card.html's markup, so one implementation
// gives every list page the same inverted-range accessibility state.

function makeDateInput() {
  const attrs = {};
  return {
    style: {},
    setAttribute(name, val) { attrs[name] = val; },
    removeAttribute(name) { delete attrs[name]; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; },
  };
}

function installDateControls(intervalValue) {
  const elements = {
    interval: { value: intervalValue === undefined ? 'custom' : intervalValue },
    startDate: makeDateInput(),
    endDate: makeDateInput(),
    dateError: { textContent: '', style: {} },
    customDates: { style: {} },
  };
  global.document = { getElementById(id) { return elements[id] || null; } };
  return elements;
}

run('an inverted range marks both date inputs aria-invalid', () => {
  const els = installDateControls();

  showRangeError(RANGE_INVERTED);

  assert.strictEqual(els.startDate.getAttribute('aria-invalid'), 'true');
  assert.strictEqual(els.endDate.getAttribute('aria-invalid'), 'true');
});

run('a valid range clears aria-invalid rather than setting it false', () => {
  const els = installDateControls();
  els.startDate.setAttribute('aria-invalid', 'true');
  els.endDate.setAttribute('aria-invalid', 'true');

  showRangeError(RANGE_OK);

  assert.strictEqual(els.startDate.getAttribute('aria-invalid'), null);
  assert.strictEqual(els.endDate.getAttribute('aria-invalid'), null);
});

run('an incomplete range (still typing) is not marked invalid', () => {
  const els = installDateControls();

  showRangeError(RANGE_INCOMPLETE);

  assert.strictEqual(els.startDate.getAttribute('aria-invalid'), null);
  assert.strictEqual(els.endDate.getAttribute('aria-invalid'), null);
});

run('syncCustomRange (the Time Period select path) carries the same aria-invalid sync', () => {
  // This is the path /top-songs, /top-artists and /top-albums actually call
  // (top-list.js's updateIntervalFilter) - showRangeError alone is not enough
  // to prove the Top pages benefit; it has to run from here too.
  const els = installDateControls('custom');
  els.startDate.value = '2026-05-01';
  els.endDate.value = '2026-01-01';   //< inverted

  syncCustomRange('customDates');

  assert.strictEqual(els.startDate.getAttribute('aria-invalid'), 'true');
  assert.strictEqual(els.endDate.getAttribute('aria-invalid'), 'true');
});

// --- failureUi ---------------------------------------------------------------
// Until the eight per-page copies of the failure block were merged here, this
// choice lived inside a page IIFE and could not be tested at all - which is why
// tests/test_ajax_loader_error_handling.py pins those loaders by source shape.

run('a swap target hosts the inline error', () => {
  assert.strictEqual(failureUi({ dataset: {} }), 'inline');
});

run('the choice is not configurable per element', () => {
  // It branched on data-htmx-failure="banner" once, and no template ever
  // emitted it - so the branch could not be taken and the "declarative" choice
  // had exactly one setting. An element that still carries the old attribute
  // must read as inline like any other, not as a knob that quietly works again.
  assert.strictEqual(failureUi({ dataset: { htmxFailure: 'banner' } }), 'inline');
});

run('no target at all falls back to the banner', () => {
  // There is nowhere to put an inline message, and saying nothing is the
  // failure mode this whole area exists to prevent.
  assert.strictEqual(failureUi(null), 'banner');
  assert.strictEqual(failureUi(undefined), 'banner');
});

run('an element with no dataset is not assumed to be inline', () => {
  // document/window can reach a listener as a target; reading .dataset off them
  // would throw inside an error handler, which is the worst place to throw.
  assert.strictEqual(failureUi({}), 'banner');
});

// --- onSwapFailure -----------------------------------------------------------
// htmx events bubble to the document, so a handler registered here sees EVERY
// failed swap on the page, not only the one its page cares about. Each
// hand-rolled equivalent scopes on that (dashboard-page.js and detail-page.js
// compare evt.detail.target; detail-history.js asks whether the target is inside
// its list); the shared helper did not, so a second htmx region on any of its
// three pages would have blanked the main list and offered a Retry for a request
// that never failed.

// The stub is the one the sibling tests use (see tests/test_ajax_status.js):
// plain objects on `global`, no jsdom. htmx-filters.js reads `document` and
// `window` when the handler RUNS, not when the module loads, so installing them
// after the require at the top of this file is enough.
function installFailureDom() {
  const calls = { banner: 0, inline: [] };
  const handlers = {};
  global.document = { addEventListener(type, fn) { handlers[type] = fn; } };
  global.window = {
    AjaxStatus: {
      showBanner() { calls.banner += 1; },
      renderInto(target) { calls.inline.push(target); },
    },
  };
  return { calls, fire: (target) => handlers['htmx:responseError']({ detail: { target } }) };
}

//< an element carries a dataset; that is what failureUi reads to tell an element
//  from a document or a window
const region = (id) => ({ id, dataset: {} });

run('a failure in another region on the page is left to that region', () => {
  // The regression this exists for. /charts, /history and the Top lists each
  // have exactly one htmx region today, so the missing check cost nothing - but
  // the dashboard already shows what a second one looks like (two deferred
  // cards that fail independently of the summary), and the first of those added
  // to one of these pages would have hit this.
  const { calls, fire } = installFailureDom();
  onSwapFailure('historyResults', () => {});
  fire(region('discoverCard'));
  assert.strictEqual(calls.inline.length, 0);
  assert.strictEqual(calls.banner, 0);
});

run('a failure in this page\'s own region is reported inline', () => {
  const { calls, fire } = installFailureDom();
  onSwapFailure('historyResults', () => {});
  const own = region('historyResults');
  fire(own);
  assert.deepStrictEqual(calls.inline, [own]);
  assert.strictEqual(calls.banner, 0);
});

run('an unattributable failure still gets the banner', () => {
  // No target means htmx could not resolve one, so there is no region to blame
  // and no placeholder to write into. Staying silent is the failure mode this
  // whole area exists to prevent, so it is reported rather than scoped away.
  const { calls, fire } = installFailureDom();
  onSwapFailure('historyResults', () => {});
  fire(null);
  assert.strictEqual(calls.banner, 1);
  assert.strictEqual(calls.inline.length, 0);
});

run('the retry handed to the reporter is the page\'s own', () => {
  const { fire } = installFailureDom();
  let retried = 0;
  onSwapFailure('historyResults', () => { retried += 1; });
  const own = region('historyResults');
  global.window.AjaxStatus.renderInto = (_target, retry) => retry();
  fire(own);
  assert.strictEqual(retried, 1);
});

run('both htmx failure events are covered, not just the response one', () => {
  // A network error fires htmx:sendError and never reaches responseError -
  // registering only the latter leaves an offline page on a stuck placeholder.
  const handlers = {};
  global.document = { addEventListener(type, fn) { handlers[type] = fn; } };
  global.window = { AjaxStatus: { showBanner() {}, renderInto() {} } };
  onSwapFailure('historyResults', () => {});
  assert.deepStrictEqual(Object.keys(handlers).sort(),
                         ['htmx:responseError', 'htmx:sendError']);
});

// --- rememberFocusBeforeSwap / restoreFocusAfterSwap -------------------------
// UT-4(b): htmx swaps out the activated control's container, and the
// browser's default is to drop focus to <body> once that element is gone -
// silently resetting keyboard navigation to the top of the page. This pair is
// wired once at module load (see the bottom of htmx-filters.js) for every
// htmx region on every page that loads this module, rather than per page.

function makeFocusable(attrs) {
  return {
    _attrs: attrs || {},
    hasAttribute(name) { return name in this._attrs; },
    setAttribute(name, value) { this._attrs[name] = value; },
    focusCalls: 0,
    focus() { this.focusCalls += 1; },
  };
}

// A real post-swap moment: the old content (and whatever was focused inside
// it) is gone, and the browser's default has already dropped focus to
// <body> - modelled here as a fresh, unrelated makeFocusable() standing in
// for <body>, moved into place between beforeSwap and afterSettle. The old
// version of these tests left document.activeElement pointed at the (by now
// detached) button through the restore call, which the real DOM never does.

run('focus inside the swap target is restored once the swap settles', () => {
  const button = makeFocusable();
  const target = makeFocusable();
  target.contains = (node) => node === button;   //< button lived inside target
  global.document = { activeElement: button };

  rememberFocusBeforeSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };   //< <body>, post-swap
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 1);
  assert.strictEqual(target._attrs.tabindex, '-1',
                     'a container needs a tabindex to be focusable at all');
});

run('a target that already carries a tabindex keeps its own value', () => {
  const button = makeFocusable();
  const target = makeFocusable({ tabindex: '0' });
  target.contains = (node) => node === button;
  global.document = { activeElement: button };

  rememberFocusBeforeSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 1);
  assert.strictEqual(target._attrs.tabindex, '0');
});

run('focus outside the swap target is never moved onto it', () => {
  const elsewhere = makeFocusable();
  const target = makeFocusable();
  target.contains = () => false;
  global.document = { activeElement: elsewhere };

  rememberFocusBeforeSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
  assert.strictEqual('tabindex' in target._attrs, false);
});

run('a swap nobody had focus in front of leaves nothing to restore', () => {
  const target = makeFocusable();
  target.contains = () => false;
  global.document = { activeElement: makeFocusable() };
  rememberFocusBeforeSwap({ detail: { target } });   //< explicitly resets state for this case

  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
});

run('two overlapping swaps each restore their own focus', () => {
  /* The flag used to be ONE module-global boolean, so the second swap's
   * beforeSwap cleared the first's: with two regions in flight (Compare
   * alone swaps a dozen), the first region's focus was never restored
   * (2026-09-03 review, C-12). Recorded on the target instead, so a second
   * swap can no longer answer for the first. */
  const buttonA = makeFocusable();
  const targetA = makeFocusable();
  targetA.contains = (node) => node === buttonA;
  const targetB = makeFocusable();
  targetB.contains = () => false;

  global.document = { activeElement: buttonA };
  rememberFocusBeforeSwap({ detail: { target: targetA } });
  global.document = { activeElement: makeFocusable() };
  rememberFocusBeforeSwap({ detail: { target: targetB } });   //< the interleaved one

  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target: targetA } });
  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target: targetB } });

  assert.strictEqual(targetA.focusCalls, 1, 'the region that held focus gets it back');
  assert.strictEqual(targetB.focusCalls, 0, 'the one that never had it must not steal it');
});

run('a second settle of the same target does not re-focus it', () => {
  //< the record is consumed, not left standing: a later settle of a region
  //  the user has since left must not pull focus back
  const button = makeFocusable();
  const target = makeFocusable();
  target.contains = (node) => node === button;
  global.document = { activeElement: button };

  rememberFocusBeforeSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 1);
});

// --- R1: the target is not always a swapped-out container --------------------
// /compare has no hx-target anywhere, so htmx resolves the target to the
// firing control itself, to the enclosing form (hx-swap="none"), or to
// <body> for a boosted badge. Node.contains is inclusive, so a target that
// IS (or still holds) the focused element must be left alone rather than
// re-focused and stamped with tabindex="-1".

run('(a) the target is the very control that fired the request', () => {
  const target = makeFocusable();
  target.contains = (node) => node === target;   //< Node.contains is inclusive
  global.document = { activeElement: target };

  rememberFocusBeforeSwap({ detail: { target } });
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
  assert.strictEqual('tabindex' in target._attrs, false);
});

run('(b) the target is a form whose focused child was not swapped away', () => {
  //< hx-swap="none": the form itself is "the target" and the live control
  //  (e.g. #sortBy) is still its child throughout
  const child = makeFocusable();
  const target = makeFocusable();
  target.contains = (node) => node === child;
  global.document = { activeElement: child };

  rememberFocusBeforeSwap({ detail: { target } });
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
  assert.strictEqual(child.focusCalls, 0);
  assert.strictEqual('tabindex' in target._attrs, false);
});

run('(c) the target is document.body (a boosted link with no ancestor hx-target)', () => {
  const target = makeFocusable();
  target.contains = () => true;   //< body contains everything
  global.document = { activeElement: target };

  rememberFocusBeforeSwap({ detail: { target } });
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
  assert.strictEqual('tabindex' in target._attrs, false, 'never stamp tabindex on <body>');
});

run('(d) a container whose focused child is really gone still restores', () => {
  //< the case the guard must not break: a genuine swapped-out container
  const button = makeFocusable();
  const target = makeFocusable();
  target.contains = (node) => node === button;
  global.document = { activeElement: button };

  rememberFocusBeforeSwap({ detail: { target } });
  global.document = { activeElement: makeFocusable() };   //< <body>, post-swap
  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 1);
  assert.strictEqual(target._attrs.tabindex, '-1');
});

run('(e) MS-1: htmx already restored focus to a by-id-matched element inside the container', () => {
  // htmx's own swap() does an id-matched focus restore for a swapped-in
  // element sharing an id with the one that had focus (#jumpToPage on
  // /history, the Top lists and detail history) - it runs well before this
  // handler's afterSettle. The new element reports as contained by the
  // target too, so this must not treat it as "gone" either.
  const oldControl = makeFocusable();
  const target = makeFocusable();
  target.contains = (node) => node === oldControl;
  global.document = { activeElement: oldControl };
  rememberFocusBeforeSwap({ detail: { target } });

  const newJumpToPage = makeFocusable();
  target.contains = (node) => node === newJumpToPage;   //< htmx's restore already ran
  global.document = { activeElement: newJumpToPage };

  restoreFocusAfterSwap({ detail: { target } });

  assert.strictEqual(target.focusCalls, 0);
  assert.strictEqual('tabindex' in target._attrs, false);
});

console.log('All htmx filter tests passed.');
