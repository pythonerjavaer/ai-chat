import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const functions = source.slice(source.indexOf('async function loadRecruitmentMonitors()'), source.indexOf('function renderRecruitmentDeadlineAlerts('));
function element(tag, className, textContent = '') {
  return { tag, className, textContent, children: [], listeners: {},
    replaceChildren(...children) { this.children = children; },
    appendChild(child) { this.children.push(child); },
    addEventListener(name, fn) { this.listeners[name] = fn; } };
}
test('directory failure is visible and retry loads employers independently of jobs', async () => {
  let fail = true;
  const requests = [];
  const container = element('div');
  const context = vm.createContext({
    elements: { recruitmentMonitorPools: container },
    document: { createElement: element }, makeElement: element,
    DOMPurify: { sanitize: (text) => text },
    api: async (path) => {
      requests.push(path);
      if (fail) throw new Error('unavailable');
      return { monitor_pools: [{ name: '企业名录', employers: ['真实机构'], focus: '公开招聘' }] };
    },
  });
  vm.runInContext(functions, context);
  await context.loadRecruitmentMonitors();
  assert.match(container.children[0].textContent, /暂时无法读取/);
  const retry = container.children[1];
  fail = false;
  await retry.listeners.click();
  assert.match(container.children[0].innerHTML, /真实机构/);
  assert.match(container.children[0].innerHTML, /1 个名录机构/);
  assert.deepEqual(requests, ['/recruitment/monitor-pools', '/recruitment/monitor-pools']);
  context.renderRecruitmentMonitors([]);
  assert.match(container.children[0].textContent, /尚未配置机构名录/);
});
