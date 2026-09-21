// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

/* What is left of the Top Songs/Artists/Albums pages' browser logic once htmx
 * owns the request/swap layer (see templates/_page_card.html for the wiring,
 * shared by all three pages).
 *
 * This file was 208 lines of the same loader /history had: an AbortController
 * to stop a superseded response landing, replaceState bookkeeping, a delegated
 * pagination click listener, and four filter handlers that each hand-edited the
 * query string. htmx does all of that declaratively, and the pure "which params
 * does this filter set vs delete" helpers went with it - the filter card now IS
 * the parameter list, serialized by htmx.
 *
 * What could not move is below, and it is now the same short list as /history:
 * the jump-to-page hook and the request veto. The "Full plays only" hidden
 * field used to make it one longer, and moved to static/js/htmx-filters.js when
 * /history started rendering the same partial.
 *
 * Note there is no `hx-on:` or event-filter shortcut available here: the CSP
 * withholds 'unsafe-eval' from these pages (see templates/_page_card.html). */

//< the form htmx watches, in _page_card.html
var TOP_LIST_FORM_ID = 'topListFilters';
//< the swap target it fills, in _top_list_container.html
var TOP_LIST_RESULTS_ID = 'topListResults';

if (typeof document !== 'undefined') {
  // Called from the Time Period select's onchange. Runs before htmx's listener
  // (an inline on*= handler fires at the target; htmx's is on the form and fires
  // as the event bubbles), so the disabled flags are already right by the time
  // the request is serialized.
  //
  // `disabled`, not merely hidden: a disabled control is not serialized, which
  // is what keeps a stale custom range out of the request - and so out of the
  // URL - after switching back to a named interval.
  window.updateIntervalFilter = function () { HtmxFilters.syncCustomRange('customDates'); };

  //< "Full plays only" moved to static/js/htmx-filters.js, which exports
  //  window.updateFullPlaysFilter itself: /history renders the same partial now
  //  (templates/_full_plays_toggle.html) and does not load this file

  // _pagination.html's jump-to-page input calls the shared
  // handleJumpToPageKeydown (static/js/layout-chrome.js), which defers to this
  // hook when present. It is an <input>, not a link, so hx-boost does not cover
  // it the way it covers Prev/Next.
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
  var goToTopListPage = function (page) {
    HtmxFilters.requestPage(page, TOP_LIST_RESULTS_ID);
  };
  window.__paginationAjaxHandler = goToTopListPage;

  // The one place a request gets vetoed. Scoped to requests the FORM makes: a
  // boosted pagination link carries its whole query in its href and must keep
  // working even while the Time Period select sits on a half-entered custom
  // range, which is exactly the state that blocks a form request.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    HtmxFilters.validateFormRequest(evt, TOP_LIST_FORM_ID);
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
  HtmxFilters.onSwapFailure(TOP_LIST_RESULTS_ID, function () {
    HtmxFilters.retryForm(TOP_LIST_FORM_ID, TOP_LIST_RESULTS_ID);
  });
}
//< no module.exports: everything pure moved to static/js/htmx-filters.js, which
//  is where the plain-node unit test now points (tests/test_htmx_filters.js)
