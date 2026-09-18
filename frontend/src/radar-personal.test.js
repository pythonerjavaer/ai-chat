import test from 'node:test';
import assert from 'node:assert/strict';
import { initRadarPersonal } from './radar-personal.js';

class Node {
  constructor(tag) { this.tag = tag; this.dataset = {}; this.children = []; this.handlers = {}; }
  append(...nodes) { this.children.push(...nodes); }
  after() {}
  setAttribute(name, value) { this[name] = value; }
  addEventListener(event, handler) { this.handlers[event] = handler; }
  dispatchEvent(event) { return this.handlers[event.type]?.(event); }
  replaceChildren(...nodes) { this.children = nodes; }
}
function fixture(t) {
  const nodes = [];
  let token = 'first-account';
  let resolve, reject;
  const requests = [], notices = [], changes = [];
  const previous = globalThis.document;
  globalThis.document = {
    createElement(tag) { const node = new Node(tag); nodes.push(node); return node; },
    querySelectorAll() { return nodes.filter(n => n.dataset.applicationJob); },
  };
  t.after(() => { if (previous) globalThis.document = previous; else delete globalThis.document; });
  const host = new Node('dialog'); host.querySelector = () => new Node('section');
  const personal = initRadarPersonal({ host, session: () => token, makeCard: () => new Node('article'),
    api: (url, options) => { requests.push({url, ...options}); return new Promise((yes,no) => { resolve=yes; reject=no; }); },
    toast: message => notices.push(message), onApplicationChange: (...args) => changes.push(args),
  });
  return { personal, requests, notices, changes, accept: value => resolve(value), fail: () => reject(new Error('offline')),
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
