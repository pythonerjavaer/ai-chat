// Presentation aliases mirror the explicit names in recruitment_directory.py.
// These do not change recruitment identities or merge subsidiaries in the data.
const DIRECTORY_ALIASES = [
  ['Deloitte 德勤', '德勤', 'Deloitte'],
  ['PwC 普华永道', '普华永道', 'PwC', 'PricewaterhouseCoopers'],
  ['EY 安永', '安永', 'EY', 'Ernst & Young'],
  ['KPMG 毕马威', '毕马威', 'KPMG'],
  ['Bain 贝恩', '贝恩', 'Bain', 'Bain & Company'],
  ['Kearney 科尔尼', 'Kearney 科尔尼', '科尔尼', 'Kearney'],
  ['McKinsey 麦肯锡', '麦肯锡', 'McKinsey', 'McKinsey & Company'],
  ['BCG 波士顿咨询', '波士顿咨询', 'BCG', 'Boston Consulting Group'],
  ['Roland Berger 罗兰贝格', '罗兰贝格', 'Roland Berger'],
  ['Oliver Wyman 奥纬咨询', 'Oliver Wyman', '奥纬咨询', '奥纬'],
  ['L.E.K. Consulting 艾意凯咨询', 'L.E.K. Consulting', 'L.E.K.', 'LEK Consulting', '艾意凯咨询'],
  ['Accenture 埃森哲', '埃森哲', 'Accenture'],
  ['Amazon 亚马逊', 'Amazon / AWS 亚马逊', 'Amazon/AWS', 'Amazon', 'AWS', 'Amazon Web Services', '亚马逊', '亚马逊 Amazon / AWS'],
  ['Microsoft 微软', 'Microsoft', '微软'], ['Google 谷歌', 'Google', '谷歌'],
  ['Apple 苹果', 'Apple', '苹果'], ['NVIDIA 英伟达', 'NVIDIA', '英伟达'],
  ['J.P. Morgan 摩根大通', 'J.P. Morgan', '摩根大通', 'JPMorgan'],
  ['Goldman Sachs 高盛', 'Goldman Sachs', '高盛'],
  ['Morgan Stanley 摩根士丹利', 'Morgan Stanley', '摩根士丹利'],
  ['UBS 瑞银', 'UBS', '瑞银', '瑞银 UBS', '瑞士银行'], ['Citi 花旗', 'Citi', '花旗', '花旗银行', 'Citigroup'],
  ['HSBC 汇丰', 'HSBC', '汇丰', '汇丰银行'],
  ['Standard Chartered 渣打银行', 'Standard Chartered', '渣打', '渣打银行'],
  ['DBS 星展银行', 'DBS', '星展', '星展银行'],
  ['Hang Seng Bank 恒生银行', 'Hang Seng Bank', '恒生银行'],
  ['Deutsche Bank 德意志银行', 'Deutsche Bank', '德意志银行'],
  ['Barclays 巴克莱银行', 'Barclays', '巴克莱', '巴克莱银行'],
  ['BlackRock 贝莱德', 'BlackRock', '贝莱德', '布莱德', 'BlackRock 布莱德'],
  ['DWS 德意志资管', 'DWS', '德意志资管', '德意志资产管理'],
  ['Nomura 野村', 'Nomura', '野村', '野村证券'],
  ['P&G 宝洁', '宝洁', 'P&G', 'Procter & Gamble'],
  ['Unilever 联合利华', '联合利华', 'Unilever'],
  ["L'Oréal 欧莱雅", '欧莱雅', "L'Oréal", "L'Oreal", 'Loreal'],
  ['Nestlé 雀巢', '雀巢', 'Nestlé', 'Nestle'],
  ['Mars 玛氏', '玛氏', 'Mars'],
  ['Coca-Cola 可口可乐', '可口可乐', 'Coca-Cola', 'Coca Cola'],
  ['PepsiCo 百事', '百事', 'PepsiCo', 'Pepsi'],
  ['Nike 耐克', '耐克', 'Nike'],
  ['Danone 达能', '达能', 'Danone'],
  ['Mondelēz 亿滋', '亿滋', 'Mondelēz', 'Mondelez'],
  ['Adidas 阿迪达斯', '阿迪达斯', 'Adidas'],
  ['IKEA 宜家', '宜家', 'IKEA'],
  ['Johnson & Johnson 强生', '强生', 'Johnson & Johnson', 'J&J'],
  ['Starbucks 星巴克', '星巴克', 'Starbucks'],
  ["McDonald's 麦当劳", '麦当劳', "McDonald's", 'McDonalds'],
  ['WTW 韦莱韬悦', 'WTW', 'Willis Towers Watson', '韦莱韬悦'],
  ['Mercer 美世', 'Mercer', '美世'],
  ['Aon 怡安', 'Aon', '怡安', '怡安翰威特', 'Aon Hewitt'],
  ['Meta Facebook', 'Meta', 'Facebook', 'Facebook/Meta'],
  ['Intel 英特尔', 'Intel', '英特尔'], ['Cisco 思科', 'Cisco', '思科'],
  ['SAP 思爱普', 'SAP', '思爱普'], ['Oracle 甲骨文', 'Oracle', '甲骨文'],
  ['Tesla 特斯拉', 'Tesla', '特斯拉'], ['BMW 宝马集团', 'BMW', '宝马', '宝马集团'],
  ['Siemens 西门子', 'Siemens', '西门子'], ['GE 通用电气', 'GE', 'General Electric', '通用电气'],
  ['Bosch 博世', 'Bosch', '博世'], ['Daimler 戴姆勒', 'Daimler', '戴姆勒'],
  ['Pfizer 辉瑞', 'Pfizer', '辉瑞'], ['Novartis 诺华', 'Novartis', '诺华'],
  ['Bayer 拜耳', 'Bayer', '拜耳'], ['MSD 默沙东', 'MSD', 'Merck Sharp & Dohme', '默沙东'],
  ['Hillhouse 高瓴资本', 'Hillhouse', '高瓴', '高瓴资本'],
  ['HongShan 红杉中国', 'HongShan', '红杉中国', '红杉资本'],
  ['CDH 鼎晖投资', 'CDH', '鼎晖', '鼎晖投资'],
  ['Hony Capital 弘毅投资', 'Hony Capital', '弘毅', '弘毅投资'],
  ['Carlyle 凯雷投资集团', 'Carlyle', '凯雷', '凯雷投资集团'],
  ['Blackstone 黑石集团', 'Blackstone', '黑石', '黑石集团'],
  ['Baring Private Equity Asia 霸菱亚洲', 'Baring Private Equity Asia', 'BPEA', '霸菱亚洲'],
  ['Warburg Pincus 华平投资集团', 'Warburg Pincus', '华平投资', '华平投资集团'],
  ['Tiger Global 老虎环球基金', 'Tiger Global', '老虎环球', '老虎环球基金'],
  ['Fidelity 富达', 'Fidelity', '富达', '富达基金'],
  ['中信证券', '中信证券股份有限公司', 'CITIC Securities', 'CITICS'],
  ['广发证券', '广发证券股份有限公司', 'GF Securities'],
  ['申万宏源', '申万宏源证券', '申万宏源证券有限公司', 'Shenwan Hongyuan'],
  ['永赢基金', '永赢基金管理有限公司', 'Yong Win Fund', 'Yongying Fund'],
  ['恒生指数', '恒生指数有限公司', 'Hang Seng Indexes'],
  ['恒生电子', '恒生电子股份有限公司', 'Hundsun'],
  ['中国电子科技集团（中国电科）', '中国电子科技集团', '中国电科'],
  ['大疆 DJI', '大疆', 'DJI', '大疆创新', '深圳市大疆创新科技'],
  ['中芯国际 SMIC', '中芯国际', 'SMIC'],
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

// Normalize older API records to the same bilingual label used by the map.
export function normalizeRadarEmployerLabel(value) {
  if (typeof value !== 'string') return value;
  return aliasLabels.get(nameKey(value)) || value;
}

export function buildRadarDirectoryView(employers = []) {
  const institutions = new Map();
  for (const original of employers) {
    if (typeof original !== 'string' || !original.trim()) continue;
    const name = original.trim();
    const label = normalizeRadarEmployerLabel(name);
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
