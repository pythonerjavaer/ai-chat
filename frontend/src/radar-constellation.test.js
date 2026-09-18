import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { buildRadarDirectoryView, renderRadarConstellation } from './radar-constellation.js';

// Exercise the maintained directory, not a second hand-maintained copy.
const directorySource = readFileSync(new URL('../../backend/recruitment_directory.py', import.meta.url), 'utf8');
const poolSource = directorySource.slice(directorySource.indexOf('PERSONAL_MONITOR_POOLS = ['), directorySource.indexOf('EMPLOYER_ALIAS_GROUPS:'));
const pools = [...poolSource.matchAll(/"id": "([^"]+)"[\s\S]*?"name": "([^"]+)"[\s\S]*?"employers": \[([\s\S]*?)\]/g)]
  .map(([, id, name, employers]) => ({ id, name, employers: JSON.parse(`[${employers.replace(/,\s*$/, '')}]`) }));
const pool = id => pools.find(entry => entry.id === id);
const view = id => buildRadarDirectoryView(pool(id).employers);

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

test('all ten real sectors retain every original name and do not mutate directory data', () => {
  assert.equal(pools.length, 10);
  const before = structuredClone(pools);
  for (const { employers } of pools) {
    const directory = buildRadarDirectoryView(employers);
    const institutions = directory.entries.flatMap(entry => entry.kind === 'group' ? [entry.parent, ...entry.members].filter(Boolean) : [entry]);
    assert.deepEqual(institutions.flatMap(entry => entry.aliases).sort(), [...new Set(employers)].sort());
    assert.equal(directory.institutionCount, institutions.length);
  }
  assert.deepEqual(pools, before);
});

test('four audit firms appear once each with exact Chinese and English aliases', () => {
  const directory = view('professional_services');
  assert.equal(directory.entries.length, 4);
  assert.equal(directory.institutionCount, 4);
  assert.equal(directory.groupCount, 0);
  assert.deepEqual(directory.entries.map(entry => entry.label), ['德勤 Deloitte', '普华永道 PwC', '安永 EY', '毕马威 KPMG']);
  assert.deepEqual(directory.entries[0].aliases, ['Deloitte', '德勤']);
});

test('Ping An has one collapsed display group while bank, technology and insurance entities survive', () => {
  const directory = view('insurance_fintech');
  assert.equal(directory.entries.length, 12);
  assert.equal(directory.institutionCount, 21);
  assert.equal(directory.groupCount, 2);
  const group = directory.entries.find(entry => entry.kind === 'group' && entry.label === '中国平安');
  assert.equal(group.parent.label, '中国平安');
  assert.deepEqual(group.members.map(entry => entry.label),
    ['平安银行', '平安科技', '平安产险', '平安养老险', '平安理财']);
  assert.equal(group.members.some(entry => entry.label === group.label), false);
  // Sector membership is retained; the securities firm is not moved or copied.
  assert.equal(view('securities_funds_asset').entries.find(entry => entry.label === '平安证券').kind, 'employer');
  assert.equal(view('securities_funds_asset').groupCount, 0);
});

test('all other observed bilingual duplicates fold without collapsing independent units', () => {
  const tech = view('state_tech_transport');
  assert.equal(tech.institutionCount, pool('state_tech_transport').employers.length - 1);
  assert.deepEqual(tech.entries.find(entry => entry.label === '中国电子科技集团（中国电科）').aliases, ['中国电科', '中国电子科技集团']);
  // The separately maintained research institute and business division stay distinct.
  for (const name of ['中国移动', '中移九天', '中国移动通信研究院', '中国移动数智事业部'])
    assert.ok(tech.entries.some(entry => entry.label === name));
  const internet = view('internet_tech_scale');
  assert.equal(internet.institutionCount, pool('internet_tech_scale').employers.length - 2);
  assert.ok(internet.entries.some(entry => entry.label === '大疆 DJI'));
  assert.ok(internet.entries.some(entry => entry.label === '中芯国际 SMIC'));
  const consulting = view('consumer_global_consulting');
  assert.deepEqual(consulting.entries.find(entry => entry.label === '罗兰贝格 Roland Berger').aliases, ['Roland Berger', '罗兰贝格']);
  const amazon = consulting.entries.find(entry => entry.aliases?.includes('Amazon/AWS'));
  assert.equal(amazon.kind, 'employer');
  assert.deepEqual(amazon.aliases, ['Amazon/AWS', 'Amazon', 'AWS']);
  const tobacco = view('tobacco_monopoly');
  assert.deepEqual(tobacco.entries.find(entry => entry.kind === 'group').members.map(entry => entry.label),
    ['国家烟草专卖局', '中国烟草总公司', '中国烟草', '中烟工业']);
  assert.ok(tobacco.entries.some(entry => entry.label === '云南中烟'));
});

test('display grouping never infers ownership from a substring or merges a regional firm', () => {
  const names = ['平安银行', '中国平安', '平安银行合作伙伴', '平安好伙伴', '德勤', 'Deloitte',
    'Deloitte Australia', '德勤招聘合作伙伴', '中国太平', '太平洋保险', 'Amazon供应商'];
  const directory = buildRadarDirectoryView(names);
  const group = directory.entries.find(entry => entry.kind === 'group');
  assert.equal(group.parent.label, '中国平安');
  assert.deepEqual(group.members.map(entry => entry.label), ['平安银行']);
  for (const name of ['平安银行合作伙伴', '平安好伙伴', 'Deloitte Australia', '德勤招聘合作伙伴', '中国太平', '太平洋保险', 'Amazon供应商'])
    assert.ok(directory.entries.some(entry => entry.kind === 'employer' && entry.label === name));
});

test('native disclosure exposes group members and distinguishes display count from directory names', () => {
  const root = element('div');
  renderRadarConstellation(root, [pool('insurance_fintech'), pool('professional_services')], { createElement: element });
  let nodes = descendants(root);
  const group = nodes.find(node => node.tag === 'details');
  assert.equal(group.open, undefined); // Collapsed by native details default.
  assert.equal(group.children[0].tag, 'summary'); // Native Enter/Space and focus behavior.
  assert.ok(descendants(group).some(node => node.tag === 'li' && node.textContent === '平安银行'));
  assert.ok(!descendants(group).some(node => node.tag === 'li' && node.textContent === '中国平安'));
  const directory = view('insurance_fintech');
  assert.ok(nodes.some(node => node.textContent.includes(`${directory.entries.length} 个展示项 · ${directory.institutionCount} 个名录名称`)));
  assert.ok(nodes.some(node => node.textContent === `2 星域 · ${directory.entries.length + 4} 个展示项`));
  const stars = nodes.filter(node => node.className === 'star-map-node');
  stars[1].listeners.click();
  nodes = descendants(root);
  assert.ok(!nodes.some(node => node.tag === 'details'));
  assert.ok(nodes.some(node => node.textContent === '德勤 Deloitte' && node.title === '名录名称：Deloitte、德勤'));
  assert.ok(nodes.some(node => node.textContent.includes('4 个名录机构')));
  assert.equal(stars[1]['aria-pressed'], 'true');
});

test('Amazon and AWS presentation names collapse to one flat item with no redundant disclosure', () => {
  const names = ['Amazon/AWS', 'Amazon', 'AWS', '亚马逊', 'Amazon', 'AWS'];
  const directory = buildRadarDirectoryView(names);
  assert.equal(directory.institutionCount, 1);
  assert.equal(directory.groupCount, 0);
  assert.equal(directory.entries.length, 1);
  assert.equal(directory.entries[0].kind, 'employer');
  assert.deepEqual(directory.entries[0].aliases, ['Amazon/AWS', 'Amazon', 'AWS', '亚马逊']);
  const root = element('div');
  renderRadarConstellation(root, [{name: '外企', employers: names}], {createElement: element});
  assert.equal(descendants(root).some(node => node.tag === 'details'), false);
  assert.equal(descendants(root).filter(node => node.title === '名录名称：Amazon/AWS、Amazon、AWS、亚马逊').length, 1);
});

test('a parent and one real subsidiary group without repeating the parent among children', () => {
  const directory = buildRadarDirectoryView(['中国平安', 'Ping An', '平安银行']);
  assert.equal(directory.groupCount, 1);
  assert.equal(directory.institutionCount, 2);
  assert.equal(directory.entries.length, 1);
  const group = directory.entries[0];
  assert.equal(group.label, '中国平安');
  assert.deepEqual(group.parent.aliases, ['中国平安', 'Ping An']);
  assert.deepEqual(group.members.map(item => item.label), ['平安银行']);
});

test('parent aliases alone remain flat and subsidiary-only groups need at least two institutions', () => {
  const parents = buildRadarDirectoryView(['中国平安', '中国平安保险集团', 'Ping An']);
  assert.equal(parents.groupCount, 0);
  assert.equal(parents.entries[0].kind, 'employer');
  assert.equal(parents.entries.length, 1);
  const one = buildRadarDirectoryView(['平安银行']);
  assert.equal(one.entries[0].kind, 'employer');
  const siblings = buildRadarDirectoryView(['平安银行', '平安科技']);
  assert.equal(siblings.entries.length, 1);
  assert.equal(siblings.entries[0].kind, 'group');
  assert.equal(siblings.entries[0].parent, undefined);
  assert.deepEqual(siblings.entries[0].members.map(item => item.label), ['平安银行', '平安科技']);
});

test('Taikang shows its actual subsidiaries underneath one parent instead of duplicate parent rows', () => {
  const names = ['泰康保险集团', '泰康人寿', '泰康养老', '泰康资产', '泰康在线'];
  const directory = buildRadarDirectoryView(names);
  assert.equal(directory.entries.length, 1);
  assert.equal(directory.institutionCount, 5);
  assert.equal(directory.groupCount, 1);
  const group = directory.entries[0];
  assert.equal(group.kind, 'group');
  assert.equal(group.label, '泰康保险集团');
  assert.equal(group.parent.label, '泰康保险集团');
  assert.deepEqual(group.members.map(item => item.label), names.slice(1));
  const root = element('div');
  renderRadarConstellation(root, [{name: '保险', employers: names}], {createElement: element});
  const disclosure = descendants(root).find(node => node.tag === 'details');
  assert.deepEqual(descendants(disclosure).filter(node => node.tag === 'li').map(node => node.textContent), names.slice(1));
  assert.equal(descendants(disclosure).filter(node => node.tag === 'strong' && node.textContent === '泰康保险集团').length, 1);
  const maintainedGroup = view('insurance_fintech').entries.find(item => item.kind === 'group' && item.label === '泰康保险集团');
  assert.equal(maintainedGroup.parent.label, '泰康保险集团');
  assert.deepEqual(maintainedGroup.members.map(item => item.label).sort(), names.slice(1).sort());
});
