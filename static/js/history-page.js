// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// What is left of the /history page's browser logic once htmx owns the
// request/swap layer (see templates/history.html for the attributes).
//
// This file used to be 225 lines. Everything that went is something htmx does
// declaratively, and the mapping is worth keeping written down, because the
// next page to migrate deletes the same five things:
//
//   loadHistoryResults + AbortController  -> hx-get + hx-sync="...:replace"
//   replaceHistoryUrl                     -> hx-replace-url="true"
//   the debounce helpers on the search box-> hx-trigger "input changed delay:400ms"
//   the delegated pagination click handler-> hx-boost on the pagination wrapper
//   the 401 -> /login branch              -> HX-Redirect, sent by the server
//                                            (see app.py unauthenticatedResponse)
//   the popstate handler                  -> nothing, and deliberately: every
//     URL update here REPLACES, so this page never puts an entry on the history
//     stack for itself and there is no in-page state to pop back to. The
//     handler only ever ran on a cross-document Back, which reloads the page
//     server-side anyway.
//
// What genuinely could not move is below: a date range needs validating before
// it is worth a request, the sort toggle has no natural form control, and the
// jump-to-page input is an <input> rather than a link so hx-boost cannot see
// it. Note there is no `hx-on:` or event-filter equivalent available as a
// shortcut here - the CSP withholds 'unsafe-eval' from this page (see the
// header comment in templates/history.html).

//< the form htmx watches; also the element the sort toggle re-triggers
var HISTORY_FORM_ID = 'historyFilters';
//< the swap target it fills, in history.html
var HISTORY_RESULTS_ID = 'historyResults';

// The date-range check, its error display, the custom-range show/hide and the
// empty-param pruning all live in static/js/htmx-filters.js, shared with the
// four other filter pages - they carry the same control set, and five copies of
// "is this range worth a request" would eventually disagree. Loaded before this
// file (see templates/history.html).
if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  // Called from the Time Period select's onchange. Runs before htmx's own
  // listener does (an inline on*= handler fires at the target, htmx's is on the
  // form and fires as the event bubbles), so the disabled flags below are
  // already correct by the time the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and therefore out
  // of the URL - after switching back to a named interval.
  window.updateHistoryInterval = function () {
    HtmxFilters.syncCustomRange('historyCustomDates');
  };

  // The Date sort toggle: flips newest-first (default) <-> oldest-first. The
  // value lives in a hidden form field so htmx builds the query string from one
  // place; this only has to flip it, relabel the button and tell the form to
  // re-fire. Resetting to page 1 is implicit - `page` is not a form field, so
  // serializing the form drops it.
  window.updateHistorySort = function () {
    var field = byId('historySortValue');
    var next = field.value === 'oldest' ? '' : 'oldest';
    field.value = next;
    var oldest = next === 'oldest';
    var button = byId('historySort');
    button.textContent = oldest ? 'Date ↑' : 'Date ↓';
    //< kept in step with the same wording templates/history.html renders on
    //  first load (2026-09-02 review, UT-15) - a toggle button's state is
    //  what a screen reader reads (the detail-history.js filter-tab pattern),
    //  and the label names both the active order and what a click switches to
    //  rather than the fixed "Toggle date sort order" it used to carry.
    button.setAttribute('aria-pressed', String(oldest));
    button.setAttribute('aria-label', 'Sorted ' + (oldest ? 'oldest' : 'newest') +
      ' first - click to sort ' + (oldest ? 'newest' : 'oldest') + ' first');
    byId(HISTORY_FORM_ID).dispatchEvent(new Event('historyRefresh'));
  };

  // _pagination.html's jump-to-page input calls the shared
  // handleJumpToPageKeydown (static/js/layout-chrome.js), which defers to this
  // hook when present instead of navigating. It is an <input>, not a link, so
  // hx-boost does not cover it the way it covers Prev/Next.
  //
  // replaceState, never push - the same rule the hx-replace-url attributes
  // encode, and tests/test_pagination_ajax_handler.py asserts for this file.
  // htmx does the replacing: the `replace` option is forwarded into the same
  // history update hx-replace-url feeds (a path is used as given; only "true"
  // means "the request path"), and that update runs only inside the
  // successful-swap branch. The address bar used to be rewritten here, BEFORE
  // the request, so a failed jump left it claiming a page the list never
  // showed. Issued off the container so the request inherits its hx-target /
  // hx-swap / hx-sync, and a jump during an in-flight filter change is
  // serialised like every other swap into it.
  var goToHistoryPage = function (page) {
    HtmxFilters.requestPage(page, HISTORY_RESULTS_ID);
  };
  window.__paginationAjaxHandler = goToHistoryPage;

  // The one place a request gets vetoed. Scoped to requests the FORM makes:
  // a boosted pagination link carries its whole query in its href and must keep
  // working even while the Time Period select sits on a half-entered custom
  // range, which is exactly the state that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    HtmxFilters.validateFormRequest(evt, HISTORY_FORM_ID);
  });

  //< cover-art fade-ins are handled once for the whole app in
  //  static/js/chrome-common.js, which already owned this behaviour and now
  //  re-runs its sweep on htmx:afterSwap

  // A genuine failure gets the shared inline error + Retry rather than a stuck
  // "Loading…" or a silently stale list. An expired session no longer arrives
  // here at all: the server answers an htmx request with HX-Redirect, so the
  // browser navigates to /login instead of this reporting a load failure.
  //
  // The retry is issued off the FORM, not the address bar. A 4xx/5xx never
  // touches the URL (htmx updates history only inside its successful-swap
  // branch), so after a failed filter change the controls already show the new
  // choice while the URL still says the old one - and a retry of the URL
  // rendered the old list under the new selection, with nothing to say so.
  // Re-serialising the form requests what is on screen, runs the configRequest
  // veto/prune above (its elt is the form) and inherits hx-replace-url and
  // hx-sync. The bare hx-get path, because htmx appends the form's values to
  // whatever path it is handed. A failed boosted page link is retried as page
  // one of the current filters - the form is what the user can see.
  HtmxFilters.onSwapFailure(HISTORY_RESULTS_ID, function () {
    HtmxFilters.retryForm(HISTORY_FORM_ID, HISTORY_RESULTS_ID);
  });
}
//< no module.exports: everything pure moved to static/js/htmx-filters.js, which
//  is where the plain-node unit test now points (tests/test_htmx_filters.js)
