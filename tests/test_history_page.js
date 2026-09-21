// Plain-node unit test for the /history browser logic (static/js/history-page.js).
// Run with: node tests/test_history_page.js
//
// The sibling of tests/test_top_list_page.js: these two files are near-twins by
// design (both are what survived the htmx migration), and they are tested
// separately rather than through a shared harness precisely because a change
// applied to one and not the other is the failure worth catching.
//
// What is unique to this page is the Date sort toggle. It has no natural form
// control - it is a button driving a hidden field - so all three of "flip the
// value", "relabel the button" and "tell the form to re-fire" are hand-written,
// and a toggle that flips the value without re-firing looks exactly like a
// toggle that works until you notice the list never changed.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'history-page.js');
const FILTERS_SCRIPT = path.join(__dirname, '..', 'static', 'js', 'htmx-filters.js');

function makeField(value) {
  return { value: value === undefined ? '' : value, textContent: '' };
}

//< a button tracking aria-pressed/aria-label the way the real DOM would -
//  the same shape as makeDateField below, plus textContent for the button's
//  own "Date up-arrow/down-arrow" glyph
function makeToggleButton() {
  const attrs = {};
  return {
    textContent: '',
    setAttribute(name, val) { attrs[name] = val; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; },
  };
}

//< a date input, tracking aria-invalid the way the real DOM would
function makeDateField(value) {
  const attrs = {};
  return {
    value: value === undefined ? '' : value,
    style: {},
    setAttribute(name, val) { attrs[name] = val; },
    removeAttribute(name) { delete attrs[name]; },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name) ? attrs[name] : null; },
  };
}

function makeForm() {
  const dispatched = [];
  return { dispatched, dispatchEvent(evt) { dispatched.push(evt.type); return true; } };
}

function loadHistory(options) {
  options = options || {};
  const calls = {
    replaced: [], pushed: [], ajax: [], requests: [], validations: [],
    retries: [], syncedRanges: [],
    swapFailure: null, bodyListeners: {},
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/history', search: options.search || '' },
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

function loadHistoryWithRealFilters(options) {
  options = options || {};
  const calls = {
    ajax: [], replaced: [], pushed: [], bodyListeners: {}, documentListeners: {},
    swapFailure: null,
  };
  const form = options.form || {
    id: 'historyFilters',
    getAttribute(name) { return name === 'hx-get' ? '/history' : null; },
  };
  const elements = {
    interval: { value: options.interval || 'custom' },
    startDate: makeDateField(options.startDate || ''),
    endDate: makeDateField(options.endDate || ''),
    dateError: { textContent: '', style: {} },
    customDates: { style: {} },
    historyFilters: form,
    historyResults: { id: 'historyResults', dataset: {} },
  };
  global.window = {
    location: { pathname: '/history', search: options.search || '' },
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); },
      pushState(state, title, url) { calls.pushed.push(url); },
    },
    AjaxStatus: {
      showBanner() {},
      renderInto(target, retry) { retry(); },
    },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    addEventListener(type, fn) { calls.documentListeners[type] = fn; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  delete require.cache[require.resolve(FILTERS_SCRIPT)];
  global.HtmxFilters = require(FILTERS_SCRIPT);
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  calls.elements = elements;
  return calls;
}

function sortElements(currentValue) {
  const field = makeField(currentValue);
  const button = makeToggleButton();
  const form = makeForm();
  return {
    elements: { historySortValue: field, historySort: button, historyFilters: form },
    field, button, form,
  };
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ---------------------------------------------------------- the sort toggle

run('the default (newest first) flips to oldest and relabels the button', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, 'oldest');
  assert.strictEqual(dom.button.textContent, 'Date ↑');
});

run('flipping back clears the field rather than writing a second spelling', () => {
  const dom = sortElements('oldest');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, '', 'the default is the ABSENCE of the param');
  assert.strictEqual(dom.button.textContent, 'Date ↓');
});

run('flipping the sort re-fires the form, or the list never changes', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.deepStrictEqual(dom.form.dispatched, ['historyRefresh']);
});

run('two flips return to exactly where they started', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();
  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, '');
  assert.strictEqual(dom.button.textContent, 'Date ↓');
  assert.deepStrictEqual(dom.form.dispatched, ['historyRefresh', 'historyRefresh']);
});

// aria-pressed and a descriptive aria-label follow the same flip (UT-15,
// 2026-09-02 review) - the fixed "Toggle date sort order" label never told a
// screen reader which order was active, and the button carried no
// aria-pressed at all, unlike detail-history.js's filter tabs.
run('flipping to oldest sets aria-pressed and names both the active and next order', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.button.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.button.getAttribute('aria-label'),
    'Sorted oldest first - click to sort newest first');
});

run('flipping back to newest clears aria-pressed and relabels for the reverse click', () => {
  const dom = sortElements('oldest');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.button.getAttribute('aria-pressed'), 'false');
  assert.strictEqual(dom.button.getAttribute('aria-label'),
    'Sorted newest first - click to sort oldest first');
});

// ------------------------------------------------------------ jump to page

// htmx does the replacing (its `replace` option is forwarded into the same
// history update hx-replace-url feeds), and only inside its successful-swap
// branch. The address bar used to be rewritten HERE, before the request, so a
// failed jump left it claiming a page the list never showed.
run('a page jump lets htmx replace the URL on success and never pushes one', () => {
  const page = loadHistory({ search: '?interval=last-7-days' });

  page.window.__paginationAjaxHandler(3);

  assert.deepStrictEqual(page.requests, [{ page: 3, targetId: 'historyResults' }]);
});

run('a page jump keeps the other filters and is issued off the history list', () => {
  const page = loadHistory({ search: '?q=liquid&page=9' });

  page.window.__paginationAjaxHandler(2);

  assert.deepStrictEqual(page.requests, [{ page: 2, targetId: 'historyResults' }]);
});

// -------------------------------------------------------- the request veto

run('History delegates form validation to the shared helper', () => {
  const page = loadHistory();

  const evt = {
    detail: { elt: { id: 'historyFilters' }, parameters: {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.deepStrictEqual(page.validations, [{ evt, formId: 'historyFilters' }]);
});

run('History delegates valid form serialization to the shared helper', () => {
  const page = loadHistory();
  const parameters = { q: '', interval: '' };

  const evt = {
    detail: { elt: { id: 'historyFilters' }, parameters },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.deepStrictEqual(page.validations, [{ evt, formId: 'historyFilters' }]);
});

run('a boosted pagination link is delegated without form validation', () => {
  const page = loadHistory();

  const evt = {
    detail: { elt: { id: 'paginationLink' }, parameters: {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.deepStrictEqual(page.validations, []);
});

// ------------------------------------------------------- interval + retry

run('the Time Period select syncs the history custom-range container', () => {
  const page = loadHistory({});

  page.window.updateHistoryInterval();

  assert.deepStrictEqual(page.syncedRanges, ['historyCustomDates'],
                         'the container id differs from the Top pages - a copy-paste would swap them');
});

// Off the form, not the address bar: a 4xx/5xx never touches the URL (htmx
// updates history only inside its successful-swap branch), so after a failed
// filter change the controls show the new choice while the URL still says the
// old one - and a retry of the URL rendered the old list under the new
// selection.
run('a failed swap offers a retry that re-serialises the form, not the stale URL', () => {
  const page = loadHistory({ search: '?q=liquid' });
  assert.strictEqual(page.swapFailure.targetId, 'historyResults');

  page.swapFailure.retry();

  assert.deepStrictEqual(page.retries, [{ formId: 'historyFilters', targetId: 'historyResults' }]);
});

// ------------------------------------------------ real shared helper integration

function realConfigRequest(page, eltId, parameters) {
  const evt = {
    detail: { elt: { id: eltId }, parameters: parameters || {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);
  return evt;
}

run('History uses the real shared range validation and ARIA state transitions', () => {
  const page = loadHistoryWithRealFilters({
    startDate: '2026-05-01',
    endDate: '2026-01-01',
  });
  const inverted = realConfigRequest(page, 'historyFilters', { q: '' });

  assert.strictEqual(inverted.prevented, 1);
  assert.strictEqual(page.elements.startDate.getAttribute('aria-invalid'), 'true');
  assert.strictEqual(page.elements.endDate.getAttribute('aria-invalid'), 'true');

  page.elements.endDate.value = '2026-06-01';
  const validParameters = { q: '' };
  const valid = realConfigRequest(page, 'historyFilters', validParameters);
  assert.strictEqual(valid.prevented, 0);
  assert.strictEqual(page.elements.startDate.getAttribute('aria-invalid'), null);
  assert.strictEqual(page.elements.endDate.getAttribute('aria-invalid'), null);
  assert.deepStrictEqual(validParameters, {});

  page.elements.endDate.value = '';
  const incomplete = realConfigRequest(page, 'historyFilters', {});
  assert.strictEqual(incomplete.prevented, 1);
  assert.strictEqual(page.elements.startDate.getAttribute('aria-invalid'), null);
  assert.strictEqual(page.elements.endDate.getAttribute('aria-invalid'), null);
});

run('History leaves unrelated requests alone with the real shared helper', () => {
  const page = loadHistoryWithRealFilters({
    startDate: '2026-05-01',
    endDate: '2026-01-01',
  });
  const evt = realConfigRequest(page, 'paginationLink');
  assert.strictEqual(evt.prevented, 0);
  assert.strictEqual(page.elements.startDate.getAttribute('aria-invalid'), null);
});

run('History retries the current form and replaces its URL only through htmx', () => {
  const page = loadHistoryWithRealFilters({ search: '?q=liquid&page=9' });
  page.window.__paginationAjaxHandler(2);

  assert.strictEqual(page.ajax.length, 1);
  assert.strictEqual(page.ajax[0].opts.replace, page.ajax[0].url);
  assert.deepStrictEqual(page.replaced, []);
  assert.deepStrictEqual(page.pushed, []);

  page.documentListeners['htmx:responseError']({
    detail: { target: page.elements.historyResults },
  });
  assert.strictEqual(page.ajax.length, 2);
  assert.strictEqual(page.ajax[1].url, '/history');
  assert.strictEqual(page.ajax[1].opts.source, page.elements.historyFilters);
  assert.strictEqual(page.ajax[1].opts.target, '#historyResults');
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
  console.log(`all ${results.length} history-page tests passed`);
})();
