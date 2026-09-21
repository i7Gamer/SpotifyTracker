// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The two pieces of filter logic every htmx-driven list page needs and htmx has
// no opinion about. Extracted when the Top lists became the second page to want
// them (after /history) rather than copied - the filter cards on /history,
// /top-songs, /top-artists and /top-albums are the same control set, and a
// divergence between two copies of "is this date range worth a request" would
// show up as one page querying on a half-typed date and another not.
//
// DOM-free and exported, so both are unit-tested in plain node
// (tests/test_htmx_filters.js). The pages wire them to htmx events; nothing here
// knows what an element is.

// The three states a Time Period selection can be in.
var RANGE_OK = null;
var RANGE_INCOMPLETE = 'incomplete';
var RANGE_INVERTED = 'inverted';
var RANGE_INVERTED_MESSAGE = 'Start date cannot be after end date.';

function rangeProblem(interval, startDate, endDate) {
  //< only a custom range can be malformed; a named interval needs no dates
  if (interval !== 'custom') return RANGE_OK;
  //< half-entered, which is what every keystroke of typing a date looks like:
  //  not an error to shout about, but not something to query on either
  if (!startDate || !endDate) return RANGE_INCOMPLETE;
  if (new Date(startDate) > new Date(endDate)) return RANGE_INVERTED;
  return RANGE_OK;
}

// Drop the params the user has not set. htmx serializes every enabled control in
// a form, so an untouched search box or tag dropdown would otherwise put
// `q=&tag=` in the request - and hx-replace-url writes the requested URL back to
// the address bar, so those empties become part of the link people copy.
//
// Takes the parameters object htmx hands to htmx:configRequest, which is a Proxy
// over a FormData with deleteProperty and ownKeys traps - so ordinary object
// operations work on it, and a plain object works in the unit test.
//
// "Unset" means blank after trimming, which is what BOTH pages' hand-written
// param code did before htmx (`if (searchQuery.trim())`) - a search box holding
// only spaces is an untouched search box, and letting it through would put
// `?q=%20%20` in the address bar and run a LIKE '% %' over the whole history.
// The value kept is the ORIGINAL, untrimmed one, also matching the old
// behaviour: leading space inside a real query is the user's to keep.
//
// "0" survives, because it is falsy but is a real value - the Top lists'
// fullOnly opt-out is exactly that, and dropping it would flip the filter back
// on (an absent fullOnly means the default, which is ON).
function pruneEmptyParams(parameters) {
  Object.keys(parameters).forEach(function (key) {
    var value = parameters[key];
    if (typeof value === 'string' && value.trim() === '') delete parameters[key];
  });
  return parameters;
}

// The History and Top-list pages use the same htmx request shape for their
// pagination input. Keep URL construction and successful-swap replacement in
// one place so the two pages cannot drift apart again.
function requestPage(page, resultsId) {
  var params = new URLSearchParams(window.location.search);
  params.set('page', page);
  var url = window.location.pathname + '?' + params.toString();
  htmx.ajax('GET', url, {
    source: document.getElementById(resultsId),
    replace: url,
  });
}

// A click the BROWSER should handle rather than htmx, because the modifier the
// user held means "open this somewhere else".
//
// htmx's boost exempts ctrl and meta only - the check in the vendored build is
// `boosted && anchor && click && (ctrlKey || metaKey)`. Every delegated
// pagination handler hx-boost replaced exempted all FOUR modifiers ("let
// new-tab clicks pass"), so adopting boost silently swallowed shift-click (new
// window) and alt-click (download) on every migrated page. This is the missing
// half, applied once below rather than per page.
//
// Only the two htmx misses are listed: ctrl/meta already reach the browser, and
// naming them here would be dead weight that reads like it is doing something.
function isNativeModifierClick(evt) {
  return !!evt && (!!evt.shiftKey || !!evt.altKey);
}

// Whether a Time Period choice makes the Trend-buckets control meaningless.
//
// A single-day view is bucketed by hour server-side, so the control is a no-op
// and hides. /charts and /genres both had this rule and spelled it differently -
// one as `SINGLE_DAY_INTERVALS.indexOf(interval) === -1`, the other as
// `interval === 'today' || interval === 'day'` - with a comment on the second
// saying it mirrored the first. Two spellings of one rule is how they stop
// mirroring.
var SINGLE_DAY_INTERVALS = ['today', 'day'];

function hidesTrendBuckets(interval) {
  return SINGLE_DAY_INTERVALS.indexOf(interval) !== -1;
}

// --- the shared custom-date-range controls -----------------------------------
// Every filter page names these the same (#interval, #startDate, #endDate,
// #dateError); only the container id differs, so it is the one argument. The
// three functions below were copied into five page modules, identical but for
// that id - and they carry the validation, which is where a divergence would
// actually hurt.

function rangeProblemFromDom() {
  return rangeProblem(document.getElementById('interval').value,
                      document.getElementById('startDate').value,
                      document.getElementById('endDate').value);
}

// aria-invalid on the two date inputs, driven by the same `problem` value
// showRangeError paints #dateError from, so the two can never disagree about
// what "invalid" means. Removed rather than set to "false" when clear: an
// absent attribute is what a screen reader treats as "no opinion", matching
// every other field on the page that was never marked invalid at all.
//
// FOLLOW-UP (2026-09-02 review): /history got this treatment first
// (alongside #dateError's role="alert" + aria-describedby in
// templates/history.html). Folded in here instead of copied a second time, so
// /top-songs, /top-artists and /top-albums - which share this file and
// templates/_page_card.html - report an inverted range too.
function syncDateAriaInvalid(problem) {
  var invalid = problem === RANGE_INVERTED;
  var startField = document.getElementById('startDate');
  var endField = document.getElementById('endDate');
  if (invalid) {
    startField.setAttribute('aria-invalid', 'true');
    endField.setAttribute('aria-invalid', 'true');
  } else {
    startField.removeAttribute('aria-invalid');
    endField.removeAttribute('aria-invalid');
  }
}

// All four ids are addressed unguarded, because they are ONE control set: a
// template rendering any of them renders all of them, which
// tests/test_custom_date_controls.py asserts. #dateError used to be the one
// null-guarded element here, which read as "the error span is optional" - it is
// not, and the guard only meant a page that had lost it would silently stop
// reporting inverted ranges instead of failing where someone would notice.
function showRangeError(problem) {
  var invalid = problem === RANGE_INVERTED;
  var errorEl = document.getElementById('dateError');
  errorEl.textContent = invalid ? RANGE_INVERTED_MESSAGE : '';
  errorEl.style.display = invalid ? 'block' : 'none';   //< block, like every sibling page
  document.getElementById('startDate').style.borderColor = invalid ? 'var(--accent)' : '';
  document.getElementById('endDate').style.borderColor = invalid ? 'var(--accent)' : '';
  syncDateAriaInvalid(problem);
}

// Called from the Time Period select's onchange. Runs before htmx's own
// listener (an inline on*= handler fires at the target; htmx's is on the form
// and fires as the event bubbles), so the disabled flags are already right by
// the time the request is serialized.
//
// `disabled`, not merely hidden: a disabled control is not serialized, which is
// what keeps a stale custom range out of the request - and so out of the URL,
// since hx-replace-url writes back what was requested.
function syncCustomRange(containerId) {
  var custom = document.getElementById('interval').value === 'custom';
  document.getElementById(containerId).style.display = custom ? 'flex' : 'none';
  document.getElementById('startDate').disabled = !custom;
  document.getElementById('endDate').disabled = !custom;
  showRangeError(rangeProblemFromDom());
}

// Both list pages veto only their own form requests. Boosted pagination links
// carry their complete query in the href and must remain usable while a custom
// range is incomplete, so callers pass the form id explicitly.
function validateFormRequest(evt, formId) {
  if (!evt.detail.elt || evt.detail.elt.id !== formId) return;
  var problem = rangeProblemFromDom();
  showRangeError(problem);
  if (problem !== RANGE_OK) {
    evt.preventDefault();
    return;
  }
  pruneEmptyParams(evt.detail.parameters);
}

// Retry the values currently visible in the form. A failed request never
// updates the address bar, so retrying its old URL could render stale filters.
function retryForm(formId, targetId) {
  var form = document.getElementById(formId);
  htmx.ajax('GET', form.getAttribute('hx-get'), {
    source: form,
    target: '#' + targetId,
    swap: 'innerHTML',
  });
}

// --- the shared "Full plays only" checkbox -----------------------------------
// It cannot be a plain form field: an unchecked checkbox is not serialized at
// all, and an ABSENT fullOnly means the DEFAULT, which is ON - so unticking the
// box would have read as ticking it. The checkbox therefore drives a hidden
// field carrying the explicit "1"/"0" the routes read.
//
// Unguarded ids like the control set above, and for the same reason: they are
// one unit, rendered together by templates/_full_plays_toggle.html or not at
// all.
function syncFullPlaysFilter() {
  document.getElementById('fullOnlyValue').value =
    document.getElementById('fullPlaysOnly').checked ? '1' : '0';
}

// Which failure UI a swap target wants, and where it goes.
//
// Every migrated page ended up with the same eight-line block: two listeners,
// a null-guard, and a choice between the inline "couldn't load / Retry" and the
// page-level banner.
//
// The choice is NOT configurable, and that is the honest shape. This once
// branched on a data-htmx-failure="banner" attribute, described as declarative
// markup - but no template ever emitted it, and the two pages that genuinely
// want a banner (/genres, /compare) could never have used it: they retry the
// request that actually failed, off its own target and path, rather than a
// fixed region, so they register their own handlers instead. A knob with no
// possible user reads as a supported mechanism and silently has one setting
// (tests/test_data_attribute_knobs.py now fails on the shape).
//
// What is left is the real rule: a target that can host a message gets the
// inline "couldn't load / Retry"; anything else gets the banner. That covers no
// target at all - a request htmx could not resolve one for - and a non-element
// like document or window, which reach a listener as targets and have no
// dataset. Both mean there is nowhere to put an inline message.
//
// Split out as a pure function because it is the half worth testing, and until
// the copies were merged there was nowhere to test it from: each lived inside a
// page IIFE, which is why tests/test_ajax_loader_error_handling.py pins these
// loaders by source shape and says so.
function failureUi(target) {
  if (!target || !target.dataset) return 'banner';
  return 'inline';
}

// Register a page's swap-failure reporting for the region with id `targetId`.
// `retry` is the page's own - it is deliberately NOT derived from the event
// here: the request to re-issue differs per page (which target, which swap,
// whether the form has to be re-serialized), and getting it subtly wrong would
// break recovery silently, in code no behavioural test covers.
//
// `targetId` is what makes that safe. htmx events bubble to the document, so
// this handler sees EVERY failed swap on the page - and `retry` re-issues one
// fixed request, so reporting another region's failure would blank this one and
// offer a Retry for something that never failed. Every hand-rolled equivalent
// already scopes this way (dashboard-page.js and detail-page.js compare
// evt.detail.target, detail-history.js asks whether the target is inside its
// list); this one did not, and only got away with it because its three callers
// each have exactly one htmx region.
function onSwapFailure(targetId, retry) {
  var handler = function (evt) {
    if (!window.AjaxStatus) return;
    var target = evt.detail && evt.detail.target;
    //< nothing to attribute it to and nowhere to write it: report it rather
    //  than scope it away, since silence is the failure this area exists for
    if (failureUi(target) === 'banner') {
      window.AjaxStatus.showBanner(retry);
      return;
    }
    //< an element, so it names a region - and only ours is ours to report
    if (target.id !== targetId) return;
    window.AjaxStatus.renderInto(target, retry);
  };
  //< on `document`: htmx events bubble, and this must not depend on where the
  //  calling script sits relative to <body>
  document.addEventListener('htmx:responseError', handler);
  document.addEventListener('htmx:sendError', handler);
}

// Focus tracking across an htmx swap. htmx replaces the target's content and,
// when the element that held focus (a pagination Previous/Next link, Show
// More Plays, a Wrapped year badge, an ajax-status Retry button) was inside
// it, that element is gone once the swap lands - the browser's default is to
// drop focus to <body>. That silently resets keyboard navigation to the top
// of the page and loses a screen reader's place, on every migrated htmx
// region, not just one page's pagination.
//
// One pair, wired once below for every page that loads this module (see the
// bottom of this file) - no page registers these itself, unlike
// onSwapFailure, which needs a page-specific target id and retry callback
// this pair does not.
// Recorded ON THE TARGET, not in a module-global boolean. One response can
// settle several regions and a page can have several requests in flight
// (Compare swaps a dozen), so with one flag the second beforeSwap cleared the
// first's: the region the user actually had focus in was left unrestored,
// silently, whenever anything overlapped it.
//
// The flag says only "focus was inside the target at beforeSwap" - it is not
// an instruction to blindly refocus the target at afterSettle. /compare has
// no hx-target anywhere, so htmx resolves the target to the firing control
// itself, to the enclosing form (hx-swap="none"), or to <body> for a boosted
// badge; restoreFocusAfterSwap checks where focus actually IS before touching
// anything, precisely because "the target" is not always a swapped-out
// container the browser dropped focus from.
var FOCUS_WAS_INSIDE = '_htmxFocusWasInside';

function rememberFocusBeforeSwap(evt) {
  var target = evt && evt.detail && evt.detail.target;
  if (!target) return;
  var active = document.activeElement;
  target[FOCUS_WAS_INSIDE] = !!(active && target.contains && target.contains(active));
}

function restoreFocusAfterSwap(evt) {
  var target = evt && evt.detail && evt.detail.target;
  if (!target || !target[FOCUS_WAS_INSIDE]) return;
  //< consumed, not merely read: a later settle of a region the user has since
  //  left must not pull focus back out of wherever they are now
  target[FOCUS_WAS_INSIDE] = false;
  //< /compare has no hx-target anywhere, so htmx resolves the target to the
  //  firing element itself, to the enclosing form (hx-swap="none"), or to
  //  <body> for a boosted link - and Node.contains is inclusive, so "the
  //  target still holds the focus" is the NORMAL case there, not an
  //  exception to it. It is also what htmx's own by-id restore inside
  //  swap() produces on /history, the Top lists and detail history: a
  //  swapped-in element sharing an id with the one that had focus (the
  //  jump-to-page input) is refocused before this handler ever runs. Either
  //  way focus is already where the user left it, and stamping
  //  tabindex="-1" on that control / form / <body> is pure damage - a
  //  <select> refocused this way never gets its tabindex removed again and
  //  drops out of the tab order for the rest of the page's life.
  var active = document.activeElement;
  if (active && target.contains && target.contains(active)) return;
  if (!target.focus) return;
  //< a container (a <div>, typically) is not focusable at all without one -
  //  document.body must never be where focus lands instead
  if (!target.hasAttribute || !target.hasAttribute('tabindex')) {
    if (target.setAttribute) target.setAttribute('tabindex', '-1');
  }
  target.focus();
}

var HtmxFilters = {
  rangeProblem: rangeProblem,
  pruneEmptyParams: pruneEmptyParams,
  isNativeModifierClick: isNativeModifierClick,
  hidesTrendBuckets: hidesTrendBuckets,
  rangeProblemFromDom: rangeProblemFromDom,
  requestPage: requestPage,
  validateFormRequest: validateFormRequest,
  retryForm: retryForm,
  showRangeError: showRangeError,
  syncDateAriaInvalid: syncDateAriaInvalid,
  syncCustomRange: syncCustomRange,
  syncFullPlaysFilter: syncFullPlaysFilter,
  failureUi: failureUi,
  onSwapFailure: onSwapFailure,
  rememberFocusBeforeSwap: rememberFocusBeforeSwap,
  restoreFocusAfterSwap: restoreFocusAfterSwap,
  RANGE_OK: RANGE_OK,
  RANGE_INCOMPLETE: RANGE_INCOMPLETE,
  RANGE_INVERTED: RANGE_INVERTED,
  RANGE_INVERTED_MESSAGE: RANGE_INVERTED_MESSAGE,
};

if (typeof window !== 'undefined') {
  window.HtmxFilters = HtmxFilters;
  // The one inline-handler target exported straight from here rather than from
  // a page module, because the markup that calls it is a shared PARTIAL
  // (templates/_full_plays_toggle.html) and this file is the only script every
  // page that renders it loads. A per-page `window.updateFullPlaysFilter =`
  // wrapper would be a step each new caller has to remember, and forgetting it
  // fails silently: tests/test_inline_handler_targets.py resolves the name
  // against every file in static/js, so it cannot see which page loads which.
  window.updateFullPlaysFilter = syncFullPlaysFilter;
}

// Hand shift/alt-clicks on a boosted link back to the browser. Installed here,
// on the document, in the CAPTURE phase: htmx binds its trigger listener to the
// boosted anchor itself, so stopping propagation on the way down means that
// listener never runs and the default navigation proceeds untouched. One
// listener covers every hx-boost in the app - the pagination strips, the genre
// chips, the compare badges, the wrapped year badges - so no page has to
// remember to opt in, which is exactly how the affordance got lost.
//
// Deliberately NOT preventDefault/stopImmediatePropagation: the whole point is
// to let the browser do what it was going to do.
if (typeof document !== 'undefined') {
  document.addEventListener('click', function (evt) {
    if (!isNativeModifierClick(evt)) return;
    var link = evt.target && evt.target.closest && evt.target.closest('a[href]');
    if (!link || !link.closest('[hx-boost]')) return;
    evt.stopPropagation();
  }, true);

  //< beforeSwap: the old content, and whatever held focus, is still in the
  //  DOM. afterSettle: the new content has landed and settle transitions have
  //  finished, so the target is safe to focus.
  document.addEventListener('htmx:beforeSwap', rememberFocusBeforeSwap);
  document.addEventListener('htmx:afterSettle', restoreFocusAfterSwap);
}
if (typeof module !== 'undefined' && module.exports) {
  module.exports = HtmxFilters;
}
