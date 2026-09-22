// Plain-node unit test for the shared page chrome (static/js/layout-chrome.js).
// Run with: node tests/test_layout_chrome.js
//
// This file is loaded by EVERY authenticated page and nothing executed it. Its
// four window exports are called only from inline on*= attributes in templates,
// so no import graph reaches them either - a typo in the clamp below would land
// on /history, /top-songs, /top-artists, /top-albums and /charts at once and
// show up as "the page number box does nothing".
//
// Two of the behaviours here are load-bearing beyond correctness:
//   * handleJumpToPageKeydown CLAMPS to [1, totalPages]. Unclamped it hands the
//     server a page nobody can be on - see routes/charts.py's _movementPage for
//     what that costs on the endpoint that cannot re-derive a row count.
//   * the listener-status poll CLEARS ITS INTERVAL on 401. Without that it kept
//     hitting the server every 10s for the life of the tab; the pill hiding is
//     the visible part, the clearInterval is the fix.
//
// The stub is hand-rolled (no jsdom): these functions touch getElementById,
// classList and location, and nothing else. setInterval/setTimeout MUST be
// stubbed rather than left to node - the script arms a 10s and a 15min interval
// at load, which would otherwise hold the process open and hang the suite.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'layout-chrome.js');
//< layout.html loads this first and the polls below read it off window at load,
//  so the stub has to reproduce that order (tests/test_poll_visibility.py pins
//  the template half)
const VISIBILITY_POLL = path.join(__dirname, '..', 'static', 'js', 'visibility-poll.js');

function makeElement() {
  const classes = new Set();
  return {
    style: {},
    className: '',
    title: '',
    textContent: '',
    attributes: {},
    links: [],
    //< what the drawer's height is measured from; a badge wrapping the topbar
    //  to a second row is exactly the case the old 62px literal got wrong.
    //  clientHeight is the PADDING box - see the topbar-border test below for
    //  why the drawer needs that one and not offsetHeight.
    clientHeight: 0,
    offsetHeight: 0,
    focused: 0,
    classList: {
      add(name) { classes.add(name); },
      remove(name) { classes.delete(name); },
      toggle(name) { if (classes.has(name)) { classes.delete(name); } else { classes.add(name); } },
      contains(name) { return classes.has(name); },
    },
    //< what getComputedStyle answers below - the display the STYLESHEET
    //  resolved to, which is the only thing the dropdown sync may read (see
    //  layout-chrome.js: :hover, :focus-within and the mobile !important all
    //  land here and none of them are visible as an inline style)
    computedDisplay: 'none',
    children: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; },
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    querySelector(selector) { return this.children[selector] || null; },
    querySelectorAll() { return this.links; },
    contains(node) { return node === this || this.links.indexOf(node) !== -1; },
    focus() { this.focused += 1; },
  };
}

// Loads the script fresh against a new stub. Fresh because the module cache
// would otherwise replay the first load's side effects - and this script's
// side effects ARE most of what is under test.
function loadChrome(options) {
  options = options || {};
  const calls = { intervals: [], timeouts: [], clearedTimeouts: [], clearedIntervals: [],
                  fetched: [], listeners: {}, windowListeners: {}, cssVars: {} };
  const elements = options.elements || {};
  const selectors = options.selectors || {};
  const body = makeElement();

  global.window = {
    location: { pathname: '/history', search: options.search || '' },
    clearTimeout(id) { calls.clearedTimeouts.push(id); },
    setTimeout(fn, ms) { calls.timeouts.push({ fn, ms }); return calls.timeouts.length; },
    addEventListener(type, fn) { calls.windowListeners[type] = fn; },
    getComputedStyle(element) { return { display: element.computedDisplay }; },
  };
  global.document = {
    hidden: !!options.hidden,
    body: body,
    documentElement: { style: { setProperty(name, value) { calls.cssVars[name] = value; } } },
    getElementById(id) { return elements[id] || null; },
    querySelector(selector) { return selectors[selector] || null; },
    querySelectorAll(selector) { return (options.selectorsAll || {})[selector] || []; },
    addEventListener(type, fn) { calls.listeners[type] = fn; },
  };
  calls.body = body;
  global.setInterval = function (fn, ms) { calls.intervals.push({ fn, ms }); return calls.intervals.length; };
  global.clearInterval = function (id) { calls.clearedIntervals.push(id); };
  global.fetch = function (url) {
    calls.fetched.push(url);
    const responder = (options.responses || {})[url];
    return responder ? responder() : Promise.reject(new Error('no stub for ' + url));
  };

  delete require.cache[require.resolve(VISIBILITY_POLL)];
  require(VISIBILITY_POLL);
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  //< what the browser does on a tab switch: flip the flag, then fire the event
  calls.setHidden = function (hidden) {
    global.document.hidden = hidden;
    calls.listeners.visibilitychange();
  };
  return calls;
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// -------------------------------------------------------------- jump to page

run('a key that is not Enter leaves the page alone', () => {
  const chrome = loadChrome({});
  let handled = 0;
  chrome.window.__paginationAjaxHandler = () => { handled += 1; };

  chrome.window.handleJumpToPageKeydown({ key: 'a', target: { value: '3' }, preventDefault() {} }, 10);

  assert.strictEqual(handled, 0);
});

run('a page number above the last one is clamped to the last one', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '999' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [7]);
});

run('a page number below one is clamped up to one', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '-4' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [1]);
});

run('a page number inside the range is passed through untouched', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '4' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [4]);
});

run('a box with nothing parseable in it asks for no page at all', () => {
  const chrome = loadChrome({});
  const asked = [];
  chrome.window.__paginationAjaxHandler = (page) => asked.push(page);

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: 'abc' }, preventDefault() {} }, 7);

  assert.deepStrictEqual(asked, [], 'NaN must not reach the handler as a page');
});

run('a page with no ajax handler navigates, keeping the other query params', () => {
  const chrome = loadChrome({ search: '?q=wu+tang&interval=last-30-days' });
  //< no __paginationAjaxHandler: every page except the htmx-driven ones

  chrome.window.handleJumpToPageKeydown({ key: 'Enter', target: { value: '3' }, preventDefault() {} }, 7);

  const url = new URL(chrome.window.location, 'http://localhost');
  assert.strictEqual(url.pathname, '/history');
  assert.strictEqual(url.searchParams.get('page'), '3');
  assert.strictEqual(url.searchParams.get('q'), 'wu tang', 'the existing filter survives the jump');
  assert.strictEqual(url.searchParams.get('interval'), 'last-30-days');
});

run('Enter is swallowed so the surrounding form does not also submit', () => {
  const chrome = loadChrome({});
  let prevented = 0;
  chrome.window.__paginationAjaxHandler = () => {};

  chrome.window.handleJumpToPageKeydown(
    { key: 'Enter', target: { value: '2' }, preventDefault() { prevented += 1; } }, 7);

  assert.strictEqual(prevented, 1);
});

// ------------------------------------------------------------ version badge

run('the badge appears only when a newer release exists', async () => {
  const badge = makeElement();
  const text = makeElement();
  const chrome = loadChrome({
    elements: { 'version-badge': badge, 'version-badge-text': text },
    responses: { '/version_status': () => Promise.resolve({ json: () => Promise.resolve({ current: '1.46.5', latest: '1.47.0' }) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(badge.style.display, 'inline-flex');
  assert.ok(text.textContent.includes('1.47.0'), text.textContent);
  assert.ok(text.textContent.includes('1.46.5'), 'it also says which one you are on');
  //< `includes`, not equality: loading this file also arms the listener-status
  //  poll, which fetches on its own before the first interval tick
  assert.ok(chrome.fetched.includes('/version_status'));
});

run('an up-to-date instance shows no badge', async () => {
  const badge = makeElement();
  const text = makeElement();
  loadChrome({
    elements: { 'version-badge': badge, 'version-badge-text': text },
    responses: { '/version_status': () => Promise.resolve({ json: () => Promise.resolve({ current: '1.46.5', latest: null }) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(badge.style.display, 'none');
});

// ----------------------------------------------------- listener status pill

function pillResponses(status) {
  return { '/api/listener-status': () => Promise.resolve({ status: 200, json: () => Promise.resolve({ status }) }) };
}

run('the sync pill reports the listener state', async () => {
  const pill = makeElement();
  loadChrome({ elements: { 'listener-status-pill': pill }, responses: pillResponses('active') });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.className, 'status-pill status-active');
  assert.strictEqual(pill.title, 'Sync Status: Active');
  assert.strictEqual(pill.style.display, 'inline-block');
});

// UI-07 (2026-09-02 review): the pill's state was colour (className) and a
// hover-only title - nothing a screen reader could read without hovering a
// span with no text of its own. #listener-status-text carries the same words
// as the title, always on screen for assistive tech via .visually-hidden.
run('the pill also carries its state as text, matching the title word for word', async () => {
  const pill = makeElement();
  const text = makeElement();
  loadChrome({
    elements: { 'listener-status-pill': pill, 'listener-status-text': text },
    responses: pillResponses('degraded'),
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(text.textContent, 'Sync Status: Degraded');
  assert.strictEqual(text.textContent, pill.title, 'the two must never be able to drift apart');
});

run('a page without the text span does not crash the poll', async () => {
  const pill = makeElement();
  loadChrome({ elements: { 'listener-status-pill': pill }, responses: pillResponses('active') });
  await new Promise(resolve => setImmediate(resolve));   //< throws if the poll's .then() blew up

  assert.strictEqual(pill.style.display, 'inline-block', 'the rest of the update must still land');
});

run('an expired session stops the poll instead of hiding the pill and polling on', async () => {
  const pill = makeElement();
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: { '/api/listener-status': () => Promise.resolve({ status: 401, json: () => Promise.resolve({}) }) },
  });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.style.display, 'none');
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  assert.ok(listenerInterval, 'the 10s poll is armed at load');
  assert.ok(chrome.clearedIntervals.length >= 1,
            'the interval is cleared, or the tab keeps hitting the server every 10s forever');
});

run('an accepted 401 stays terminal when a newer listener response succeeds', async () => {
  const pill = makeElement();
  const statusText = makeElement();
  const pending = [];
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill, 'listener-status-text': statusText },
    responses: {
      '/api/listener-status': () => new Promise((resolve) => pending.push(resolve)),
    },
  });
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  listenerInterval.fn();

  pending[0]({ status: 401, json: () => Promise.resolve({}) });
  await new Promise(resolve => setImmediate(resolve));
  pending[1]({ status: 200, json: () => Promise.resolve({ status: 'active' }) });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.style.display, 'none');
  assert.strictEqual(pill.className, '');
  assert.strictEqual(pill.title, '');
  assert.strictEqual(statusText.textContent, '');
  assert.strictEqual(chrome.clearedIntervals.length, 1);
});

run('a late listener failure leaves accepted session expiry terminal', async () => {
  const pill = makeElement();
  const pending = [];
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: {
      '/api/listener-status': () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
    },
  });
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  listenerInterval.fn();
  pending[0].resolve({ status: 401, json: () => Promise.resolve({}) });
  await new Promise(resolve => setImmediate(resolve));
  pending[1].reject(new Error('late failure'));
  await new Promise(resolve => setImmediate(resolve));
  listenerInterval.fn();

  assert.strictEqual(pill.style.display, 'none');
  assert.strictEqual(pill.className, '');
  assert.strictEqual(chrome.clearedIntervals.length, 1);
  assert.strictEqual(pending.length, 2);
});

run('a deferred listener body cannot revive the pill after a 401', async () => {
  const pill = makeElement();
  const pending = [];
  let releaseBody;
  const body = new Promise((resolve) => { releaseBody = resolve; });
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: {
      '/api/listener-status': () => new Promise((resolve) => pending.push(resolve)),
    },
  });
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  pending[0]({ status: 200, json: () => body });
  await new Promise(resolve => setImmediate(resolve));
  listenerInterval.fn();
  pending[1]({ status: 401, json: () => Promise.resolve({}) });
  await new Promise(resolve => setImmediate(resolve));

  releaseBody({ status: 'active' });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(pill.style.display, 'none');
  assert.strictEqual(pill.className, '');
  assert.strictEqual(chrome.clearedIntervals.length, 1);
});

run('a repeated 401 does not clear the listener interval twice', async () => {
  const pill = makeElement();
  const pending = [];
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: {
      '/api/listener-status': () => new Promise((resolve) => pending.push(resolve)),
    },
  });
  const listenerInterval = chrome.intervals.find(i => i.ms === 10 * 1000);
  listenerInterval.fn();
  pending[0]({ status: 401, json: () => Promise.resolve({}) });
  await new Promise(resolve => setImmediate(resolve));
  pending[1]({ status: 401, json: () => Promise.resolve({}) });
  await new Promise(resolve => setImmediate(resolve));

  assert.strictEqual(chrome.clearedIntervals.length, 1);
  assert.strictEqual(pill.style.display, 'none');
});

run('a hidden tab stops asking for the pill', async () => {
  // Every authenticated page arms this, so a browser sitting on any of them
  // overnight used to hit /api/listener-status every 10s until it was closed.
  const pill = makeElement();
  const chrome = loadChrome({ elements: { 'listener-status-pill': pill }, responses: pillResponses('active') });
  await new Promise(resolve => setImmediate(resolve));

  chrome.setHidden(true);

  assert.ok(chrome.clearedIntervals.length >= 1, 'the 10s poll keeps running in a background tab');
});

run('coming back to the tab asks again straight away', async () => {
  const pill = makeElement();
  const chrome = loadChrome({ elements: { 'listener-status-pill': pill }, responses: pillResponses('active') });
  await new Promise(resolve => setImmediate(resolve));
  const before = chrome.fetched.filter(url => url === '/api/listener-status').length;

  chrome.setHidden(true);
  chrome.setHidden(false);

  assert.strictEqual(chrome.fetched.filter(url => url === '/api/listener-status').length, before + 1,
                     'a returning tab shows a pill that is up to 10s stale otherwise');
});

run('an expired session stays stopped across a tab switch', async () => {
  // The 401 branch exists so an expired session stops hitting the server;
  // restarting it on the next tab switch would put that straight back.
  const pill = makeElement();
  const chrome = loadChrome({
    elements: { 'listener-status-pill': pill },
    responses: { '/api/listener-status': () => Promise.resolve({ status: 401, json: () => Promise.resolve({}) }) },
  });
  await new Promise(resolve => setImmediate(resolve));
  const after401 = chrome.fetched.filter(url => url === '/api/listener-status').length;

  chrome.setHidden(true);
  chrome.setHidden(false);

  assert.strictEqual(chrome.fetched.filter(url => url === '/api/listener-status').length, after401);
});

// ------------------------------------------------------------- nav menu

run('the burger reports its state to screen readers, both ways', () => {
  const navToggle = makeElement();
  const navMenu = makeElement();
  loadChrome({ elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu } });

  navToggle.handlers.click();
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'true');

  navToggle.handlers.click();
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'false');
});

run('following a link closes the menu and says so', () => {
  const navToggle = makeElement();
  const navMenu = makeElement();
  const link = makeElement();
  navMenu.links = [link];
  loadChrome({ elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu } });
  navToggle.handlers.click();   //< open it first

  link.handlers.click();

  assert.strictEqual(navMenu.classList.contains('active'), false);
  assert.strictEqual(navToggle.attributes['aria-expanded'], 'false');
});

// The drawer covers the whole page below the topbar and is opaque, so the ways
// out of it are the only ways out: the burger (now an X), a link, Escape, and
// the strip of topbar still visible above it.

function makeNav(options) {
  options = options || {};
  const navToggle = makeElement();
  const navMenu = makeElement();
  const topbar = makeElement();
  topbar.clientHeight = options.topbarHeight || 52;
  //< the border box, always >= clientHeight: .topbar has a 1px bottom border
  topbar.offsetHeight = (options.topbarHeight || 52) + (options.topbarBorder || 0);
  const chrome = loadChrome({
    elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu },
    selectors: { '.topbar': topbar },
  });
  return { navToggle, navMenu, topbar, chrome };
}

run('Escape closes the open drawer and hands focus back to the burger', () => {
  const nav = makeNav();
  nav.navToggle.handlers.click();

  nav.chrome.listeners.keydown({ key: 'Escape' });

  assert.strictEqual(nav.navMenu.classList.contains('active'), false);
  assert.strictEqual(nav.navToggle.attributes['aria-expanded'], 'false');
  //< without this, focus is left on a link inside a hidden panel
  assert.strictEqual(nav.navToggle.focused, 1);
});

run('Escape on a closed drawer does not steal focus', () => {
  const nav = makeNav();

  nav.chrome.listeners.keydown({ key: 'Escape' });

  assert.strictEqual(nav.navToggle.focused, 0);
});

run('a key that is not Escape leaves the open drawer alone', () => {
  const nav = makeNav();
  nav.navToggle.handlers.click();

  nav.chrome.listeners.keydown({ key: 'e' });

  assert.strictEqual(nav.navMenu.classList.contains('active'), true);
});

run('a tap on what is left of the topbar closes the drawer', () => {
  const nav = makeNav();
  nav.navToggle.handlers.click();

  nav.chrome.listeners.click({ target: makeElement() });

  assert.strictEqual(nav.navMenu.classList.contains('active'), false);
});

run('a tap inside the drawer leaves it open', () => {
  const nav = makeNav();
  const link = makeElement();
  nav.navMenu.links = [link];
  nav.navToggle.handlers.click();

  nav.chrome.listeners.click({ target: link });

  assert.strictEqual(nav.navMenu.classList.contains('active'), true);
});

run('the click that opened the drawer does not immediately close it', () => {
  const nav = makeNav();
  nav.navToggle.handlers.click();

  //< the same click bubbles to document; the burger must count as "inside"
  nav.chrome.listeners.click({ target: nav.navToggle });

  assert.strictEqual(nav.navMenu.classList.contains('active'), true);
});

run('opening the drawer locks the page behind it and closing releases it', () => {
  const nav = makeNav();

  nav.navToggle.handlers.click();
  assert.strictEqual(nav.chrome.body.classList.contains('nav-open'), true);

  nav.navToggle.handlers.click();
  assert.strictEqual(nav.chrome.body.classList.contains('nav-open'), false);
});

run('the drawer is measured off the real topbar, not a constant', () => {
  //< 88px is the topbar with a badge wrapped onto a second row - the case the
  //  62px literal it replaces got wrong, and --topbar-height (52px) too
  const nav = makeNav({ topbarHeight: 88 });

  assert.strictEqual(nav.chrome.cssVars['--topbar-current-height'], '88px');
});

run('a resize re-measures the topbar', () => {
  const nav = makeNav({ topbarHeight: 52 });

  nav.topbar.clientHeight = 88;   //< rotating to landscape unwraps the badge row
  nav.chrome.windowListeners.resize();

  assert.strictEqual(nav.chrome.cssVars['--topbar-current-height'], '88px');
});

run('the measurement excludes the topbar border, which the drawer sits inside of', () => {
  // The drawer is positioned with `top: 100%`, and a percentage top resolves
  // against the containing block's PADDING box. .topbar has a 1px bottom
  // border, so publishing offsetHeight (the border box) made the drawer start
  // 1px high and stop 1px short of the viewport bottom - measured at 375x812,
  // where the bar was 177px/176px and the drawer ended at 811 of 812.
  const nav = makeNav({ topbarHeight: 176, topbarBorder: 1 });

  assert.strictEqual(nav.topbar.offsetHeight, 177);   //< the value NOT to publish
  assert.strictEqual(nav.chrome.cssVars['--topbar-current-height'], '176px');
});

run('opening the drawer re-measures before it is shown', () => {
  // The safety net: the var is otherwise only written at load and on resize,
  // so any layout change in between (a badge arriving) would open a drawer
  // sized to the old bar.
  const nav = makeNav({ topbarHeight: 52 });
  nav.topbar.clientHeight = 177;   //< a badge wrapped the bar since load

  nav.navToggle.handlers.click();

  assert.strictEqual(nav.chrome.cssVars['--topbar-current-height'], '177px');
});

run('a page without a topbar still opens its menu', () => {
  //< the public share layout has no .topbar-scoped burger at all; guard the
  //  measurement rather than throwing past the rest of the handler
  const navToggle = makeElement();
  const navMenu = makeElement();
  loadChrome({ elements: { 'nav-toggle': navToggle, 'nav-menu': navMenu } });

  navToggle.handlers.click();

  assert.strictEqual(navMenu.classList.contains('active'), true);
});

// ------------------------------------------------------- nav dropdown a11y

// The dropdowns are opened by CSS alone - :hover and :focus-within - so
// aria-expanded has to be DERIVED from what the stylesheet resolved to. A
// static aria-expanded="false" in the template is worse than none at all: it
// announces "collapsed" for the whole life of the page, including while the
// menu is open.
function makeDropdown(display) {
  const container = makeElement();
  const trigger = makeElement();
  const content = makeElement();
  content.computedDisplay = display || 'none';
  container.children['.dropdown-trigger'] = trigger;
  container.children['.dropdown-content'] = content;
  return { container, trigger, content };
}

function loadWithDropdowns(dropdowns) {
  return loadChrome({
    selectorsAll: { '.nav-item-dropdown': dropdowns.map((d) => d.container) },
  });
}

run('a closed dropdown is announced as collapsed at load', () => {
  const d = makeDropdown('none');
  loadWithDropdowns([d]);

  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'false');
});

run('a dropdown the stylesheet has opened is announced as expanded', () => {
  const d = makeDropdown('none');
  loadWithDropdowns([d]);

  //< what :focus-within does when Tab reaches the trigger
  d.content.computedDisplay = 'block';
  d.container.handlers.focusin();

  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'true');
});

run('the flattened mobile menu is never announced as collapsed', () => {
  //< at <=1024px the stylesheet flattens the menu with display: block
  //  !important and the trigger becomes an inert section label. Deriving the
  //  state is what keeps that honest; assuming "closed until focused" would
  //  announce every one of those permanently-open sections as collapsed.
  const d = makeDropdown('block');
  loadWithDropdowns([d]);

  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'true');
});

run('Escape dismisses an open dropdown without moving focus', () => {
  const d = makeDropdown('block');
  loadWithDropdowns([d]);

  //< the CSS class is what closes it, so the stub follows the stylesheet
  d.container.handlers.keydown({ key: 'Escape' });
  d.content.computedDisplay = d.container.classList.contains('dropdown-dismissed') ? 'none' : 'block';
  d.container.handlers.keydown({ key: 'Escape' });

  assert.strictEqual(d.container.classList.contains('dropdown-dismissed'), true);
  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'false');
  assert.strictEqual(d.trigger.focused, 0, 'blurring would drop the user at the top of the tab order');
});

run('a key that is not Escape leaves the dropdown open', () => {
  const d = makeDropdown('block');
  loadWithDropdowns([d]);

  d.container.handlers.keydown({ key: 'ArrowDown' });

  assert.strictEqual(d.container.classList.contains('dropdown-dismissed'), false);
  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'true');
});

run('leaving and returning clears a dismissal, so the menu opens again', () => {
  const d = makeDropdown('block');
  loadWithDropdowns([d]);
  d.container.handlers.keydown({ key: 'Escape' });

  d.container.handlers.mouseenter();

  assert.strictEqual(d.container.classList.contains('dropdown-dismissed'), false,
                     'a one-shot dismissal must not outlive the visit that made it');
  assert.strictEqual(d.trigger.getAttribute('aria-expanded'), 'true');
});

run('every dropdown on the page gets its own state', () => {
  const open = makeDropdown('block');
  const closed = makeDropdown('none');
  loadWithDropdowns([open, closed]);

  assert.strictEqual(open.trigger.getAttribute('aria-expanded'), 'true');
  assert.strictEqual(closed.trigger.getAttribute('aria-expanded'), 'false');
});

run('a malformed dropdown is skipped rather than throwing past the rest', () => {
  const broken = { container: makeElement() };   //< no trigger, no content
  const good = makeDropdown('block');
  loadWithDropdowns([broken, good]);

  assert.strictEqual(good.trigger.getAttribute('aria-expanded'), 'true');
});

run('a page with no dropdowns at all is a no-op', () => {
  //< the public share layout has no nav dropdowns
  assert.doesNotThrow(() => loadChrome({}));
});

(async () => {
  for (const { name, fn } of results) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      console.error(`FAIL - ${name}`);
      console.error(err);
      process.exit(1);
    }
  }
  console.log(`all ${results.length} layout-chrome tests passed`);
})();
