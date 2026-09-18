// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Shared chrome for every authenticated page: the jump-to-page box and the
// scroll-to-top button.
//
// Lived inline in layout.html, where no linter could see it. Moved verbatim -
// it carries no Jinja at all, and a classic <script src> in the same position
// executes in the same order, so the inline handlers keep resolving these off
// window exactly as before.
//
// It also carried three debounced search-filter helpers (scheduleSearchFilter,
// handleSearchKeydown, handleSearchBlur) until the htmx migration replaced
// every search box's on*= wiring with hx-trigger="input changed delay:400ms"
// (see the note at templates/compare.html). They were removed once nothing
// called them: their tests still passed, which is exactly what made them read
// as load-bearing to anyone wiring up a new filter page.

// _pagination.html's "Go to page" input - Enter navigates, clamped to
// [1, totalPages] so a stray typo can't request a nonexistent page.
window.handleJumpToPageKeydown = function(event, totalPages) {
  if (event.key !== 'Enter') {
    return;
  }
  event.preventDefault();

  const value = parseInt(event.target.value, 10);
  if (!Number.isInteger(value)) {
    return;
  }

  const page = Math.min(Math.max(value, 1), totalPages);

  // A page can register window.__paginationAjaxHandler (see
  // history.html) to handle the jump in place instead of navigating -
  // unset on every other page, so this falls through to the default.
  if (window.__paginationAjaxHandler) {
    window.__paginationAjaxHandler(page);
    return;
  }

  const params = new URLSearchParams(window.location.search);
  params.set('page', page);
  window.location = window.location.pathname + '?' + params.toString();
};

//< how often the topbar's two background checks ask. Both go through
//  VisibilityPoll (loaded just before this file), so a background tab asks for
//  neither - see static/js/visibility-poll.js for why that is not just tidiness.
const VERSION_CHECK_POLL_MS = 15 * 60 * 1000;
const LISTENER_STATUS_POLL_MS = 10 * 1000;

(function(){
  const badge = document.getElementById('version-badge');
  const textEl = document.getElementById('version-badge-text');

  function hideBadge(){
    badge.style.display = 'none';
  }
  function checkVersion(){
    fetch('/version_status').then(r=>r.json()).then(data=>{
      if(data && data.latest){
        textEl.textContent = `New version: ${data.latest} (you: ${data.current})`;
        badge.style.display = 'inline-flex';
      } else {
        hideBadge();
      }
    }).catch(()=>{});
  }

  window.VisibilityPoll.start(checkVersion, VERSION_CHECK_POLL_MS);
})();

(function(){
  const statusPill = document.getElementById('listener-status-pill');
  //< UI-07 (2026-09-02 review): the pill's state was colour (className) and a
  //  hover-only title - nothing a screen reader could read without hovering
  //  an element with no text of its own. This carries the same words,
  //  visually-hidden, so it is read regardless.
  const statusText = document.getElementById('listener-status-text');

  let poll = null;

  function updateListenerStatus() {
    fetch('/api/listener-status')
      .then(r => {
        if (r.status === 401) {
          // Session expired: stop polling, don't just hide the pill. The
          // throw below lands in the catch, and with nothing stopping the
          // poll this kept hitting the server every 10s for the life of
          // the tab - the exact leak dashboard-page.js's now-playing poll
          // already fixed. A background poll shouldn't yank the page away
          // mid-read, so it stops rather than navigates - the next click on
          // anything goes through the normal login redirect. stop() is
          // permanent, so the next tab switch does not restart it.
          if (statusPill) statusPill.style.display = 'none';
          if (poll) poll.stop();
          throw new Error('Not logged in');
        }
        return r.json();
      })
      .then(data => {
        if (!data || !data.status || !statusPill) return;

        const status = data.status.toUpperCase();
        //< the pill's title and the visually-hidden span carry this SAME
        //  string, from the one place it is built, so the two can never drift
        const label = `Sync Status: ${status.charAt(0) + status.slice(1).toLowerCase()}`;
        statusPill.className = `status-pill status-${status.toLowerCase()}`;
        statusPill.title = label;
        if (statusText) statusText.textContent = label;
        statusPill.style.display = 'inline-block';
      })
      .catch(() => {});
  }

  poll = window.VisibilityPoll.start(updateListenerStatus, LISTENER_STATUS_POLL_MS);
})();


(function(){
  const navToggle = document.getElementById('nav-toggle');
  const navMenu = document.getElementById('nav-menu');

  if (!navToggle || !navMenu) return;

  const topbar = document.querySelector('.topbar');
  // Locks the page under the open drawer. The rule is scoped inside the
  // <=1024px media query, so opening at 900px and then resizing past the
  // breakpoint cannot leave the page unscrollable with no drawer to close.
  const NAV_OPEN_CLASS = 'nav-open';
  // The drawer starts where the topbar ends and runs to the bottom of the
  // viewport, so its height is (viewport - topbar). .topbar is flex-wrap and
  // grows a second row for any of the four badges (share request, accepted
  // share, milestone, Spotify reauth), so that height is not a constant: the
  // literal this replaces said 62px, --topbar-height says 61px, and with a
  // badge showing neither was right.
  const TOPBAR_HEIGHT_VAR = '--topbar-current-height';

  function publishTopbarHeight() {
    if (!topbar) return;   //< nothing to measure, and the CSS fallback covers it
    // clientHeight, NOT offsetHeight: the drawer is placed with `top: 100%`,
    // and a percentage top resolves against the containing block's PADDING
    // box. .topbar has a 1px bottom border, so the border box is one pixel
    // taller than where the drawer actually starts - publishing it left a 1px
    // strip of page showing under the drawer at the bottom of the viewport.
    document.documentElement.style.setProperty(TOPBAR_HEIGHT_VAR, topbar.clientHeight + 'px');
  }

  // Both class flips are purely visual, so the button has to say what it did
  // (see the aria-expanded note in layout.html). Driven off navMenu's own
  // class rather than a counter, so every path that closes the menu - link,
  // Escape, tap-outside - agrees on the state it left behind.
  function syncNavExpanded() {
    navToggle.setAttribute('aria-expanded', String(navMenu.classList.contains('active')));
  }

  function isNavOpen() {
    return navMenu.classList.contains('active');
  }

  function setNavOpen(open) {
    const flip = open ? 'add' : 'remove';
    navMenu.classList[flip]('active');
    navToggle.classList[flip]('active');
    document.body.classList[flip](NAV_OPEN_CLASS);
    //< re-measured on open rather than only at load: a badge can appear on a
    //  page the drawer was already installed on
    if (open) publishTopbarHeight();
    syncNavExpanded();
  }

  publishTopbarHeight();
  window.addEventListener('resize', publishTopbarHeight);

  navToggle.addEventListener('click', () => setNavOpen(!isNavOpen()));

  navMenu.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => setNavOpen(false));
  });

  // The drawer is opaque and covers the whole page below the topbar, so
  // without these two the only way out is the burger itself.
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !isNavOpen()) return;
    setNavOpen(false);
    navToggle.focus();   //< focus would otherwise be left inside a hidden panel
  });

  // "Outside" is the strip of topbar still visible above the drawer - small,
  // but it is the only region left, and the click that OPENED the drawer
  // bubbles here too, which is what the navToggle check is for.
  document.addEventListener('click', (event) => {
    if (!isNavOpen()) return;
    if (navMenu.contains(event.target) || navToggle.contains(event.target)) return;
    setNavOpen(false);
  });
})();

// Nav dropdown state, for screen readers. The menus are opened by CSS alone -
// .nav-item-dropdown:hover and :focus-within in style.css - so nothing here
// controls them, and aria-expanded is DERIVED from what the stylesheet resolved
// to rather than tracked from the events. That is the only way it stays true
// under all three of the rules that can open one: hover, focus-within, and the
// <=1024px block that flattens the whole menu open with display: block
// !important.
//
// A static aria-expanded="false" in the template would be worse than having no
// attribute at all - it announces "collapsed" for the life of the page,
// including while the menu is open, and on mobile where it never closes.
//
// Escape adds a class the stylesheet uses to override :focus-within, so the
// menu closes with focus left where it was; blurring the trigger instead would
// drop the user at the top of the tab order. The dismissal is one-shot, cleared
// as soon as focus or the pointer moves, so the next Tab or hover opens it
// again.
const DROPDOWN_DISMISSED_CLASS = 'dropdown-dismissed';

(function () {
  const containers = document.querySelectorAll('.nav-item-dropdown');
  if (!containers || !containers.length) return;

  containers.forEach((container) => {
    const trigger = container.querySelector('.dropdown-trigger');
    const content = container.querySelector('.dropdown-content');
    if (!trigger || !content) return;   //< skip it rather than throw past the rest

    const sync = () => {
      const open = window.getComputedStyle(content).display !== 'none';
      trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    };

    // One handler for arriving and leaving alike: both end any dismissal, and
    // both have to re-read the state. focusin is what re-syncs when focus moves
    // BETWEEN the trigger and a link inside it - focusout fires first and would
    // otherwise leave the menu announced as closed while it is still open.
    const visit = () => {
      container.classList.remove(DROPDOWN_DISMISSED_CLASS);
      sync();
    };
    container.addEventListener('mouseenter', visit);
    container.addEventListener('mouseleave', visit);
    container.addEventListener('focusin', visit);
    container.addEventListener('focusout', visit);

    container.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      container.classList.add(DROPDOWN_DISMISSED_CLASS);
      sync();
    });

    sync();
  });
})();

// The track-cover fade-in and the scroll-to-top progress ring live in
// chrome-common.js: they're byte-identical on the public (share-link)
// layout, and the two copies are exactly the fix-lands-in-one hazard.
