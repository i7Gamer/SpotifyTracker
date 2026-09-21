// Plain-node unit test for the Top Songs/Artists/Albums browser logic
// (static/js/top-list.js). Run with: node tests/test_top_list_page.js
//
// Everything htmx could express declaratively was deleted from this file; what
// is left is the handful of things it could NOT, which is exactly the part with
// no coverage. Two of them are load-bearing:
//
//   * the jump-to-page handler REPLACES the history entry, never pushes. A push
//     would make Back walk through page numbers instead of leaving the page.
//   * the htmx:configRequest veto is scoped to the FORM. A boosted pagination
//     link carries its whole query in its href and has to keep working while
//     the Time Period select sits on a half-typed custom range - the very state
//     that blocks a form request.
//
// The "Full plays only" hidden field used to be a third; it moved to
// static/js/htmx-filters.js (and tests/test_htmx_filters.js with it) when
// /history started rendering the same partial, because this file is loaded by
// the Top pages only.
//
// The stub is hand-rolled (no jsdom). Note HtmxFilters.RANGE_OK is null, not a
// string: the veto below turns on `problem !== RANGE_OK`, so a stub returning
// a truthy "ok" would pass a broken test.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'top-list.js');
function loadTopList(options) {
  options = options || {};
  const calls = {
    replaced: [], pushed: [], ajax: [], requests: [], validations: [], retries: [],
    syncedRanges: [], swapFailure: null, bodyListeners: {},
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/top-songs', search: options.search || '' },
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); },
      pushState(state, title, url) { calls.pushed.push(url); },
    },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.HtmxFilters = {
    syncCustomRange(containerId) { calls.syncedRanges.push(containerId); },
    requestPage(page, targetId) { calls.requests.push({ page, targetId }); },
    validateFormRequest(evt, formId) {
      if (!evt.detail.elt || evt.detail.elt.id !== formId) return;
      calls.validations.push({ evt, formId });
    },
    retryForm(formId, targetId) { calls.retries.push({ formId, targetId }); },
    onSwapFailure(targetId, retry) { calls.swapFailure = { targetId, retry }; },
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

function configRequest(page, elementId, parameters) {
  const evt = {
    detail: { elt: elementId === null ? null : { id: elementId }, parameters: parameters || {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);
  return evt;
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------ jump to page

// htmx does the replacing (its `replace` option is forwarded into the same
// history update hx-replace-url feeds), and only inside its successful-swap
// branch. The address bar used to be rewritten HERE, before the request, so a
// failed jump left it claiming a page the list never showed.
run('a page jump lets htmx replace the URL on success and never pushes one', () => {
  const page = loadTopList({ search: '?interval=last-30-days' });

  page.window.__paginationAjaxHandler(4);

  assert.deepStrictEqual(page.requests, [{ page: 4, targetId: 'topListResults' }]);
});

run('a page jump keeps the other filters in both the URL and the request', () => {
  const page = loadTopList({ search: '?q=mf+doom&sortBy=plays&page=2' });

  page.window.__paginationAjaxHandler(5);

  assert.deepStrictEqual(page.requests, [{ page: 5, targetId: 'topListResults' }]);
});

// -------------------------------------------------------- the request veto

run('Top lists delegate form validation to the shared helper', () => {
  const page = loadTopList();

  const evt = configRequest(page, 'topListFilters');

  assert.deepStrictEqual(page.validations, [{ evt, formId: 'topListFilters' }]);
});

run('Top lists delegate valid form serialization to the shared helper', () => {
  const page = loadTopList();
  const parameters = { q: '', sortBy: 'plays' };

  const evt = configRequest(page, 'topListFilters', parameters);

  assert.deepStrictEqual(page.validations, [{ evt, formId: 'topListFilters' }]);
});

run('a boosted pagination link is delegated without form validation', () => {
  const page = loadTopList();

  configRequest(page, 'somePaginationLink');

  assert.deepStrictEqual(page.validations, []);
});

run('a request from an element with no id is delegated without form validation', () => {
  const page = loadTopList();

  configRequest(page, null);

  assert.deepStrictEqual(page.validations, []);
});

// ------------------------------------------------------- interval + retry

run('the Time Period select syncs its own custom-range container', () => {
  const page = loadTopList({});

  page.window.updateIntervalFilter();

  assert.deepStrictEqual(page.syncedRanges, ['customDates']);
});

// Off the form, not the address bar: a 4xx/5xx never touches the URL (htmx
// updates history only inside its successful-swap branch), so after a failed
// filter change the controls show the new choice while the URL still says the
// old one - and a retry of the URL rendered the old list under the new
// selection.
run('a failed swap offers a retry that re-serialises the form, not the stale URL', () => {
  const form = { id: 'topListFilters', getAttribute(name) { return name === 'hx-get' ? '/top-songs' : null; } };
  const page = loadTopList({ search: '?sortBy=plays', elements: { topListFilters: form } });
  assert.strictEqual(page.swapFailure.targetId, 'topListResults');

  page.swapFailure.retry();

  assert.deepStrictEqual(page.retries, [{ formId: 'topListFilters', targetId: 'topListResults' }]);
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
  console.log(`all ${results.length} top-list tests passed`);
})();
