// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// What is left of the Wrapped page's browser logic once htmx owns the
// request/swap layer (see templates/wrapped.html for the attributes, and
// templates/_wrapped_results.html for what comes back).
//
// This file used to be 579 lines. What went, and what took it over:
//
//   loadWrappedData + AbortController      -> hx-get + hx-sync="...:replace"
//   setWrappedFade's add/remove/cleanup    -> hx-indicator + .htmx-fade-target
//   updateWrappedFilters + the 3 onchange= -> hx-trigger="change" on the form
//   the delegated year-badge click handler -> hx-boost on the badge nav
//   replaceState, written out three times  -> hx-replace-url="true"
//   the popstate handler                   -> nothing, and deliberately: every
//     URL update here REPLACES, so this page never puts an entry on the history
//     stack for itself and there is no in-page state to pop back to. The
//     handler only ever ran on a cross-document Back, which reloads the page
//     server-side anyway.
//   ~200 lines of DOM surgery (8 stat tiles, 6 track lists and their
//     show/hide, the genre card, the share panel, 13 export-button data-*,
//     the hero title, the chart heading)
//                                          -> the server renders all of it;
//     the four regions that sit OUTSIDE the swap container come back as
//     out-of-band swaps (see _wrapped_results.html's header)
//   the 401 -> /login branch               -> HX-Redirect, sent by the server
//                                             (app.py unauthenticatedResponse)
//
// What genuinely could not move is below, and it is more than /history kept:
//
//   - the chart. htmx swaps the HTML; the <canvas> is a NEW element after every
//     swap and has to be redrawn from the freshly-rendered data island. That is
//     an htmx:afterSettle listener, and there is no version of it htmx owns.
//   - the Export Summary Card PNG, which is ~100 lines of canvas drawing.
//   - which stats-filter category is showing. That is client state the server
//     has no idea about (it is not in the URL), so it is remembered here and
//     re-applied after each swap.
//   - the share modal: opening/closing it, and its create/revoke forms, which
//     POST to an action endpoint and swap back a panel rather than the recap.
//     Those keep their own fetch() (see routes/auth.py's profileShareLinkAction
//     and routes/wrapped.py's createWrappedShareLink, both still ?ajax=true).
//
// Note there is no `hx-on:` or event-filter shortcut available for any of it -
// the CSP withholds 'unsafe-eval' from this page (see wrapped.html's header).

//< the form htmx watches, and the container it swaps into
var WRAPPED_FORM_ID = 'wrappedFilters';
var WRAPPED_RESULTS_ID = 'wrappedResults';
//< the stats-filter category that shows every section; also the fallback when
//  the chosen one has no items in the year just loaded
var STATS_FILTER_ALL = 'all';
//< what Revoke asks before a share link dies for good
var SHARE_LINK_REVOKE_CONFIRM = 'Revoke this link? Anyone who has it will be refused, and sharing again means creating a new one.';

if (typeof document !== 'undefined') {
  var byId = function (id) { return document.getElementById(id); };

  //< everything after the year in the title the server already rendered
  //  ("{{ year }} Wrapped - SpotifyTracker" - templates/wrapped.html's title
  //  block), derived rather than hardcoding "SpotifyTracker" a second time so
  //  a renamed base title (layout.html) still matches
  var WRAPPED_TITLE_SUFFIX = document.title.replace(/^\d+/, '');

  // --- the chart -------------------------------------------------------------

  // The time series is server-rendered into a JSON data island inside the swap
  // container, so a swap brings the new data with it. charts.js reads
  // window.__chartData at draw time, which is why this only has to reassign it.
  //
  // Deliberately no `interval` key. charts.js reads that as "these buckets are
  // hours" (interval === 'day' means Yesterday on /charts) and splits each label
  // on a space to get "HH:00". Wrapped's day buckets are whole dates with no
  // space in them, so the old loader's `__chartData.interval = groupBy` turned
  // every x-axis label and tooltip into "undefined" the moment someone picked
  // Trend buckets = Day. The full-page render never set it and was always
  // right; now both paths agree.
  var loadChartData = function () {
    var island = byId('wrapped-bootstrap');
    if (!island) return;
    window.__chartData = { timeSeries: JSON.parse(island.textContent).timeSeries };
  };
  //< at parse time, before charts.js runs its initial render
  loadChartData();

  // --- the stats-filter category nav -----------------------------------------

  // Remembered across swaps: the nav and the sections are re-rendered by the
  // server on every filter change, and the server has no idea which category
  // the user is looking at (it is not in the URL). Without this, changing the
  // sort order would silently bounce them back to All Stats.
  var activeStatsFilter = STATS_FILTER_ALL;

  var applyStatsFilter = function (filter) {
    var chosen = document.querySelector('.stats-filter-button[data-filter="' + filter + '"]');
    // A category with nothing in it is hidden server-side, so the year just
    // loaded can take the chosen category away underneath the user - show
    // everything rather than an empty page.
    if (!chosen || chosen.style.display === 'none') {
      filter = STATS_FILTER_ALL;
    }
    activeStatsFilter = filter;
    document.querySelectorAll('.stats-filter-button').forEach(function (button) {
      var pressed = button.dataset.filter === filter;
      button.classList.toggle('active', pressed);
      //< aria-pressed moves with the class: the pressed pill is otherwise
      //  colour alone, and a toggle button's state is what a screen reader reads
      button.setAttribute('aria-pressed', String(pressed));
    });
    document.querySelectorAll('[data-category]').forEach(function (div) {
      div.classList.toggle('visible', filter === STATS_FILTER_ALL || div.dataset.category === filter);
    });
  };
  applyStatsFilter(STATS_FILTER_ALL);

  // --- the Export Summary Card PNG -------------------------------------------

  // Drawn entirely from the button's data-*, which the server refreshes out of
  // band on every swap. Unchanged from the pre-htmx version except for taking
  // the button as an argument: it is a new element after each swap, so its
  // click handler has to be delegated (see the listener at the bottom).
  var exportSummaryCard = function (btn) {
    var canvas = document.createElement('canvas');
    canvas.width = 600;
    canvas.height = 900;
    var ctx = canvas.getContext('2d');

    var theme = document.documentElement.className || 'theme-rose';
    var gradStart = '#3c0b1f';
    var gradEnd = '#121212';
    var accentColor = '#FB717B';

    if (theme === 'theme-green') {
      gradStart = '#0b3c1d';
      gradEnd = '#121212';
      accentColor = '#1DB954';
    } else if (theme === 'theme-purple') {
      gradStart = '#2b1055';
      gradEnd = '#121212';
      accentColor = '#C77DFF';
    } else if (theme === 'theme-red') {
      gradStart = '#500505';
      gradEnd = '#121212';
      accentColor = '#FF4A4A';
    }

    var gradient = ctx.createLinearGradient(0, 0, 0, 900);
    gradient.addColorStop(0, gradStart);
    gradient.addColorStop(1, gradEnd);
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 600, 900);

    ctx.strokeStyle = accentColor + '44';
    ctx.lineWidth = 15;
    ctx.strokeRect(15, 15, 570, 870);

    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 36px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('SPOTIFY TRACKER', 300, 80);

    ctx.fillStyle = accentColor;
    ctx.font = 'bold 24px sans-serif';
    ctx.fillText(btn.dataset.year + ' WRAPPED', 300, 120);

    ctx.fillStyle = '#888888';
    ctx.font = '16px sans-serif';
    ctx.fillText('Listening Summary for @' + btn.dataset.user, 300, 150);

    var drawStat = function (label, val, x, y) {
      ctx.fillStyle = '#ffffff';
      ctx.font = 'bold 32px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(val, x, y);
      ctx.fillStyle = '#aaaaaa';
      ctx.font = '14px sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(label, x, y + 25);
    };

    drawStat('Total Plays', btn.dataset.plays, 180, 220);
    drawStat('Total Time', btn.dataset.time, 420, 220);
    drawStat('Unique Songs', btn.dataset.songs, 180, 310);
    drawStat('Unique Artists', btn.dataset.artists, 420, 310);
    drawStat('Longest Streak', btn.dataset.streak + (btn.dataset.streak === '1' ? ' day' : ' days'), 300, 400);

    ctx.fillStyle = 'rgba(255,255,255,0.05)';
    ctx.fillRect(40, 440, 520, 380);
    ctx.strokeStyle = accentColor + '33';
    ctx.lineWidth = 2;
    ctx.strokeRect(40, 440, 520, 380);

    ctx.fillStyle = accentColor;
    ctx.font = 'bold 18px sans-serif';
    ctx.textAlign = 'left';
    ctx.fillText('TOP HIGHLIGHTS', 65, 475);

    var truncate = function (str, len) { return str.length > len ? str.substring(0, len) + '..' : str; };

    var drawHighlight = function (label, value, y) {
      ctx.font = 'bold 17px sans-serif';
      ctx.textAlign = 'left';
      ctx.fillStyle = accentColor;
      ctx.fillText(label + ':', 65, y);

      ctx.font = '16px sans-serif';
      ctx.fillStyle = '#e0e0e0';
      ctx.fillText(truncate(value, 32), 65, y + 25);
    };

    drawHighlight('Top Song', btn.dataset.topsong, 505);
    drawHighlight('Top Artist', btn.dataset.topartist, 560);
    drawHighlight('Top Album', btn.dataset.topalbum, 615);
    drawHighlight('Peak Day', btn.dataset.peakday + ' (' + btn.dataset.peakplays + ' plays)', 670);
    drawHighlight('Discovered Songs', btn.dataset.discoveredsongs, 725);
    drawHighlight('Discovered Artists', btn.dataset.discoveredartists, 780);

    ctx.fillStyle = '#666666';
    ctx.font = 'italic 12px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('Generated via SpotifyTracker', 300, 860);

    var link = document.createElement('a');
    link.download = btn.dataset.user + '_' + btn.dataset.year + '_wrapped_summary.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
  };

  // --- the share modal -------------------------------------------------------
  // It lives outside the swap container, so these elements survive every swap
  // and can be bound directly. Only present when not publicView and the admin
  // has share links on, hence the ?. throughout.
  //
  // Focus moves with it. A role="dialog" is announced by focus landing inside
  // it - display:flex alone says nothing to a screen reader and left focus on
  // the Share button behind the overlay - so opening focuses the Close button,
  // and every close path hands focus back to whatever opened it; otherwise
  // Escape dropped it from the now display:none button to <body>. Same rule as
  // layout-chrome.js's drawer. The fallback is the Share button, for a dialog
  // the server rendered open (?openShareModal=1) that nothing on the page
  // opened - that case is focused on load too, right below, since opening it
  // never runs through openShareModal().

  var shareModalOpener = null;

  var openShareModal = function () {
    var modal = byId('shareLinkModal');
    if (!modal) return;
    shareModalOpener = document.activeElement;
    modal.style.display = 'flex';
    modal.querySelector('.share-modal-close')?.focus();
  };

  var closeShareModal = function () {
    var modal = byId('shareLinkModal');
    //< only an OPEN dialog hands focus back: the Escape listener below hears
    //  every keypress on the page and must not yank focus to the Share button
    if (!modal || modal.style.display !== 'flex') return;
    modal.style.display = 'none';
    (shareModalOpener || byId('shareWrappedBtn'))?.focus();
    shareModalOpener = null;
  };

  byId('shareWrappedBtn')?.addEventListener('click', openShareModal);
  byId('shareLinkModal')?.querySelector('.share-modal-close')?.addEventListener('click', closeShareModal);

  byId('shareLinkModal')?.addEventListener('click', function (e) {
    if (e.target === this) closeShareModal();
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeShareModal();
  });

  // ?openShareModal=1 renders the dialog already open (share-link create,
  // revoke and rate-limit redirects all use it) - nothing on the page called
  // openShareModal() to focus it, so a keyboard user lands with focus still on
  // <body> and role="dialog" never gets announced. Leave shareModalOpener
  // null: closing this one still falls back to the Share button, same as today.
  var serverOpenedModal = byId('shareLinkModal');
  if (serverOpenedModal && serverOpenedModal.style.display === 'flex') {
    serverOpenedModal.querySelector('.share-modal-close')?.focus();
  }

  var showShareLinkError = function (message) {
    var panelBody = byId('shareLinkPanelBody');
    if (!panelBody) return;
    var errorEl = panelBody.querySelector('.share-link-error');
    if (!errorEl) {
      errorEl = document.createElement('p');
      errorEl.className = 'share-link-error';
      errorEl.setAttribute('role', 'alert');
      errorEl.style.cssText = 'color: #ff4a4a; font-size: 0.9rem; margin: 0 0 0.75rem;';
      panelBody.prepend(errorEl);
    }
    errorEl.textContent = message;
  };

  // Event-delegated: the create/revoke forms below are replaced wholesale after
  // each round-trip (and the whole panel is replaced out of band on every year
  // switch), so a directly bound listener would be lost on the very first
  // submit.
  //
  // Deliberately still a fetch() rather than htmx: these POST to an ACTION
  // endpoint that answers with the share panel, not with the recap - a
  // different response for a different target, and one that has to be able to
  // report "you've reached the limit" from a 4xx body. Migrating it would mean
  // teaching those routes to return a fragment too; see the note in
  // routes/wrapped.py's createWrappedShareLink.
  /* Every submit takes the next sequence number; only the newest one may
     write. The panel lists one revoke form PER LINK beside the create form,
     and each one answers with the WHOLE panel - so two overlapping submits is
     the ordinary shape of this UI, not an edge case. Revoke A, then B: if A
     settled second it repainted the panel rendered while B still existed,
     putting a revoked link back on screen with a live Copy button handing out
     a dead URL and a Revoke that now answers 403. Same idiom as playlists.js's
     previewToken and the subnav pages' _navSeq. */
  var shareSubmitSeq = 0;

  byId('shareLinkModal')?.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches('.share-link-create-form, .share-link-revoke-form')) return;
    e.preventDefault();
    /* Revoke asks first: a revoked token is gone for good (friends holding the
       link are refused; sharing again means a new link to hand round), and the
       button sits 8px from Copy on a phone. HERE, not in an inline onsubmit:
       `return false` there only cancels the default action - the event still
       bubbles to this handler, which would fetch anyway. */
    if (form.matches('.share-link-revoke-form') && !window.confirm(SHARE_LINK_REVOKE_CONFIRM)) return;

    var panelBody = byId('shareLinkPanelBody');
    var url = new URL(form.action, window.location.href);
    url.searchParams.set('ajax', 'true');
    var seq = ++shareSubmitSeq;
    /* Disabled for the round trip: the cap is enforced per request, so a
       double-click on Create used to put a bucket one link over it. The
       server-side half of that is createShareLinkIfUnderCap, which closes the
       same window for two tabs and for curl. */
    var submitButton = form.querySelector('button[type="submit"], input[type="submit"]');
    if (submitButton) submitButton.disabled = true;

    fetch(url, { method: 'POST', body: new FormData(form) })
      .then(function (response) {
        // A 4xx here still carries the JSON the handler below wants (the
        // route's own error message - "reached the limit", "unknown expiry"),
        // so only the 401 is peeled off for the shared login redirect; a
        // .json() straight through couldn't tell "session expired" from
        // "invalid form", and both showed the generic line.
        if (window.AjaxStatus && window.AjaxStatus.redirectIfUnauthorized(response)) {
          return null;
        }
        return response.json();
      })
      .then(function (data) {
        if (!data) return;   //< navigating to /login
        /* Checked AFTER the 401 peel above and never before it: a 401 is news
           about the SESSION, which every in-flight submit shares, so whichever
           one notices acts on it (see playlists.js, same rule). */
        if (seq !== shareSubmitSeq) return;
        if (data.html !== undefined) {
          panelBody.innerHTML = data.html;
        } else {
          showShareLinkError(data.error || 'Something went wrong. Please try again.');
        }
      })
      .catch(function () {
        //< gated too: an error about a request the user has already moved past
        //  is as wrong as a stale repaint
        if (seq !== shareSubmitSeq) return;
        showShareLinkError('Something went wrong. Please try again.');
      })
      .finally(function () {
        /* A successful paint replaced this button along with the rest of the
           panel, so this only matters on the paths that did NOT repaint - a
           refusal or a network error, both of which have to stay retryable. */
        if (submitButton) submitButton.disabled = false;
      });
  });

  // --- htmx wiring -----------------------------------------------------------

  // Both of these buttons are inside content the server re-renders (the export
  // button out of band, the stats filters inside the swap container), so
  // neither can hold a listener of its own. The playlist download that used to
  // be handled here is the shared _playlist_download.html control now, and
  // chrome-common.js owns its (equally delegated) handler.
  document.body.addEventListener('click', function (evt) {
    if (!evt.target || !evt.target.closest) return;

    var statsFilterButton = evt.target.closest('.stats-filter-button');
    if (statsFilterButton) {
      applyStatsFilter(statsFilterButton.dataset.filter);
      return;
    }
    var exportButton = evt.target.closest('#exportWrappedBtn');
    if (exportButton) {
      exportSummaryCard(exportButton);
    }
  });

  // Drop the params the user has not set. htmx serializes every enabled control
  // in the form, so Trend buckets on Auto (the empty option) would otherwise put
  // `groupBy=` in the request - and hx-replace-url writes the requested URL back
  // to the address bar, so it becomes part of the link people copy. The old
  // hand-written builder skipped it with `if (groupBy)`. Shared with the other
  // htmx pages (static/js/htmx-filters.js), loaded before this file.
  document.body.addEventListener('htmx:configRequest', function (evt) {
    if (!evt.detail.elt || evt.detail.elt.id !== WRAPPED_FORM_ID) return;
    HtmxFilters.pruneEmptyParams(evt.detail.parameters);
    // X6 (2026-09-02 review): the marker header above tells
    // routes/wrapped.py's _genreCardIsAlreadyCorrect that #wrappedGenresCard
    // is unchanged and already on screen, so it answers with an hx-preserve
    // stub instead of recomputing the card - true only while that card is
    // actually still there. After a failed swap has emptied #wrappedResults
    // (see AjaxStatus's failure path), the card is gone, and vendored htmx's
    // hx-preserve only preserves an id it can still find: sending the marker
    // on the next filter change would swap the hollow stub in, and the real
    // genre card would stay missing until the next year switch. Guarded on
    // `headers` existing: every real htmx request carries one, but nothing
    // here needs it to.
    if (evt.detail.headers && !byId('wrappedGenresCard')) {
      delete evt.detail.headers['X-Wrapped-Filter-Change'];
    }
  });

  // Everything that has to happen once new markup is in the page. Scoped to the
  // main swap so the four out-of-band regions (which fire their own events)
  // don't run it four more times.
  //
  // afterSETTLE, not afterSwap: the new #timeSeriesChart carries the id of the
  // canvas it replaces, and htmx's settle step restores a same-id arrival's
  // ORIGINAL attributes 20ms after the swap - so a canvas sized and painted in
  // afterSwap had its width/height/style stripped again, which blanks the
  // bitmap. Every year and filter change after the first paint went invisible.
  // The full mechanism is written up once, on charts-page.js's listener;
  // pinned by tests/test_wrapped_page.js. (The title listener below stays on
  // afterSwap: it writes document.title, which settle never touches.)
  document.body.addEventListener('htmx:afterSettle', function (evt) {
    if (evt.target.id !== WRAPPED_RESULTS_ID) return;

    loadChartData();
    renderTimeSeriesChart();
    applyStatsFilter(activeStatsFilter);
    //< cover-art fade-ins are handled once for the whole app in
    //  static/js/chrome-common.js, which already owned this behaviour and now
    //  re-runs its sweep on htmx:afterSwap
  });

  // document.title stayed on whatever year the page first loaded with: a year
  // switch swaps the hero (its <h1>) and this hidden field out of band - both
  // sit OUTSIDE #wrappedResults (see _wrapped_hero.html and
  // _wrapped_year_field.html), so neither reaches the listener above, which is
  // deliberately scoped to the main results swap only. The field, not the h1
  // text: its value IS the plain year, with nothing to parse back out of
  // "Your 2025 Wrapped" / "{name}'s 2025 Wrapped".
  document.body.addEventListener('htmx:afterSwap', function (evt) {
    if (evt.target.id !== 'wrappedYearField') return;
    document.title = evt.target.value + WRAPPED_TITLE_SUFFIX;
  });

  // A genuine failure gets the shared inline error + Retry rather than a page
  // left dimmed under a URL that already says otherwise. Two things reach here
  // and nothing else: a 5xx, and - on the PUBLIC /shared/<token> page - the 404
  // or 429 a dead or hammered token earns. That route is not behind
  // @requiresUser, so it has no session to redirect (see routes/wrapped.py).
  // An expired session on the owner's own page never arrives here at all: the
  // server answers an htmx request with HX-Redirect, and the browser navigates.
  //
  // The shared helper, because this page has exactly one htmx region: the
  // year badges target #wrappedResults too, the export button is an
  // out-of-band fragment of the same response, and the share forms use their
  // own fetch() above. The hand-rolled block this replaced took no event, so it
  // answered every failed swap on the page - the helper scopes on the target.
  //
  // js-F1 (2026-09-02 review): re-issued off the FORM, not the address bar -
  // the charts-page.js/history-page.js/top-list.js pattern. A 4xx/5xx never
  // touches the URL (htmx updates history only inside its successful-swap
  // branch), so after a failed filter change the controls already show the
  // new selection while the URL still says the old one; retrying the URL
  // would re-request the OLD selection under a page that already shows the
  // new one. Re-serialising the form also runs the configRequest veto/prune
  // above (its elt is the form, so the marker-header guard applies here too)
  // and inherits hx-replace-url and hx-sync. The bare hx-get path, because
  // htmx appends the form's values to whatever path it is handed.
  HtmxFilters.onSwapFailure(WRAPPED_RESULTS_ID, function () {
    var form = byId(WRAPPED_FORM_ID);
    htmx.ajax('GET', form.getAttribute('hx-get'),
              { source: form, target: '#' + WRAPPED_RESULTS_ID, swap: 'innerHTML' });
  });
}
//< no module.exports: nothing here is pure enough to unit-test off the DOM. The
//  shared filter helpers live in static/js/htmx-filters.js, which has its own
//  plain-node test (tests/test_htmx_filters.js).
