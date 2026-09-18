import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { renderRadarConstellation } from './radar-constellation.js';
const source = readFileSync(new URL('./app.js', import.meta.url), 'utf8');
const functions = source.slice(source.indexOf('async function loadRecruitmentMonitors()'), source.indexOf('function renderRecruitmentDeadlineAlerts('));
function element(tag, className, textContent = '') {
  return { tag, className, textContent, style: {}, children: [], listeners: {},
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    appendChild(child) { this.children.push(child); },
    addEventListener(name, fn) { this.listeners[name] = fn; } };
}
test('directory failure is visible and retry loads employers independently of jobs', async () => {
  let fail = true;
  const requests = [];
  const container = element('div');
  const context = vm.createContext({
    renderRadarConstellation: (el, pools) => renderRadarConstellation(el, pools, { createElement: element }),
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
  const all = el => [el, ...el.children.flatMap(all)];
  assert.ok(all(container).some(el => el.textContent === '真实机构'));
  assert.ok(all(container).some(el => el.textContent.includes('1 个高优先级单位')));
  assert.deepEqual(requests, ['/recruitment/monitor-pools', '/recruitment/monitor-pools']);
  context.renderRecruitmentMonitors([]);
  assert.match(container.children[0].textContent, /尚无可展示的高优先级单位/);
});
test('selecting a star updates the employer list and both navigation controls', () => {
  const root = element('div');
  renderRadarConstellation(root,[{name:'能源',employers:['中国石油']},{name:'银行',employers:['中国银行']}],{createElement:element});
  const all = el => [el,...el.children.flatMap(all)];
  const stars = all(root).filter(el=>el.className==='star-map-node');
  stars[1].listeners.click();
  assert.equal(stars[0]['aria-pressed'],'false');
  assert.equal(stars[1]['aria-pressed'],'true');
  assert.equal(all(root).filter(el=>el.className==='star-map-key')[1]['aria-pressed'],'true');
  assert.ok(all(root).some(el=>el.textContent==='中国银行'));
  assert.ok(!all(root).some(el=>el.textContent==='中国石油'));
});
