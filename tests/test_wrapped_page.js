// Plain-node unit test for the /wrapped browser logic (static/js/wrapped.js).
// Run with: node tests/test_wrapped_page.js
//
// The largest of the untested browser scripts, and the one whose own comments
// record the most already-paid-for bugs. Three of them are pinned here:
//
//   * loadChartData deliberately sets NO `interval` key. charts.js reads that as
//     "these buckets are hours" and splits each label on a space; Wrapped's day
//     buckets are whole dates, so the old loader's `interval = groupBy` turned
//     every x-axis label and tooltip into "undefined" the moment someone chose
//     Trend buckets = Day.
//   * applyStatsFilter falls back to All when the chosen category is not in the
//     year just loaded. A category with nothing in it is hidden server-side, so
//     switching years can take it away underneath the user - and without the
//     fallback they get a blank page.
//   * the remembered filter survives a swap. The server re-renders the nav and
//     has no idea which category is open (it is not in the URL), so without the
//     module-level `activeStatsFilter` a sort change bounces the user to All.
//
// eslint.config.js names wrapped.js as one of the two files that shipped a
// ReferenceError to production. That is the floor this file is raising.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'wrapped.js');

function makeClassList(initial) {
  const classes = new Set(initial || []);
  return {
    classes,
    contains: (n) => classes.has(n),
    add: (n) => classes.add(n),
    remove: (n) => classes.delete(n),
    toggle(n, force) { if (force) { classes.add(n); } else { classes.delete(n); } },
  };
}

function makeElement(extra) {
  return Object.assign({
    id: '', style: {}, value: '', textContent: '', innerHTML: null, className: '',
    dataset: {}, classList: makeClassList(), attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    closest() { return null; },
    matches() { return false; },
    querySelector() { return null; },
    prepend(node) { (this.prepended = this.prepended || []).push(node); },
    focus() { this.focused = (this.focused || 0) + 1; },
  }, extra || {});
}

function filterButton(name, hidden) {
  return makeElement({ dataset: { filter: name }, style: { display: hidden ? 'none' : '' } });
}

function categoryDiv(name) {
  return makeElement({ dataset: { category: name } });
}

// Everything the PNG export touches. Asserting each fillText would pin the
// layout, not the behaviour - what matters is that it picks the theme's accent
// and hands the browser a download with the right filename.
function makeCanvasContext(record) {
  const noop = () => {};
  return {
    createLinearGradient() { return { addColorStop(stop, colour) { record.gradient.push(colour); } }; },
    fillRect: noop, strokeRect: noop, beginPath: noop, arc: noop, fill: noop, stroke: noop,
    moveTo: noop, lineTo: noop, closePath: noop, save: noop, restore: noop, translate: noop,
    measureText() { return { width: 10 }; },
    fillText(text) { record.texts.push(text); },
    set fillStyle(v) { record.fills.push(v); },
    get fillStyle() { return ''; },
    set strokeStyle(v) { record.strokes.push(v); },
    get strokeStyle() { return ''; },
    font: '', textAlign: '', lineWidth: 0,
  };
}

function loadWrapped(options) {
  options = options || {};
  const calls = {
    bodyListeners: {}, docListeners: {}, pruned: [], ajax: [], fetched: [],
    created: [], canvas: { gradient: [], texts: [], fills: [], strokes: [] },
    charts: 0, swapFailure: null, downloads: [], confirms: [],
  };
  const elements = options.elements || {};
  const buttons = options.buttons || [];
  const categories = options.categories || [];

  global.window = {
    location: { pathname: '/wrapped', search: options.search || '', href: 'http://localhost/wrapped' },
    //< the Revoke confirm: every message asked is recorded, and the answer
    //  is the test's (yes unless it says otherwise)
    confirm(message) { calls.confirms.push(message); return options.confirmAnswer !== false; },
    AjaxStatus: options.noAjaxStatus ? undefined : {
      redirectIfUnauthorized(response) { return response.status === 401; },
    },
  };
  global.document = {
    documentElement: { className: options.theme || 'theme-rose' },
    title: options.title || '2026 Wrapped - SpotifyTracker',
    addEventListener(type, fn) { calls.docListeners[type] = fn; },
    //< multiple listeners for one event type, like the real DOM allows -
    //  wrapped.js registers more than one htmx listener per event, and a
    //  single-slot stub would silently drop all but the last
    body: {
      addEventListener(type, fn) {
        (calls.bodyListeners[type] = calls.bodyListeners[type] || []).push(fn);
      },
    },
    getElementById(id) { return elements[id] || null; },
    querySelector(selector) {
      const match = /\.stats-filter-button\[data-filter="(.*)"\]/.exec(selector);
      if (match) return buttons.find(b => b.dataset.filter === match[1]) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '.stats-filter-button') return buttons;
      if (selector === '[data-category]') return categories;
      return [];
    },
    createElement(tag) {
      const node = tag === 'canvas'
        ? {
            tag, width: 0, height: 0,
            getContext() { return makeCanvasContext(calls.canvas); },
            toDataURL() { return 'data:image/png;base64,STUB'; },
          }
        : Object.assign(makeElement(), { tag, click() { calls.downloads.push({ name: this.download, href: this.href }); } });
      calls.created.push(node);
      return node;
    },
  };
  global.renderTimeSeriesChart = function () { calls.charts += 1; };
  global.HtmxFilters = {
    pruneEmptyParams(parameters) { calls.pruned.push(parameters); },
    onSwapFailure(targetId, retry) { calls.swapFailure = { targetId, retry }; },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.FormData = function FormDataStub(form) { this.form = form; };
  global.fetch = function (url, init) {
    calls.fetched.push({ url: String(url), init });
    return options.respond ? options.respond() : Promise.reject(new Error('no stub'));
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  calls.buttons = buttons;
  calls.categories = categories;
  return calls;
}

// document.body can carry more than one listener per event type - fire every
// one of them, the way the real DOM would.
function fireBody(page, type, evt) {
  (page.bodyListeners[type] || []).forEach((fn) => fn(evt));
}

function clickOn(page, node) {
  fireBody(page, 'click', { target: { closest: (sel) => (node.selectors || []).includes(sel) ? node : null } });
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------- chart data

run('the bootstrap island becomes the chart data at parse time', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{"buckets":["2026-01-01"]}}' }) },
  });

  assert.deepStrictEqual(page.window.__chartData, { timeSeries: { buckets: ['2026-01-01'] } });
});

run('the chart data carries no interval key, or every label reads "undefined"', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{},"groupBy":"day"}' }) },
  });

  assert.deepStrictEqual(Object.keys(page.window.__chartData), ['timeSeries'],
                         'charts.js reads `interval` as "these buckets are hours"');
});

run('a page with no island leaves the chart data alone', () => {
  const page = loadWrapped({});

  assert.strictEqual(page.window.__chartData, undefined);
});

// -------------------------------------------------------- the stats filter

function filterSetup(options) {
  const all = filterButton('all');
  const songs = filterButton('songs');
  const podcasts = filterButton('podcasts', options && options.podcastsHidden);
  const songSection = categoryDiv('songs');
  const podcastSection = categoryDiv('podcasts');
  const page = loadWrapped(Object.assign({
    buttons: [all, songs, podcasts],
    categories: [songSection, podcastSection],
  }, options || {}));
  return { page, all, songs, podcasts, songSection, podcastSection };
}

run('All Stats shows every category on load', () => {
  const dom = filterSetup();

  assert.strictEqual(dom.songSection.classList.contains('visible'), true);
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), true);
  assert.strictEqual(dom.all.classList.contains('active'), true);
  //< the state a screen reader hears - written beside the class, every time
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'false');
});

run('choosing a category shows only it', () => {
  const dom = filterSetup();
  dom.songs.selectors = ['.stats-filter-button'];

  clickOn(dom.page, dom.songs);

  assert.strictEqual(dom.songSection.classList.contains('visible'), true);
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), false);
  assert.strictEqual(dom.songs.classList.contains('active'), true);
  assert.strictEqual(dom.all.classList.contains('active'), false);
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'false');
});

run('a category the new year does not have falls back to All, not a blank page', () => {
  const dom = filterSetup({ podcastsHidden: true });
  dom.podcasts.selectors = ['.stats-filter-button'];

  clickOn(dom.page, dom.podcasts);

  assert.strictEqual(dom.all.classList.contains('active'), true);
  assert.strictEqual(dom.songSection.classList.contains('visible'), true,
                     'everything shows rather than nothing');
});

run('the chosen category survives a swap, instead of bouncing back to All', () => {
  const dom = filterSetup();
  dom.songs.selectors = ['.stats-filter-button'];
  clickOn(dom.page, dom.songs);

  fireBody(dom.page, 'htmx:afterSettle', { target: { id: 'wrappedResults' } });

  assert.strictEqual(dom.songs.classList.contains('active'), true,
                     'the server has no idea which category is open - this file remembers');
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), false);
});

// ---------------------------------------------------------------- swaps

run('a swap of the recap reloads the data and redraws once', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{"buckets":[]}}' }) },
  });
  const before = page.charts;

  fireBody(page, 'htmx:afterSettle', { target: { id: 'wrappedResults' } });

  assert.strictEqual(page.charts, before + 1);
});

// The redraw is on htmx:afterSettle, NOT htmx:afterSwap. The swapped-in canvas
// keeps the id of the one it replaces, and htmx's settle step restores such an
// element's ORIGINAL attributes 20ms after the swap - so a canvas sized and
// painted in afterSwap had its width/height/style stripped again, resetting
// the bitmap to a blank 300x150. Every year and filter change after the first
// paint went invisible (2026-09-05 regression).
run('the redraw waits for settle - a canvas painted in afterSwap is wiped 20ms later', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{"buckets":[]}}' }) },
  });
  const before = page.charts;

  fireBody(page, 'htmx:afterSwap', { target: { id: 'wrappedResults' } });

  assert.strictEqual(page.charts, before,
                     'settle restores a same-id element' + "'s original attributes, " +
                     'so a width/height set before it is stripped and the bitmap goes blank');
});

run('an out-of-band region swapping does not redraw the chart again', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{}}' }) },
  });
  const before = page.charts;

  fireBody(page, 'htmx:afterSettle', { target: { id: 'shareLinkPanel' } });

  assert.strictEqual(page.charts, before, 'four OOB regions would otherwise redraw it four times');
});

// UT-7: document.title stayed on the year the page first loaded with. The
// hero and the hidden year field both swap out of band on a year switch (see
// _wrapped_hero.html / _wrapped_year_field.html) - OUTSIDE #wrappedResults -
// so this is its own htmx:afterSwap listener, not a branch of the settle one
// above (and afterSwap is fine here: it writes document.title, not an
// attribute of anything settle will touch).

run('a year switch updates document.title, keeping the server\'s suffix', () => {
  const page = loadWrapped({ title: '2026 Wrapped - SpotifyTracker' });

  fireBody(page, 'htmx:afterSwap', { target: { id: 'wrappedYearField', value: '2025' } });

  assert.strictEqual(global.document.title, '2025 Wrapped - SpotifyTracker');
});

run('a self-hosted rename of the base title still tracks, nothing hardcoded', () => {
  const page = loadWrapped({ title: '2026 Wrapped - My Own Instance' });

  fireBody(page, 'htmx:afterSwap', { target: { id: 'wrappedYearField', value: '2019' } });

  assert.strictEqual(global.document.title, '2019 Wrapped - My Own Instance');
});

run('the main results swap does not touch the title - it never carries the year field', () => {
  const page = loadWrapped({ title: '2026 Wrapped - SpotifyTracker' });

  fireBody(page, 'htmx:afterSwap', { target: { id: 'wrappedResults' } });
  fireBody(page, 'htmx:afterSettle', { target: { id: 'wrappedResults' } });

  assert.strictEqual(global.document.title, '2026 Wrapped - SpotifyTracker');
});

run('the wrapped form prunes its empty params, and nothing else does', () => {
  const page = loadWrapped({});
  const mine = { groupBy: '' };
  const theirs = { groupBy: '' };

  fireBody(page, 'htmx:configRequest', { detail: { elt: { id: 'wrappedFilters' }, parameters: mine } });
  fireBody(page, 'htmx:configRequest', { detail: { elt: { id: 'somethingElse' }, parameters: theirs } });

  assert.deepStrictEqual(page.pruned, [mine]);
});

// X6 (2026-09-02 review): templates/wrapped.html's form always sends
// X-Wrapped-Filter-Change - routes/wrapped.py reads it as "the genre card is
// unchanged and already on screen" and answers with an hx-preserve stub
// instead of a real card. True only while #wrappedGenresCard is actually
// there. After a failed swap has emptied #wrappedResults (AjaxStatus's
// failure path), the card is gone, and vendored htmx's hx-preserve only
// preserves an id it can still find - so sending the marker on the very next
// filter change would swap the hollow stub in and the genre card would stay
// missing until the next year switch.

run('the marker header is dropped once the genre card is gone', () => {
  const page = loadWrapped({});   //< no wrappedGenresCard in elements
  const headers = { 'X-Wrapped-Filter-Change': '1' };

  fireBody(page, 'htmx:configRequest',
    { detail: { elt: { id: 'wrappedFilters' }, parameters: {}, headers: headers } });

  assert.strictEqual('X-Wrapped-Filter-Change' in headers, false);
});

run('the marker header survives while the genre card is still on screen', () => {
  const page = loadWrapped({ elements: { wrappedGenresCard: makeElement() } });
  const headers = { 'X-Wrapped-Filter-Change': '1' };

  fireBody(page, 'htmx:configRequest',
    { detail: { elt: { id: 'wrappedFilters' }, parameters: {}, headers: headers } });

  assert.strictEqual(headers['X-Wrapped-Filter-Change'], '1');
});

run('a request with no headers object at all does not crash the listener', () => {
  // htmx always populates evt.detail.headers on a real request; this pins
  // that the guard does not assume it, the way tests above already omit it
  // for the OTHER assertion this same listener makes (pruneEmptyParams).
  const page = loadWrapped({});

  assert.doesNotThrow(() => {
    fireBody(page, 'htmx:configRequest', { detail: { elt: { id: 'wrappedFilters' }, parameters: {} } });
  });
});

// ---------------------------------------------------------- the PNG export

function exportSetup(theme, streak = '2') {
  const btn = makeElement({
    dataset: {
      year: '2026', user: 'timo', topsong: 'Aruarian Dance', topartist: 'Nujabes',
      topalbum: 'Modal Soul', peakday: '2026-03-01', peakplays: '120',
      discoveredsongs: '340', discoveredartists: '58', streak,
    },
  });
  btn.selectors = ['#exportWrappedBtn'];
  const page = loadWrapped({ theme });
  clickOn(page, btn);
  return page;
}

run('the export downloads a PNG named for the user and year', () => {
  const page = exportSetup('theme-rose');

  assert.strictEqual(page.downloads.length, 1);
  assert.strictEqual(page.downloads[0].name, 'timo_2026_wrapped_summary.png');
  assert.ok(page.downloads[0].href.startsWith('data:image/png'), page.downloads[0].href);
});

run('the card is drawn in the active theme', () => {
  const page = exportSetup('theme-green');

  assert.ok(page.canvas.gradient.includes('#0b3c1d'), page.canvas.gradient.join());
  assert.ok(page.canvas.fills.includes('#1DB954'), 'the green accent, not the default rose');
});

run('the exported card uses the grammatical streak unit', () => {
  for (const [streak, unit] of [['0', 'days'], ['1', 'day'], ['2', 'days']]) {
    const page = exportSetup('theme-rose', streak);

    assert.ok(page.canvas.texts.includes(streak + ' ' + unit));
  }
});

run('an unknown theme falls back to the default rather than drawing nothing', () => {
  const page = exportSetup('theme-does-not-exist');

  assert.ok(page.canvas.gradient.includes('#3c0b1f'));
  assert.ok(page.canvas.texts.includes('2026 WRAPPED'));
});

// The playlist download that used to be tested here is the shared
// _playlist_download.html control now - its (still delegated) handler lives
// in chrome-common.js and is pinned by test_chrome_common.js instead.

// ----------------------------------------------------------- share modal

function modalSetup(options) {
  options = options || {};
  const closeBtn = makeElement();
  const modal = makeElement({
    id: 'shareLinkModal',
    querySelector(selector) { return selector === '.share-modal-close' ? closeBtn : null; },
  });
  //< ?openShareModal=1 renders the modal already open - set before loadWrapped
  //  requires the script, so wrapped.js sees it exactly as it would on load
  if (options.serverOpened) modal.style.display = 'flex';
  const openBtn = makeElement();
  const panelBody = makeElement();
  const page = loadWrapped(Object.assign({
    elements: { shareLinkModal: modal, shareWrappedBtn: openBtn, shareLinkPanelBody: panelBody },
  }, options));
  return { page, modal, openBtn, panelBody, closeBtn };
}

run('the Share button opens the modal', () => {
  const dom = modalSetup();

  dom.openBtn.handlers.click();

  assert.strictEqual(dom.modal.style.display, 'flex');
});

run('clicking the backdrop closes it, clicking inside does not', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.modal.handlers.click.call(dom.modal, { target: makeElement() });
  assert.strictEqual(dom.modal.style.display, 'flex', 'a click on the panel must not dismiss it');

  dom.modal.handlers.click.call(dom.modal, { target: dom.modal });
  assert.strictEqual(dom.modal.style.display, 'none');
});

run('Escape closes the modal', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.modal.style.display, 'none');
});

// A role="dialog" is announced by focus landing inside it; display:flex alone
// says nothing to a screen reader and left focus on the Share button behind
// the overlay. Closing has to hand it back, or Escape drops focus from the now
// display:none Close button to <body>. Same rule as layout-chrome.js's drawer.
run('opening the dialog moves focus into it', () => {
  const dom = modalSetup();

  dom.openBtn.handlers.click();

  assert.strictEqual(dom.closeBtn.focused, 1);
});

run('every close path returns focus to whatever opened the dialog', () => {
  const dom = modalSetup();
  global.document.activeElement = dom.openBtn;

  dom.openBtn.handlers.click();
  dom.page.docListeners.keydown({ key: 'Escape' });
  assert.strictEqual(dom.openBtn.focused, 1, 'Escape');

  dom.openBtn.handlers.click();
  dom.closeBtn.handlers.click();
  assert.strictEqual(dom.openBtn.focused, 2, 'the Close button');
  assert.strictEqual(dom.modal.style.display, 'none', 'the Close button no longer needs an inline handler');

  dom.openBtn.handlers.click();
  dom.modal.handlers.click.call(dom.modal, { target: dom.modal });
  assert.strictEqual(dom.openBtn.focused, 3, 'the overlay');
});

run('a dialog the server opened is focused on load', () => {
  //< ?openShareModal=1 renders it open with focus still on <body> - nothing
  //  on the page called openShareModal() to move it there
  const dom = modalSetup({ serverOpened: true });

  assert.strictEqual(dom.closeBtn.focused, 1);
});

run('a dialog the server opened returns focus to the Share button', () => {
  //< ?openShareModal=1 renders it open, so nothing on the page opened it -
  //  shareModalOpener stays null and Escape must still fall back
  const dom = modalSetup({ serverOpened: true });

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.openBtn.focused, 1);
});

run('Escape with the dialog closed leaves focus where it is', () => {
  const dom = modalSetup();

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.openBtn.focused, undefined, 'it hears every keypress on the page');
});

run('another key leaves it open', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.page.docListeners.keydown({ key: 'a' });

  assert.strictEqual(dom.modal.style.display, 'flex');
});

// ------------------------------------------------- creating a share link

function submitShareForm(dom, options) {
  const opts = options || {};
  const form = makeElement({ action: 'http://localhost/wrapped/share-links/2026' });
  //< a create form unless the test says revoke: the handler tells the two
  //  apart by class, and only one of them asks first
  const ownClass = opts.revoke ? '.share-link-revoke-form' : '.share-link-create-form';
  form.matches = (selector) => selector.split(',').some((part) => part.trim() === ownClass);
  if (opts.submitButton) form.querySelector = () => opts.submitButton;
  const evt = { target: form, prevented: 0, preventDefault() { this.prevented += 1; } };
  dom.modal.handlers.submit.call(dom.modal, evt);
  return evt;
}

// A fetch whose settlement THIS test controls, so two submits can be resolved
// in the opposite order to the one they were made in - which is the whole
// scenario and is not reproducible with an already-resolved promise.
function deferredShareScenario() {
  const settle = [];
  const dom = shareScenario(() => new Promise((resolve, reject) => settle.push({ resolve, reject })));
  return { dom, settle };
}

function shareScenario(respond) {
  return modalSetup({ respond });
}

run('creating a link posts with ajax=true and swaps the panel body in', async () => {
  const dom = shareScenario(() => Promise.resolve({ status: 200, json: () => Promise.resolve({ html: '<p>link</p>' }) }));

  const evt = submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(evt.prevented, 1, 'the browser must not submit this form normally');
  assert.ok(dom.page.fetched[0].url.includes('ajax=true'), dom.page.fetched[0].url);
  assert.strictEqual(dom.page.fetched[0].init.method, 'POST');
  assert.strictEqual(dom.panelBody.innerHTML, '<p>link</p>');
});

run('a refused create shows the route\'s own reason, not a generic line', async () => {
  const dom = shareScenario(() => Promise.resolve({
    status: 400, json: () => Promise.resolve({ error: "You've reached the limit." }),
  }));

  submitShareForm(dom);
  await tick(); await tick();

  const errorEl = dom.panelBody.prepended[0];
  assert.strictEqual(errorEl.textContent, "You've reached the limit.");
  assert.strictEqual(errorEl.className, 'share-link-error');
  assert.strictEqual(errorEl.getAttribute('role'), 'alert',
                     'UT-4d: a silently-appearing paragraph is not announced without one');
});

run('an expired session redirects instead of painting an error', async () => {
  const dom = shareScenario(() => Promise.resolve({ status: 401, json: () => Promise.resolve({}) }));

  submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended, undefined, '"session expired" is not a form error');
  assert.strictEqual(dom.panelBody.innerHTML, null);
});

run('a network failure still says something', async () => {
  const dom = shareScenario(() => Promise.reject(new Error('offline')));

  submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended[0].textContent, 'Something went wrong. Please try again.');
});

// The panel lists one revoke form PER LINK next to the create form, and every
// one of them answers with the WHOLE panel - so two overlapping submits is the
// ordinary shape of this UI, not an edge case. This was the only async path in
// static/js with no in-flight guard (contrast playlists.js's previewToken, the
// pages' _navSeq, detail-chart.js's activeLoad, and hx-sync everywhere else).
run('a superseded response cannot repaint a panel the newer one replaced', async () => {
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom);   //< Revoke on link A
  submitShareForm(dom);   //< Revoke on link B, a moment later
  await tick();

  //< B answers first, with the panel as it stands now that both are gone
  settle[1].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>A and B gone</p>' }) });
  await tick(); await tick(); await tick();
  //< then A's answer lands - rendered while B still existed
  settle[0].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>B still listed</p>' }) });
  await tick(); await tick(); await tick();

  assert.strictEqual(dom.panelBody.innerHTML, '<p>A and B gone</p>',
    'a superseded response put a revoked link back on screen, with a live Copy '
    + 'button handing out a dead URL and a Revoke that now 403s');
});

run('a superseded failure cannot show an error over a newer success', async () => {
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom);
  submitShareForm(dom);
  await tick();

  settle[1].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>done</p>' }) });
  await tick(); await tick(); await tick();
  settle[0].reject(new Error('offline'));
  await tick(); await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended, undefined,
    'an error about a request the user has already moved past is a lie');
  assert.strictEqual(dom.panelBody.innerHTML, '<p>done</p>');
});

run('the submit button is disabled while its own request is in flight', async () => {
  const button = makeElement({ disabled: false });
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom, { submitButton: button });
  await tick();

  assert.strictEqual(button.disabled, true,
    'a double-click on Create is what put a bucket one over its cap');

  settle[0].resolve({ status: 400, json: () => Promise.resolve({ error: 'nope' }) });
  await tick(); await tick(); await tick();

  assert.strictEqual(button.disabled, false, 'a refused create has to stay retryable');
});

// A revoked token is gone for good - friends holding the link get refused and
// a new one has to be created and handed round - and the button sits 8px from
// Copy on a phone. So Revoke asks first. The confirm lives in this delegated
// handler and not in an inline onsubmit: `return false` there cancels only the
// default action, the event still bubbles here and the fetch would run anyway.

run('Revoke asks first, and a refusal submits nothing - natively or by fetch', () => {
  const dom = modalSetup({ confirmAnswer: false, respond: () => Promise.resolve({ status: 200 }) });

  const evt = submitShareForm(dom, { revoke: true });

  assert.strictEqual(dom.page.confirms.length, 1);
  assert.deepStrictEqual(dom.page.fetched, [], 'a refused revoke must not be sent');
  assert.strictEqual(evt.prevented, 1, 'nor may the browser submit it natively');
});

run('a confirmed Revoke goes through', async () => {
  const dom = shareScenario(() => Promise.resolve({ status: 200, json: () => Promise.resolve({ html: '<p>gone</p>' }) }));

  submitShareForm(dom, { revoke: true });
  await tick(); await tick();

  assert.strictEqual(dom.page.confirms.length, 1);
  assert.strictEqual(dom.page.fetched.length, 1);
  assert.strictEqual(dom.panelBody.innerHTML, '<p>gone</p>');
});

run('Create never asks', () => {
  const dom = shareScenario(() => Promise.resolve({ status: 200, json: () => Promise.resolve({ html: '' }) }));

  submitShareForm(dom);

  assert.deepStrictEqual(dom.page.confirms, []);
  assert.strictEqual(dom.page.fetched.length, 1);
});

run('an unrelated form inside the modal is left to submit normally', () => {
  const dom = modalSetup();
  const form = makeElement();
  form.matches = () => false;
  const evt = { target: form, prevented: 0, preventDefault() { this.prevented += 1; } };

  dom.modal.handlers.submit.call(dom.modal, evt);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(dom.page.fetched, []);
});

// ---------------------------------------------------------------- failure

// The scoping is the shared helper's (tests/test_htmx_filters.js pins it): a
// failure on some OTHER region must not blank the recap and offer a Retry for
// a request that never failed. The hand-rolled body listeners this replaced
// took no event at all, so they answered every failed swap on the page.
run('a failed swap is reported through the shared helper, scoped to the recap', () => {
  const page = loadWrapped({});

  assert.strictEqual(page.swapFailure.targetId, 'wrappedResults');
  assert.strictEqual(page.bodyListeners['htmx:responseError'], undefined,
    'an unscoped listener of its own is the shape the helper exists to replace');
  assert.strictEqual(page.bodyListeners['htmx:sendError'], undefined);
});

// js-F1's Wrapped half (2026-09-02 review, done alongside X6 above): re-issued
// off the FORM, not the address bar, the same as charts-page.js/history-
// page.js/top-list.js - a 4xx/5xx never touches the URL (htmx updates history
// only inside its successful-swap branch), so a retry of the stale URL after a
// failed filter change would re-request the OLD selection under a page that
// already shows the new one.
run('the retry re-serialises the form, not the stale URL', () => {
  // hx-get deliberately differs from window.location.pathname here, the way
  // it genuinely does on the public /shared/<token> page: the attribute names
  // the bare route (see templates/wrapped.html's comment on why - a year in
  // both hx-get and `action` would be sent twice), while the address bar
  // carries the token. Reading the URL instead of the attribute would ask a
  // route that does not know what to do with these params.
  const form = { id: 'wrappedFilters', getAttribute(name) { return name === 'hx-get' ? '/shared/tok123' : null; } };
  const page = loadWrapped({ search: '?year=2026', elements: { wrappedFilters: form } });

  page.swapFailure.retry();

  assert.strictEqual(page.ajax[0].url, '/shared/tok123',
    'the bare hx-get path: htmx appends the form values itself');
  assert.strictEqual(page.ajax[0].opts.source, form);
  assert.strictEqual(page.ajax[0].opts.target, '#wrappedResults');
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
  console.log(`all ${results.length} wrapped tests passed`);
})();
