// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// The dashboard: what is left of its browser logic once htmx owns the
// request/swap layer (see templates/tracks.html for the attributes), plus the
// two things htmx could not take - the now-playing poll and the
// listening-calendar tooltip.
//
// Extracted from tracks.html so it can be linted - it was the single largest
// unlinted file in the project. USERNAME stays behind as a data island because
// the poll builds cover-image URLs from it.
//
// Three of this file's four fetches went to htmx, and the mapping is worth
// keeping written down:
//
//   loadDashboardSummary + AbortController -> hx-get + hx-sync="...:replace"
//   replaceDashboardUrl                    -> hx-replace-url="true"
//   updateDashboardFilters' set/delete     -> the form's own inputs, serialized
//     surgery on the query string             by htmx, plus `disabled` on the
//                                             custom dates when not custom
//   updateDashboardDateFilter's range      -> HtmxFilters.rangeProblem, vetoing
//     check + error styling                   the request in htmx:configRequest
//   the loading-fade bookkeeping           -> .htmx-fade-target + hx-indicator
//   the 401 -> /login branch               -> HX-Redirect, sent by the server
//   the popstate handler                   -> nothing, and deliberately: every
//     URL update here REPLACES, so this page never puts an entry on the history
//     stack for itself. It only ever ran on a cross-document Back, which
//     reloads the page server-side anyway - and DASHBOARD_DEFAULT_WINDOW, the
//     fallback it needed to re-select the right option, went with it.
//   the Discover + trends card fetches     -> hx-trigger="load" on each card,
//     and the DOM they built by hand          answering with markup
//
// The FOURTH fetch, the now-playing poll below, deliberately stayed. htmx can
// issue a request every 15s, but not do either of the things this poll exists
// for: it renders DATA into a dozen elements with per-link logic (an internal
// /song/<id> link only when the track has actually been played before, a
// Spotify link otherwise, plain text with no id at all), and it must STOP on a
// 401 rather than navigate - a background poll yanking the page to /login
// mid-read is the exact bug its 401 branch was added to fix, while every htmx
// request in the app is answered with HX-Redirect, which navigates. Stopping a
// poll client-side needs hx-on::, and that compiles a JS expression with the
// Function constructor, which this page's CSP denies.

//< the form htmx watches, in tracks.html
var DASHBOARD_FORM_ID = 'dashboardFilters';
//< the swap target
var DASHBOARD_SUMMARY_ID = 'dashboardSummary';

var byId = function (id) { return document.getElementById(id); };

//< how far into the track the bar sits, as a percentage, or null for a track
//  with no duration to measure against (podcasts, local files)
var PROGRESS_BAR_FULL_PERCENT = 100;
//< consecutive failed polls before the panel is marked out of date. A 15s poll
//  drops one request on any flaky connection, so one is not news; three is 45
//  seconds of silence, by which point what is on screen may be minutes old.
var NOW_PLAYING_STALE_AFTER_FAILURES = 3;

// Where the progress bar should sit `elapsedMs` after a payload said the track
// was at `positionMs`. The poll only lands every 15 seconds, so without this
// the bar stepped once per poll; this is the same arithmetic the server does
// against the connect state's own timestamp (see getNowPlaying).
//
// Pure and exported (see the bottom of this file) for tests/test_dashboard_page.js.
function progressPercent(positionMs, durationMs, elapsedMs, isPaused) {
  if (!(durationMs > 0)) return null;
  //< a paused track does not advance, and wall-clock time is not monotonic - an
  //  NTP correction can hand back a negative elapsed, which must not rewind it
  var position = isPaused ? positionMs : positionMs + Math.max(0, elapsedMs);
  return Math.max(0, Math.min(PROGRESS_BAR_FULL_PERCENT,
                              (position / durationMs) * PROGRESS_BAR_FULL_PERCENT));
}

// Whether the panel should say it is out of date. Its own function so the
// threshold is asserted somewhere: the failure path is a catch() nothing else
// exercises, and "keep the last state" silently rendering a track that ended
// twenty minutes ago is the defect this exists to end.
function pollIsStale(consecutiveFailures) {
  return consecutiveFailures >= NOW_PLAYING_STALE_AFTER_FAILURES;
}

// Whether the bar may move on this tick, given the last payload and whether the
// poll that delivered it is still getting through.
//
// Separate from progressPercent because that function is happy to keep answering
// forever off a stale anchor - which is what it did. The bar runs on its own 1s
// timer, so it is the one thing on this card that goes on LOOKING live after the
// 15s poll behind it has stopped: a dimmed card captioned Stale, with a progress
// bar still sliding toward the end of a track nothing has confirmed is playing.
function progressShouldAdvance(playing, isStale) {
  return !!playing && !playing.isPaused && !isStale;
}

// X5 (2026-09-02 review): whether the cover's onerror fallback should actually
// reassign its src. templates/tracks.html used to null the handler out after
// firing once (`this.onerror=null`), so a SECOND failure - a different track
// whose art also hasn't been fetched yet - showed a broken-image icon with no
// handler left to catch it. Owned here instead and left armed forever: this
// guard is what keeps that idempotent rather than looping, since the
// placeholder is a data: URI that can never itself fail to "load".
function coverErrorShouldFallback(currentSrc, placeholderSrc) {
  return currentSrc !== placeholderSrc;
}

// Whether a freshly polled imageId's URL differs from what the cover was last
// TOLD to show - deliberately NOT from its current src attribute, which the
// fallback above may have already rewritten to the placeholder. Comparing
// against that rewritten attribute (the old code's `cover.getAttribute('src')`)
// made a not-yet-fetched cover's URL look "new" again on every single poll,
// since the placeholder never equals a real image URL - a fetch retried every
// 15 seconds for as long as the track keeps playing.
function coverSrcNeedsUpdate(wantedSrc, newSrc) {
  return wantedSrc !== newSrc;
}

// The streak calendar tooltip's second line. Pure and exported (see the bottom
// of this file) because the DOM code that shows it cannot be tested from here,
// and a source-shape assertion on it proved worthless: the check looked for the
// data-time read, which survives reading the value and then dropping it.
function calendarTooltipLabel(count, timeText) {
  var plays = count + ' play' + (String(count) === '1' ? '' : 's');
  //< a day with nothing played carries no data-time at all, so this is null
  return timeText ? plays + ' · ' + timeText : plays;
}

// What the friends strip currently shows, as a comparable string, so a poll
// that brings no news leaves the DOM alone.
//
// The chips are LINKS now, which turns a 15-second rebuild from invisible
// churn into a defect: tearing down the element under a cursor swallows the
// click (mousedown and mouseup on different elements dispatch no click), and
// tearing it down under keyboard focus drops activeElement back to <body>, so
// the user's next Tab restarts at the top of the page with nothing to explain
// why. Neither could happen while the chips were inert divs.
//
// The separators are control characters rather than punctuation: a track named
// "One Two" beside an empty artist must not serialize like "One" beside "Two".
//
// The artists are flattened into the same field list rather than getting a
// separator level of their own: they sit last, so a chip crediting one more of
// them is simply a longer field list and cannot serialize like a shorter one.
function friendsStripSignature(friends, moreCount) {
  return (friends || []).map(function (friend) {
    var fields = [friend.username, friend.displayName, friend.name,
                  friend.artistsText, friend.imageId, friend.compareUrl,
                  friend.trackUrl];
    (friend.artists || []).forEach(function (artist) {
      fields.push(artist.name, artist.url);
    });
    return fields.join('\u001f');
  }).join('\u001e') + '\u001d' + moreCount;
}

// The <a> behind anything a chip links, or an inert <span> where there is
// nothing to link to (a track with no id, artists the connect-state fallback
// knows only by name). An <a href=""> would be a link back to the current
// page, which is worse than no link at all.
//
// Internal or external is read off the URL rather than carried as a payload
// field of its own: every internal link is built with url_for and so arrives
// as an app-relative path, and the only other thing the payload ever holds is
// a Spotify URL. Only that one opens in a new tab - following a link that
// stays inside the app should not spawn one.
function friendsChipAnchor(url, className) {
  var el = document.createElement(url ? 'a' : 'span');
  el.className = className;
  if (url) {
    el.href = url;
    if (url.charAt(0) !== '/') {
      el.target = '_blank';
      el.rel = 'noreferrer noopener';
    }
  }
  return el;
}

// One of the three things a chip NAMES. The cover art uses the anchor above
// directly, since it wraps an image rather than text.
//
// textContent, not innerHTML: every one of these strings is another user's
// data, and two of them are names Spotify supplied.
function friendsChipLink(text, url, className) {
  var el = friendsChipAnchor(url, className);
  el.textContent = text || '';
  return el;
}

// Called from the Time Period select's onchange. Runs before htmx's listener
// (an inline on*= handler fires at the target; htmx's is on the form and fires
// as the event bubbles), so the disabled flags are already right by the time
// the request is serialized.
//
// `disabled`, not merely hidden: a disabled control is not serialized, which is
// what keeps a stale custom range out of the request - and so out of the URL -
// after switching back to a named interval.
window.updateDashboardInterval = function () { HtmxFilters.syncCustomRange('dashboardCustomDates'); };

// The one place a request gets vetoed, and what stops "custom" firing one the
// moment it is selected: a range with no dates yet is RANGE_INCOMPLETE, which
// is exactly what the old handler's early return covered. Shared with every
// other filter page - /history, /charts, /genres, /compare and the Top lists
// render the same control set, so the logic lives once in
// static/js/htmx-filters.js and tests/test_custom_date_controls.py keeps it
// that way.
document.body.addEventListener('htmx:configRequest', function (evt) {
  if (!evt.detail.elt || evt.detail.elt.id !== DASHBOARD_FORM_ID) return;
  var problem = HtmxFilters.rangeProblemFromDom();
  HtmxFilters.showRangeError(problem);
  if (problem !== HtmxFilters.RANGE_OK) {
    evt.preventDefault();
    return;
  }
  HtmxFilters.pruneEmptyParams(evt.detail.parameters);
});

// A genuine failure replaces the stuck/stale cards with an inline error +
// Retry, so the numbers are never silently stale under a URL that says
// otherwise. Scoped to the summary swap: the two deferred cards below fail
// independently and keep their own placeholders rather than blanking this.
//
// The retry is issued off the FORM, not the address bar. A 4xx/5xx never
// touches the URL (htmx updates history only inside its successful-swap
// branch), so after a failed filter change the select already shows the new
// choice while the URL still says the old one - and a retry of the URL rendered
// the old range under the new selection, with nothing to say so. Re-serialising
// the form requests what is on screen, runs the configRequest veto/prune above
// (its elt is the form) and inherits hx-replace-url and hx-sync. The bare
// hx-get path, because htmx appends the form's values to whatever path it is
// handed.
var reportDashboardFailure = function (evt) {
  var target = byId(DASHBOARD_SUMMARY_ID);
  if (!target || !window.AjaxStatus || !evt.detail || evt.detail.target !== target) return;
  window.AjaxStatus.renderInto(target, function () {
    var form = byId(DASHBOARD_FORM_ID);
    htmx.ajax('GET', form.getAttribute('hx-get'),
              { source: form, target: '#' + DASHBOARD_SUMMARY_ID, swap: 'innerHTML' });
  });
};
document.body.addEventListener('htmx:responseError', reportDashboardFailure);
document.body.addEventListener('htmx:sendError', reportDashboardFailure);

// Now Playing: poll the listener's cached connect state (no Spotify
// calls server-side) and show the now-playing sub-panel only while
// something is playing. The listening streak below it stays put.
(function () {
  var NOW_PLAYING_POLL_MS = 15000;
  //< the bar's own clock: fast enough that the track visibly moves, slow enough
  //  to be free. The bar's 0.6s CSS transition smooths the step between ticks.
  var PROGRESS_TICK_MS = 1000;
  var card = document.getElementById('nowPlayingCard');
  var panel = document.getElementById('nowPlayingPanel');
  // Friends' current tracks ride along on this same poll (see
  // routes/system.py) so the block costs no extra requests. It sits in the
  // same card as the user's own now-playing, but is looked up separately
  // because the guard below has to know about BOTH: the two halves are
  // independently absent (the friends block isn't rendered without shares or
  // with the admin switch off), so neither may gate the other's polling.
  var friendsRow = document.getElementById('friendsListening');
  var friendsChips = document.getElementById('friendsListeningChips');
  var friendsMore = document.getElementById('friendsListeningMore');
  if (!card && !friendsRow) return;   //< nothing on this page to poll for

  // X5 (2026-09-02 review): owned here, once, instead of the self-nulling
  // inline onerror templates/tracks.html used to carry - see
  // coverErrorShouldFallback's comment for why that broke a second failure.
  var cover = document.getElementById('nowPlayingCover');
  if (cover) {
    cover.onerror = function () {
      if (coverErrorShouldFallback(this.src, window.PLACEHOLDER_IMG)) this.src = window.PLACEHOLDER_IMG;
    };
  }

  function render(np) {
    if (!card) return;   //< now-playing card absent; the strip may still poll
    if (!np || !np.name) {
      card.style.display = 'none';
      if (panel) panel.classList.remove('has-now-playing');
      playing = null;   //< or the tick keeps advancing a bar nobody is playing
      return;
    }
    var nameEl = document.getElementById('nowPlayingName');
    nameEl.textContent = np.name;

    // Link the title (and cover) to our own /song/<id> page when the user
    // has actually played this track before; otherwise fall back to Spotify
    // (a track playing for the first time has no completed play logged yet,
    // so the internal detail page would have nothing to show). No id at all
    // (local files / podcasts / transitional states) -> plain text.
    function applyTrackLink(el) {
      if (!el) return;
      if (np.trackId && np.trackPlayed) {
        el.href = '/song/' + encodeURIComponent(np.trackId);
        el.removeAttribute('target');
        el.removeAttribute('rel');
      } else if (np.trackId) {
        el.href = 'https://open.spotify.com/track/' + encodeURIComponent(np.trackId);
        el.target = '_blank';
        el.rel = 'noreferrer noopener';
      } else {
        el.removeAttribute('href');
        el.removeAttribute('target');
        el.removeAttribute('rel');
      }
    }
    applyTrackLink(nameEl);
    applyTrackLink(document.getElementById('nowPlayingCoverLink'));

    // Artist names: link each to our /artist/<id> page when the user has
    // played that artist before, else to their Spotify page (first play).
    // The first-listen fallback carries no artist ids, so keep plain text.
    var artistsEl = document.getElementById('nowPlayingArtists');
    if (np.artists && np.artists.length) {
      artistsEl.textContent = '';
      np.artists.forEach(function (a, i) {
        if (i) artistsEl.appendChild(document.createTextNode(', '));
        var el;
        if (a.id && a.played) {
          el = document.createElement('a');
          el.href = '/artist/' + encodeURIComponent(a.id);
        } else if (a.id) {
          el = document.createElement('a');
          el.href = 'https://open.spotify.com/artist/' + encodeURIComponent(a.id);
          el.target = '_blank';
          el.rel = 'noreferrer noopener';
        } else {
          el = document.createElement('span');
        }
        el.className = 'now-playing-artist-link';
        el.textContent = a.name;
        artistsEl.appendChild(el);
      });
    } else {
      artistsEl.textContent = np.artistsText || '';
    }

    var stateEl = document.getElementById('nowPlayingState');
    stateEl.textContent = np.isPaused ? 'Paused' : 'Playing';
    stateEl.classList.toggle('paused', !!np.isPaused);
    var coverLink = document.getElementById('nowPlayingCoverLink');
    if (np.imageId) {
      var src = '/img/' + encodeURIComponent(USERNAME) + '/tracks/' + encodeURIComponent(np.imageId) + '.jpeg';
      //< dataset.wanted, not the src attribute: the onerror fallback above may
      //  have already rewritten that to the placeholder, and comparing against
      //  the rewritten value made a not-yet-fetched cover re-request itself
      //  every 15s poll for as long as the track keeps playing (see
      //  coverSrcNeedsUpdate's comment).
      if (coverSrcNeedsUpdate(cover.dataset.wanted, src)) {
        cover.dataset.wanted = src;
        cover.src = src;
      }
      cover.style.display = '';
      if (coverLink) coverLink.style.display = '';
    } else {
      cover.style.display = 'none';
      if (coverLink) coverLink.style.display = 'none';
    }
    // What the bar needs to keep moving between polls: the position this
    // payload reported and the moment it arrived. tickProgress does the rest.
    playing = { positionMs: np.positionMs, durationMs: np.durationMs,
                isPaused: !!np.isPaused, atMs: Date.now() };
    drawProgress(0);
    card.style.display = '';
    if (panel) panel.classList.add('has-now-playing');
  }

  //< the last payload's position + when it landed, or null while nothing is
  //  playing. Read by the tick below, which runs on its own (much faster) timer.
  var playing = null;
  //< kept beside `playing` because the tick needs both: an anchor is only worth
  //  advancing while the poll that set it is still getting through
  var isStale = false;

  function drawProgress(elapsedMs) {
    var bar = document.getElementById('nowPlayingBar');
    if (!bar) return;
    var percent = playing
      ? progressPercent(playing.positionMs, playing.durationMs, elapsedMs, playing.isPaused)
      : null;
    if (percent === null) {
      bar.parentElement.style.display = 'none';   //< nothing to measure against
      return;
    }
    bar.parentElement.style.display = '';
    bar.style.width = percent + '%';
  }

  function tickProgress() {
    if (!progressShouldAdvance(playing, isStale)) return;
    drawProgress(Date.now() - playing.atMs);
  }

  //< what the chips currently show, so an unchanged poll leaves them alone -
  //  see friendsStripSignature for why rebuilding links every 15s is not free
  var renderedFriends = null;

  function renderFriends(friends, moreCount) {
    if (!friendsRow) return;   //< admin switch off: the row isn't rendered
    friends = friends || [];
    if (!friends.length) {
      friendsRow.style.display = 'none';
      if (panel) panel.classList.remove('has-friends');
      renderedFriends = null;
      return;
    }
    //< the panel draws the divider above the streak off this mark, so it goes
    //  BEFORE the unchanged-poll early return below: a poll that brings no news
    //  still has to leave the panel marked
    if (panel) panel.classList.add('has-friends');
    var signature = friendsStripSignature(friends, moreCount);
    if (signature === renderedFriends) return;
    renderedFriends = signature;
    friendsChips.textContent = '';
    friends.forEach(function (friend) {
      // The chip is a container of INDEPENDENT links, not one link wrapping
      // everything: it shows a person, a track and some artists, and those are
      // three different places to go. (It used to be a single <a> to /compare,
      // which made the track title a link to a page about the friend.) Every
      // href is built server-side - see routes/system.py's _friendChip - so
      // they all go through url_for like the rest of the app.
      var chip = document.createElement('div');
      chip.className = 'friends-listening-chip';

      var cover = document.createElement('img');
      cover.className = 'friends-listening-cover';
      cover.alt = '';
      //< catalog images are shared files; the <username> segment is only an
      //  authorization check, so they resolve under the VIEWER's own name
      cover.src = friend.imageId
        ? '/img/' + encodeURIComponent(USERNAME) + '/tracks/' + encodeURIComponent(friend.imageId) + '.jpeg'
        : window.PLACEHOLDER_IMG;
      cover.onerror = function () { this.onerror = null; this.src = window.PLACEHOLDER_IMG; };
      // The artwork is the track's, so it goes where the track's title goes -
      // the same destination, not a fourth one. Kept out of the tab order and
      // out of the accessibility tree because it is a DUPLICATE of the title
      // link beside it: four chips would otherwise cost eight tab stops to
      // reach four places, and a screen reader would announce every track
      // twice. The img is alt="" already, so nothing is lost by hiding it.
      var coverLink = friendsChipAnchor(friend.trackUrl, 'friends-listening-cover-link');
      coverLink.setAttribute('aria-hidden', 'true');
      coverLink.tabIndex = -1;
      coverLink.appendChild(cover);
      chip.appendChild(coverLink);

      var meta = document.createElement('div');
      meta.className = 'friends-listening-meta';

      var title = document.createElement('div');
      title.className = 'friends-listening-track';
      title.appendChild(friendsChipLink(friend.displayName || friend.username,
                                        friend.compareUrl,
                                        'friends-listening-link friends-listening-name'));
      title.appendChild(document.createTextNode(' · '));
      title.appendChild(friendsChipLink(friend.name, friend.trackUrl,
                                        'friends-listening-link'));

      var artist = document.createElement('div');
      artist.className = 'friends-listening-artist';
      if (friend.artists && friend.artists.length) {
        friend.artists.forEach(function (each, index) {
          if (index) artist.appendChild(document.createTextNode(', '));
          artist.appendChild(friendsChipLink(each.name, each.url,
                                             'friends-listening-link'));
        });
      } else {
        //< a first listen isn't in the catalog yet, so the payload has the
        //  artist names but no ids to link them by (see getFriendsNowPlaying)
        artist.textContent = friend.artistsText || '';
      }

      meta.appendChild(title);
      meta.appendChild(artist);
      chip.appendChild(meta);
      friendsChips.appendChild(chip);
    });

    if (moreCount > 0) {
      friendsMore.textContent = '+' + moreCount + ' more';
      friendsMore.style.display = '';
    } else {
      friendsMore.style.display = 'none';
    }
    friendsRow.style.display = '';
  }

  var pollHandle = null;
  var tickHandle = null;
  var consecutiveFailures = 0;
  // Two in-flight polls can resolve out of order (a response only has to
  // outlive the 15s interval), and these handlers used to apply whichever
  // landed LAST: stale data over newer, a late failure counted against a
  // healthy feed, and a slow success resolving after the 401 branch stopped
  // both timers un-dimmed a card that will never poll again.
  //
  // The rule is "a response acts unless a NEWER response already acted", so
  // the counter that matters is the newest one APPLIED, not the last one
  // ISSUED. Comparing against the last issued request looks equivalent and is
  // not: VisibilityPoll.start is a bare setInterval with no in-flight guard,
  // so on a feed slower than NOW_PLAYING_POLL_MS every response is superseded
  // before it settles, and guarding on `seq !== pollSeq` dropped ALL of them -
  // no render, no failure counted, no Stale pill, and an expired session that
  // never stopped the poll. That is the frozen-card defect this whole block
  // exists to prevent, reintroduced for the slow connections that need it most.
  //
  // A sequence number rather than an AbortController, for the reason
  // profile-page.js/admin-page.js give: aborting REJECTS the superseded
  // promise, which here would land in the catch and count toward the stale
  // threshold.
  var pollSeq = 0;      //< last request issued
  var appliedSeq = 0;   //< newest response that has already had its say
  var pollStopped = false; //< an accepted 401 is terminal for every in-flight response

  //< true when this response is still the newest word and may act; claiming is
  //  what makes it the newest word, so each response claims at most once
  function claimPoll(seq) {
    if (seq <= appliedSeq) return false;
    appliedSeq = seq;
    return true;
  }

  // Both blocks are fed by the one request, so when it stops answering both are
  // equally out of date - and neither said so: the card kept rendering a track
  // that may have ended twenty minutes ago exactly like one playing now. The
  // panel dims and the state pill says Stale until a poll gets through again.
  //
  // The progress bar is the third thing this governs, and the least obvious:
  // it moves on its own timer, so nothing here would have stopped it (see
  // progressShouldAdvance).
  function setStale(stale) {
    isStale = stale;
    if (card) card.classList.toggle('is-stale', stale);
    if (friendsRow) friendsRow.classList.toggle('is-stale', stale);
    var stateEl = document.getElementById('nowPlayingState');
    if (stateEl && stale) {
      stateEl.textContent = 'Stale';
      stateEl.title = 'The last few updates did not get through - this may be out of date';
    } else if (stateEl) {
      stateEl.removeAttribute('title');   //< render() writes Playing/Paused back
    }
  }

  function poll() {
    if (pollStopped) return;
    var seq = ++pollSeq;
    fetch('/api/now-playing')
      .then(function (resp) {
        if (pollStopped) return null;
        // An expired session used to be treated as a transient blip, so the
        // card and the friends block sat frozen on stale content for as long as
        // the tab stayed open, polling every 15s forever and never redirecting.
        // A background poll shouldn't yank the page away mid-read, so it stops
        // rather than navigates - the next click on anything goes through the
        // normal login redirect. What is on screen is frozen from here on, so
        // it is marked as such rather than left looking live.
        //
        // Both timers, not just the fetching one: the bar's tick is the leak
        // this whole change closed, reintroduced. stop() is permanent on both.
        if (resp.status === 401) {
          //< claimed here rather than at the top of this handler: a 401 is
          //  evidence about the SESSION, which every in-flight request shares,
          //  so it acts unless something newer already has. The success path
          //  below claims later, after its body read. Wrapped rather than an
          //  early return so this branch keeps ONE exit: the stale/stop pins in
          //  tests/test_background_polls.py read the source text from the 401
          //  check to the branch's first return, and an early one hid them.
          if (claimPoll(seq)) {
            pollStopped = true;
            if (pollHandle) pollHandle.stop();
            if (tickHandle) tickHandle.stop();
            setStale(true);
          }
          return null;
        }
        //< peeked, not claimed: a superseded response is not worth reading a
        //  body for, and returning null here keeps a stale non-2xx out of the
        //  catch (a superseded failure is not news about the live feed)
        if (seq <= appliedSeq) return null;
        //< thrown, not returned as null: a 500 is a FAILED poll, and the null
        //  branch below cannot tell one from "the session ended" without it
        if (!resp.ok) throw new Error('now-playing poll failed: ' + resp.status);
        return resp.json();
      })
      .then(function (data) {
        if (pollStopped) return;
        if (!data) return;   //< the 401/superseded branches above have had their say
        //< claimed only now: resp.json() was awaited in between, and a newer
        //  response may have landed - and applied - inside that window
        if (!claimPoll(seq)) return;
        consecutiveFailures = 0;
        setStale(false);
        render(data.nowPlaying);
        renderFriends(data.friends, data.friendsMoreCount);
      })
      .catch(function () {
        if (pollStopped) return;
        //< a superseded failure is not news about the LIVE feed
        if (!claimPoll(seq)) return;
        //< one dropped request is not news on a 15s poll; three in a row is
        consecutiveFailures += 1;
        if (pollIsStale(consecutiveFailures)) setStale(true);
      });
  }

  // Not a bare setInterval: a hidden tab has nobody reading this, and the
  // request is not cheap (a fan-out over everyone the viewer shares with). The
  // tick has its own handle so the bar stops moving in a background tab too,
  // where its arithmetic would be answering a question nobody asked.
  pollHandle = window.VisibilityPoll.start(poll, NOW_PLAYING_POLL_MS);
  tickHandle = window.VisibilityPoll.start(tickProgress, PROGRESS_TICK_MS);
})();

// The two deferred cards fail independently of the summary and of each other,
// and htmx swaps nothing on a non-2xx - so without these both would sit on a
// placeholder claiming work is still in progress. They fail DIFFERENTLY, which
// is why this is two handlers and not one:
//
//   Discover goes blank. Its three states are locked / empty / a list, and the
//     first two are statements about the user's own library that a server error
//     is no evidence for - the old fetch()'s catch made the same call.
//   Trends gets the shared inline error + Retry, which is what its own catch
//     did: three "Loading listening trends…" placeholders say nothing useful on
//     their own and would otherwise stay up forever.
//
// Bound to sendError as well as responseError, like the summary handler above:
// the two are one failure to a reader of the page (nothing came back), and
// sendError is the half that fires when the request never reached the server -
// a dropped connection, DNS, offline. Bound to responseError alone, exactly
// those left both cards on a placeholder claiming work was still in progress.
var reportDeferredCardFailure = function (evt) {
  if (!evt.detail) return;
  var card = document.getElementById('discoverCard');
  if (card && evt.detail.target === card) {
    card.replaceChildren();
    return;
  }
  var trends = document.getElementById('dashboardTrendsContainer');
  if (trends && evt.detail.target === trends && window.AjaxStatus) {
    window.AjaxStatus.renderInto(trends, function () {
      htmx.ajax('GET', '/api/dashboard-trends',
                { target: '#dashboardTrendsContainer', swap: 'innerHTML' });
    });
  }
};
document.body.addEventListener('htmx:responseError', reportDeferredCardFailure);
document.body.addEventListener('htmx:sendError', reportDeferredCardFailure);

// Listening calendar: a cursor-following overlay on hover (like the charts
// page tooltips), replacing the native `title` hint so the day's play count
// shows instantly instead of after the browser's slow title-hover delay.
// Reuses the global #chartTooltip element + .chart-tooltip styling and is
// delegated off the grid so all ~370 day cells share a single handler.
(function () {
  var grid = document.querySelector('.streak-calendar-grid');
  if (!grid) return;

  function ensureTooltip() {
    var tip = document.getElementById('chartTooltip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'chartTooltip';
      tip.className = 'chart-tooltip';
      document.body.appendChild(tip);
    }
    return tip;
  }

  function hideTooltip() {
    var tip = document.getElementById('chartTooltip');
    if (tip) tip.style.display = 'none';
  }

  // Build the date from its parts so the label doesn't slip a day in
  // timezones behind UTC (new Date("2026-07-20") parses as UTC midnight).
  function formatDay(iso) {
    var p = iso.split('-');
    var d = new Date(+p[0], (+p[1]) - 1, +p[2]);
    return d.toLocaleDateString(undefined,
      { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
  }

  //< bound once at load: the calendar is unfiltered, so it is never swapped
  grid.addEventListener('mousemove', function (evt) {
    // Future cells carry no data-date, so they (and the gaps) get no tooltip.
    var cell = evt.target.closest('.streak-calendar-day[data-date]');
    if (!cell) { hideTooltip(); return; }
    var count = cell.getAttribute('data-count');
    //< absent on a day with nothing played, which is why the label below is
    //  built rather than concatenated unconditionally: "0 plays - 0s" says
    //  the same thing twice
    var timeText = cell.getAttribute('data-time');
    var tip = ensureTooltip();
    // Built as nodes rather than an innerHTML concat: both values come off
    // data-* attributes, and reinterpreting DOM text as HTML is the one
    // step that would turn a future non-numeric count into markup.
    var day = document.createElement('strong');
    day.textContent = formatDay(cell.getAttribute('data-date'));
    tip.replaceChildren(day, document.createElement('br'),
      document.createTextNode(calendarTooltipLabel(count, timeText)));
    tip.style.left = (evt.clientX + 14) + 'px';
    tip.style.top = (evt.clientY + 14) + 'px';
    tip.style.display = 'block';
  });
  grid.addEventListener('mouseleave', hideTooltip);
})();

// The two pure decisions at the top of this file, for tests/test_dashboard_page.js.
// Same feature detection the other dual-use scripts here use - a browser has no
// `module`, so this is a no-op there. Everything else in this file is DOM wiring
// or a poll, which node cannot exercise; these two are the parts where being
// wrong is invisible from the outside.
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    calendarTooltipLabel: calendarTooltipLabel,
    friendsStripSignature: friendsStripSignature,
    friendsChipAnchor: friendsChipAnchor,
    friendsChipLink: friendsChipLink,
    progressPercent: progressPercent,
    progressShouldAdvance: progressShouldAdvance,
    pollIsStale: pollIsStale,
    coverErrorShouldFallback: coverErrorShouldFallback,
    coverSrcNeedsUpdate: coverSrcNeedsUpdate,
  };
}
