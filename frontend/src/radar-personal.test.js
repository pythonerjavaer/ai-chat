import test from 'node:test';
import assert from 'node:assert/strict';
import { initRadarPersonal } from './radar-personal.js';

class Node {
  constructor(tag) {
    this.tag = tag; this.dataset = {}; this.children = []; this.handlers = {};
    this.classList = { contains: name => (this.className || '').split(' ').includes(name) };
  }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  after() {}
  contains() { return false; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  dispatchEvent(event) { return this.handlers[event.type]?.(event); }
  replaceChildren(...nodes) { this.children = nodes; }
}
function fixture(t) {
  const nodes = [];
  let token = 'first-account';
  const requests = [], notices = [], changes = [];
  const previous = globalThis.document;
  globalThis.document = {
    createElement(tag) { const node = new Node(tag); nodes.push(node); return node; },
    querySelectorAll() { return nodes.filter(n => n.dataset.applicationJob); },
  };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const host = new Node('dialog'); host.open = true; host.querySelector = () => new Node('section');
  const personal = initRadarPersonal({ host, session: () => token, makeCard: job => { const card = new Node('article'); card.job = job; return card; },
    api: (url, options) => new Promise((resolve, reject) => requests.push({url, ...options, resolve, reject})),
    toast: message => notices.push(message), onApplicationChange: (...args) => changes.push(args),
  });
  t.after(() => personal.reset());
  return { personal, requests, notices, changes, nodes, host,
    panel: name => nodes.find(node => node.dataset.radarPanel === name),
    accept: value => requests.at(-1).resolve(value), fail: () => requests.at(-1).reject(new Error('offline')),
    switchAccount() { token = 'second-account'; personal.reset(); },
  };
}

test('manual choices update list and detail only after server confirmation', async t => {
  const f = fixture(t);
  const job = {id:'actual-job', company:'Example', title:'Analyst', application_status:'not_applied'};
  const a = f.personal.applicationControl({...job});
  const b = f.personal.applicationControl({...job});
  const select = a.children[1]; select.value = 'applied';
  const pending = select.dispatchEvent(new Event('change'));
  assert.equal(a.dataset.applicationStatus, 'not_applied');
  assert.equal(b.children[1].disabled, true);
  assert.deepEqual(JSON.parse(f.requests[0].body), {status:'applied'});
  f.accept({application_status:'applied'}); await pending;
  assert.equal(a.children[0].textContent, '已投递');
  assert.equal(b.children[1].value, 'applied');
  assert.equal(b.children[1].disabled, false);
  assert.deepEqual(f.changes, [['actual-job','applied']]);
  select.value = 'skipped';
  const skip = select.dispatchEvent(new Event('change'));
  f.accept({application_status:'skipped'}); await skip;
  assert.equal(b.dataset.applicationStatus, 'skipped');
});

const descendants = node => [node, ...node.children.flatMap(descendants)];
const cards = node => descendants(node).filter(item => item.tag === 'article');
const text = node => descendants(node).map(item => item.textContent || '').join(' ');

test('applied history uses an independent complete query and shows unbookmarked closed jobs', async t => {
  const f = fixture(t);
  const pending = f.personal.loadApplied();
  const params = new URL(f.requests[0].url, 'https://test.local').searchParams;
  assert.equal(params.get('status'), 'all');
  assert.equal(params.get('application_status'), 'applied');
  assert.equal(params.get('view'), 'jobs');
  assert.equal(params.get('balanced_only'), 'false');
  assert.equal(params.get('priority_only'), 'false');
  for (const filter of ['q', 'company', 'category', 'tier_code', 'closing_after', 'source_id']) assert.equal(params.has(filter), false);
  f.accept({total: 1, items: [{id: 'closed-unbookmarked', application_status: 'applied', status: 'closed'}]});
  await pending;
  assert.deepEqual(cards(f.panel('applied')).map(card => card.job.id), ['closed-unbookmarked']);
  assert.match(text(f.panel('applied')), /招聘已关闭/);
  assert.match(text(f.panel('applied')), /共 1 条已报名/);
});

test('applied history paginates every record and backs up after the last record on a page is removed', async t => {
  const f = fixture(t);
  let pending = f.personal.loadApplied();
  f.accept({total: 51, items: Array.from({length: 50}, (_, i) => ({id: String(i), application_status: 'applied'}))});
  await pending;
  const next = descendants(f.panel('applied')).find(node => node.textContent === '下一页 →');
  assert.equal(next.disabled, false);
  pending = next.dispatchEvent(new Event('click'));
  assert.match(f.requests.at(-1).url, /page=2(?:&|$)/);
  f.accept({total: 51, items: [{id: 'last', application_status: 'applied'}]}); await pending;
  assert.match(text(f.panel('applied')), /第 2 \/ 2 页/);
  assert.equal(cards(f.panel('applied')).length, 1);
  pending = f.personal.loadApplied(2);
  f.accept({total: 50, items: []});
  await Promise.resolve();
  assert.match(f.requests.at(-1).url, /page=1(?:&|$)/);
  f.accept({total: 50, items: [{id: 'remaining', application_status: 'applied'}]}); await pending;
  assert.match(text(f.panel('applied')), /第 1 \/ 1 页/);
  assert.deepEqual(cards(f.panel('applied')).map(card => card.job.id), ['remaining']);
});

test('applied history rejects old account and superseded responses', async t => {
  const f = fixture(t);
  const old = f.personal.loadApplied(); const oldRequest = f.requests[0];
  f.switchAccount();
  const current = f.personal.loadApplied();
  f.accept({total: 1, items: [{id: 'new-account', application_status: 'applied'}]}); await current;
  oldRequest.resolve({total: 1, items: [{id: 'private-old-account', application_status: 'applied'}]}); await old;
  assert.deepEqual(cards(f.panel('applied')).map(card => card.job.id), ['new-account']);
  const superseded = f.personal.loadApplied(); const supersededRequest = f.requests.at(-1);
  const latest = f.personal.loadApplied(); f.accept({total: 0, items: []}); await latest;
  supersededRequest.resolve({total: 1, items: [{id: 'stale'}]}); await superseded;
  assert.equal(cards(f.panel('applied')).length, 0);
});

test('removing an application refreshes the visible history without requiring a bookmark', async t => {
  const f = fixture(t);
  f.panel('applied').className = 'radar-tab-panel';
  let pending = f.personal.loadApplied();
  f.accept({total: 1, items: [{id: 'applied-job', application_status: 'applied'}]}); await pending;
  const control = f.personal.applicationControl({id: 'applied-job', application_status: 'applied'});
  control.children[1].value = 'not_applied'; pending = control.children[1].dispatchEvent(new Event('change'));
  f.accept({application_status: 'not_applied'}); await pending;
  assert.match(f.requests.at(-1).url, /application_status=applied/);
  f.accept({total: 0, items: []}); await Promise.resolve();
  assert.equal(cards(f.panel('applied')).length, 0);
  assert.match(text(f.panel('applied')), /暂无已确认/);
});

test('saved candidates exclude already-applied and skipped bookmarks', async t => {
  const f = fixture(t);
  f.personal.start();
  const records = ['not_applied', 'planned', 'applied', 'skipped'].map((status, i) => ({priority: i + 1, job: {id: status, application_status: status}}));
  f.requests.find(request => request.url.endsWith('/saved-jobs')).resolve({items: records});
  f.requests.find(request => request.url.endsWith('/notifications')).resolve({items: []});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(cards(f.panel('saved')).map(card => card.job.id), ['not_applied', 'planned']);
  assert.match(text(f.panel('saved')), /收藏不代表已报名/);
});

test('failed applied-history reads show a retryable error and preserve confirmed records', async t => {
  const f = fixture(t);
  let pending = f.personal.loadApplied(); f.accept({total: 1, items: [{id: 'retained'}]}); await pending;
  pending = f.personal.loadApplied(); f.fail(); await pending;
  assert.deepEqual(cards(f.panel('applied')).map(card => card.job.id), ['retained']);
  assert.match(text(f.panel('applied')), /读取失败/);
  assert.equal(descendants(f.panel('applied')).find(node => node.textContent === '刷新已报名').disabled, false);
});

test('failed save restores the previous choice without removing the card', async t => {
  const f = fixture(t);
  const control = f.personal.applicationControl({id:'job', application_status:'planned'});
  const select = control.children[1]; select.value = 'skipped';
  const pending = select.dispatchEvent(new Event('change')); f.fail(); await pending;
  assert.equal(select.value, 'planned'); assert.equal(select.disabled, false);
  assert.deepEqual(f.changes, []); assert.match(f.notices[0], /失败/);
});

test('an old account response cannot update the next account', async t => {
  const f = fixture(t);
  const control = f.personal.applicationControl({id:'job', application_status:'not_applied'});
  const select = control.children[1]; select.value = 'applied';
  const pending = select.dispatchEvent(new Event('change'));
  f.switchAccount(); f.accept({application_status:'applied'}); await pending;
  assert.deepEqual(f.changes, []); assert.deepEqual(f.notices, []);
});
