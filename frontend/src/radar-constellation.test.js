import assert from 'node:assert/strict';
import test from 'node:test';
import { buildRadarDirectoryView, normalizeRadarEmployerLabel, renderRadarConstellation } from './radar-constellation.js';

function element(tag) {
  return { tag, children: [], style: {}, listeners: {}, textContent: '',
    setAttribute(name, value) { this[name] = value; },
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); },
    replaceChildren(...children) { this.children = children; },
    addEventListener(name, fn) { this.listeners[name] = fn; },
  };
}
const descendants = node => [node, ...node.children.flatMap(descendants)];

test('aliases merge, but the star map never infers a group or subsidiary', () => {
  const directory = buildRadarDirectoryView(['中国平安保险集团', 'Ping An', '平安银行', '泰康人寿']);
  assert.equal(directory.groupCount, 0);
  assert.equal(directory.institutionCount, 3);
  assert.deepEqual(directory.entries.map(item => item.label), ['中国平安', '平安银行', '泰康人寿']);
  assert.deepEqual(directory.entries[0].aliases, ['中国平安保险集团', 'Ping An']);
  assert.equal(directory.entries.some(item => item.label === '泰康保险集团'), false);
});

test('only supplied high-priority employers are rendered', () => {
  const root = element('div');
  renderRadarConstellation(root, [{
    name: 'GPT 已发现单位',
    focus: '仅展示 ChatGPT 监控中实际出现的开放岗位招聘单位。',
    employers: ['亚马逊', 'AWS', '平安银行'],
  }], { createElement: element });
  const nodes = descendants(root);
  assert.equal(nodes.some(node => node.tag === 'details'), false);
  assert.ok(nodes.some(node => node.textContent === 'Amazon 亚马逊'));
  assert.ok(nodes.some(node => node.textContent === '平安银行'));
  assert.ok(!nodes.some(node => node.textContent === '中国平安'));
  assert.ok(nodes.some(node => node.textContent.includes('2 个高优先级单位')));
});

test('foreign employer labels use the same English-first bilingual identity everywhere', () => {
  assert.equal(normalizeRadarEmployerLabel('瑞银'), 'UBS 瑞银');
  assert.equal(normalizeRadarEmployerLabel('瑞银 UBS'), 'UBS 瑞银');
  assert.equal(normalizeRadarEmployerLabel('UBS'), 'UBS 瑞银');
});

test('empty priority atlas explains that no employer is available', () => {
  const root = element('div');
  renderRadarConstellation(root, [], { createElement: element });
  assert.equal(root.children[0].textContent, '尚无可展示的高优先级单位；已报名或收藏后会按行业归入星域。');
});
