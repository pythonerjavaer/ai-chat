import test from 'node:test';
import assert from 'node:assert/strict';
import { prepareApplicationRecords, initApplicationRecords } from './radar-application-records.js';

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.handlers = {}; this.value = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  dispatch(name) { return this.handlers[name]?.({ preventDefault() {} }); }
}
const all = node => [node, ...node.children.flatMap(all)];
const text = node => all(node).map(item => item.textContent || '').join(' ');
function fixture(t) {
  const previous = globalThis.document;
  globalThis.document = { createElement: tag => new Node(tag) };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const requests = [], notices = []; let token = 'owner';
  const component = initApplicationRecords({ session: () => token, toast: message => notices.push(message),
    api: (url, options = {}) => new Promise((resolve, reject) => requests.push({url, ...options, resolve, reject})),
  });
  return { component, requests, notices, byId: id => all(component.panel).find(node => node.id === id),
    button: label => all(component.panel).find(node => node.tag === 'button' && node.textContent === label),
    switchAccount() { token = 'new-owner'; component.reset(); },
  };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test('company-only confirmations remain private records with no fabricated job and stable keys', async () => {
  const [first] = await prepareApplicationRecords([{company: 'Example', batch: '2027 秋招'}]);
  assert.equal(first.body.title, null);
  assert.equal(first.body.notes, null);
  assert.match(first.key, /^manual-[0-9a-f]{64}$/);
  const [again] = await prepareApplicationRecords([{company: ' example ', batch: '2027 秋招', notes: '补充备注'}]);
  assert.equal(first.key, again.key);
  const [specific] = await prepareApplicationRecords([{company: 'Example', title: '产品经理', batch: '2027 秋招'}]);
  assert.notEqual(first.key, specific.key);
});

test('imports validate fields and real dates and reject chat identifiers', async () => {
  for (const invalid of [[], [{title: 'Unknown company'}], [{company: 'A', source_url: 'https://example.com'}],
    [{company: 'A', confirmed_date: '2026-02-30'}], [{company: 'A', notes: 'https://chatgpt.com/c/private'}]]) {
    await assert.rejects(prepareApplicationRecords(invalid));
  }
  const values = await prepareApplicationRecords([{company: 'A'}, {company: 'A'}]);
  assert.equal(values.length, 1);
});

test('batch import previews names before writing and saves only private confirmed records', async t => {
  const f = fixture(t);
  f.byId('radar-application-records-json').value = JSON.stringify([{company: 'Company A'}, {company: 'Company B', title: 'Analyst'}]);
  await f.button('预览导入').dispatch('click');
  assert.equal(f.requests.length, 0);
  assert.match(text(f.component.panel), /将保存 2 条/);
  assert.match(text(f.component.panel), /Company A · 按单位记录/);
  const pending = f.button('保存预览中的报名记录').dispatch('click');
  assert.equal(f.requests[0].method, 'PUT');
  assert.match(f.requests[0].url, /^\/future-radar\/application-records\/manual-/);
  assert.equal(JSON.parse(f.requests[0].body).title, null);
  f.requests[0].resolve({}); await tick();
  assert.equal(JSON.parse(f.requests[1].body).title, 'Analyst');
  f.requests[1].resolve({}); await tick();
  f.requests[2].resolve({total: 2, items: [{record_key: 'a', company: 'Company A', title: null}, {record_key: 'b', company: 'Company B', title: 'Analyst'}]});
  await pending;
  assert.match(text(f.component.panel), /共 2 条历史报名记录/);
  assert.equal(f.requests.some(request => request.url.includes('/opportunities')), false);
});

test('editing the JSON invalidates its preview and an account switch stops remaining writes', async t => {
  const f = fixture(t), input = f.byId('radar-application-records-json');
  input.value = JSON.stringify([{company: 'A'}, {company: 'B'}]); await f.button('预览导入').dispatch('click');
  await input.dispatch('input'); assert.equal(f.button('保存预览中的报名记录').disabled, true);
  await f.button('预览导入').dispatch('click');
  const pending = f.button('保存预览中的报名记录').dispatch('click');
  f.switchAccount(); f.requests[0].resolve({}); await pending;
  assert.equal(f.requests.length, 1);
  assert.equal(f.notices.length, 0);
  assert.doesNotMatch(text(f.component.panel), /Company A/);
  assert.equal(input.value, '');
});

test('history pagination is complete and old account reads are discarded', async t => {
  const f = fixture(t);
  let pending = f.component.load(); f.requests[0].resolve({total: 101, items: [{record_key: 'one', company: 'A'}]}); await pending;
  pending = f.button('下一页 →').dispatch('click');
  assert.match(f.requests[1].url, /page=2&/);
  f.requests[1].resolve({total: 101, items: [{record_key: 'last', company: 'Last'}]}); await pending;
  assert.match(text(f.component.panel), /第 2 \/ 2 页/);
  pending = f.component.load(); f.switchAccount(); f.requests[2].resolve({total: 1, items: [{record_key: 'private', company: 'Private old account'}]}); await pending;
  assert.doesNotMatch(text(f.component.panel), /Private old account/);
});

test('partial import failure retains retryable keys and reports only successful writes', async t => {
  const f = fixture(t);
  f.byId('radar-application-records-json').value = JSON.stringify([{company: 'A'}, {company: 'B'}]); await f.button('预览导入').dispatch('click');
  let pending = f.button('保存预览中的报名记录').dispatch('click');
  const firstKey = f.requests[0].url;
  f.requests[0].resolve({}); await tick(); f.requests[1].reject(new Error('offline')); await tick();
  f.requests[2].resolve({total: 1, items: [{record_key: 'a', company: 'A'}]}); await pending;
  assert.match(text(f.component.panel), /已保存 1 \/ 2 条/);
  assert.equal(f.button('保存预览中的报名记录').disabled, false);
  pending = f.button('保存预览中的报名记录').dispatch('click'); assert.equal(f.requests[3].url, firstKey);
  f.switchAccount(); f.requests[3].resolve({}); await pending;
});
