// Presentation aliases mirror the explicit names in recruitment_directory.py.
// These do not change recruitment identities or merge subsidiaries in the data.
const DIRECTORY_ALIASES = [
  ['德勤 Deloitte', '德勤', 'Deloitte'],
  ['普华永道 PwC', '普华永道', 'PwC'],
  ['安永 EY', '安永', 'EY'],
  ['毕马威 KPMG', '毕马威', 'KPMG'],
  ['中国电子科技集团（中国电科）', '中国电子科技集团', '中国电科'],
  ['大疆 DJI', '大疆', 'DJI', '大疆创新', '深圳市大疆创新科技'],
  ['中芯国际 SMIC', '中芯国际', 'SMIC'],
  ['罗兰贝格 Roland Berger', '罗兰贝格', 'Roland Berger'],
  ['亚马逊 / AWS', 'Amazon/AWS', 'Amazon', 'AWS', 'Amazon Web Services', '亚马逊', '亚马逊 Amazon / AWS'],
  ['中国平安', '中国平安', '中国平安保险集团', 'Ping An'],
  ['平安科技', '平安科技', '平安科技（深圳）有限公司'],
  ['平安证券', '平安证券', '平安证券股份有限公司'],
  ['泰康保险集团', '泰康', '泰康保险', '泰康保险集团股份有限公司'],
];
// The star map never infers a parent/subsidiary relationship. It only shows
// the user's maintained high-priority units in their actual business sector.
const DIRECTORY_GROUPS = [];
const nameKey = name => name.trim().normalize('NFKC').toLocaleLowerCase('en');
const aliasLabels = new Map(DIRECTORY_ALIASES.flatMap(([label, ...aliases]) =>
  [label, ...aliases].map(alias => [nameKey(alias), label])));

export function buildRadarDirectoryView(employers = []) {
  const institutions = new Map();
  for (const original of employers) {
    if (typeof original !== 'string' || !original.trim()) continue;
    const name = original.trim();
    const label = aliasLabels.get(nameKey(name)) || name;
    const key = nameKey(label);
    if (!institutions.has(key)) institutions.set(key, { kind: 'employer', label, aliases: [] });
    const item = institutions.get(key);
    if (!item.aliases.includes(name)) item.aliases.push(name);
  }
  const groups = DIRECTORY_GROUPS.map(group => ({
    label: group.label,
    parent: group.parent ? institutions.get(nameKey(group.parent)) : undefined,
    members: [...institutions.values()].filter(item => group.members.has(item.label)),
  })).filter(group => group.members.length > 1 || (group.parent && group.members.length > 0));
  const groupByMember = new Map(groups.flatMap(group => [...(group.parent ? [group.parent] : []), ...group.members].map(item => [item, group])));
  const entries = [];
  const insertedGroups = new Set();
  for (const item of institutions.values()) {
    const group = groupByMember.get(item);
    if (group) {
      if (!insertedGroups.has(group)) entries.push({ kind: 'group', ...group });
      insertedGroups.add(group);
    } else entries.push(item);
  }
  return { entries, institutionCount: institutions.size, groupCount: groups.length };
}

// The constellation is a category view of maintained priorities, never a scan report.
export function renderRadarConstellation(container, pools, doc = document) {
  const node = (tag, cls, text = '') => {
    const el = doc.createElement(tag); el.className = cls; el.textContent = text; return el;
  };
  container.replaceChildren();
  if (!pools.length) { container.appendChild(node('p', 'star-map-empty', '尚无可展示的高优先级单位；已报名或收藏后会按行业归入星域。')); return; }
  const directories = pools.map(pool => buildRadarDirectoryView(pool.employers || []));
  const shell = node('section', 'star-map-console');
  const header = node('div', 'star-map-telemetry');
  header.append(node('span', '', 'SECTOR ATLAS'), node('span', '', `${pools.length} 星域 · ${directories.reduce((n, directory) => n + directory.entries.length, 0)} 个展示项`));
  const field = node('div', 'star-map-field');
  field.setAttribute('role', 'group'); field.setAttribute('aria-label', '星域导航，查看完整行业名录与高优先级单位');
  for (let i = 0; i < 3; i++) field.appendChild(node('i', `star-map-orbit orbit-${i}`));
  const core = node('div', 'star-map-core');
  core.append(node('span', '', '◎'), node('strong', '', '星域导航'), node('small', '', 'PRIORITY ATLAS'));
  field.appendChild(core);
  const detail = node('section', 'star-map-detail'); detail.setAttribute('aria-live', 'polite');
  const legend = node('div', 'star-map-legend');
  const controls = [];
  function select(index) {
    controls.forEach((pair, i) => pair.forEach(el => el.setAttribute('aria-pressed', String(i === index))));
    const pool = pools[index];
    const title = node('div', 'star-map-detail-heading');
    title.append(node('span', '', String(index + 1).padStart(2, '0')), node('strong', '', pool.name));
    const directory = directories[index];
    const list = node('div', 'star-map-employers');
    function employerLabel(item, tag = 'span') {
      const label = node(tag, '', item.label);
      label.title = `已维护单位写法：${item.aliases.join('、')}`;
      return label;
    }
    directory.entries.forEach(item => {
      if (item.kind !== 'group') { list.appendChild(employerLabel(item)); return; }
      // Native details/summary preserves Enter/Space operation without JS-only controls.
      const group = node('details', 'star-map-employer-group');
      const summary = node('summary', 'star-map-group-toggle');
      summary.append(node('strong', '', item.label), node('small', '', `${item.members.length} 家机构`));
      if (item.parent) summary.title = `已维护单位写法：${item.parent.aliases.join('、')}`;
      const members = node('ul', 'star-map-group-members');
      item.members.forEach(member => members.appendChild(employerLabel(member, 'li')));
      group.append(summary, members);
      list.appendChild(group);
    });
    const count = directory.groupCount
      ? `${directory.entries.length} 个招聘单位`
      : `${directory.institutionCount} 个高优先级单位`;
    detail.replaceChildren(title, node('p', '', pool.focus || ''), node('small', '', `${count} · 不代表当前招聘数量`), list);
  }
  pools.forEach((pool, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / pools.length;
    const radius = index % 2 ? 40 : 31;
    const x = 50 + Math.cos(angle) * radius, y = 50 + Math.sin(angle) * radius;
    const star = node('button', 'star-map-node'); star.type = 'button';
    star.style.left = `${x}%`; star.style.top = `${y}%`;
    const directory = directories[index];
    star.setAttribute('aria-label', `${pool.name}，${directory.institutionCount} 个高优先级单位`);
    star.title = pool.name;
    star.append(node('i', '', '✦'), node('span', '', String(index + 1).padStart(2, '0')));
    const ray = node('i', 'star-map-ray');
    ray.style.width = `${radius}%`; ray.style.transform = `rotate(${angle}rad)`;
    field.append(ray, star);
    const key = node('button', 'star-map-key'); key.type = 'button';
    key.append(node('span', '', String(index + 1).padStart(2, '0')), node('strong', '', pool.name));
    for (const el of [star, key]) el.addEventListener('click', () => select(index));
    controls.push([star, key]); legend.appendChild(key);
  });
  shell.append(header, field, node('p', 'star-map-hint', '点亮星域，查看按行业归类的高优先级单位'), legend, detail);
  container.appendChild(shell); select(0);
}
