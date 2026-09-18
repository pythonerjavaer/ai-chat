// The constellation is a view of the configured directory, never invented jobs.
export function renderRadarConstellation(container, pools, doc = document) {
  const node = (tag, cls, text = '') => {
    const el = doc.createElement(tag); el.className = cls; el.textContent = text; return el;
  };
  container.replaceChildren();
  if (!pools.length) { container.appendChild(node('p', 'star-map-empty', '尚未配置机构名录。')); return; }
  const shell = node('section', 'star-map-console');
  const header = node('div', 'star-map-telemetry');
  header.append(node('span', '', 'SECTOR ATLAS'), node('span', '', `${pools.length} 星域 · ${pools.reduce((n, p) => n + (p.employers || []).length, 0)} 名录条目`));
  const field = node('div', 'star-map-field');
  field.setAttribute('role', 'group'); field.setAttribute('aria-label', '雷达星图，选择星域查看机构名录');
  for (let i = 0; i < 3; i++) field.appendChild(node('i', `star-map-orbit orbit-${i}`));
  const core = node('div', 'star-map-core');
  core.append(node('span', '', '◎'), node('strong', '', '星域导航'), node('small', '', 'DIRECTORY'));
  field.appendChild(core);
  const detail = node('section', 'star-map-detail'); detail.setAttribute('aria-live', 'polite');
  const legend = node('div', 'star-map-legend');
  const controls = [];
  function select(index) {
    controls.forEach((pair, i) => pair.forEach(el => el.setAttribute('aria-pressed', String(i === index))));
    const pool = pools[index];
    const title = node('div', 'star-map-detail-heading');
    title.append(node('span', '', String(index + 1).padStart(2, '0')), node('strong', '', pool.name));
    const list = node('div', 'star-map-employers');
    (pool.employers || []).forEach(name => list.appendChild(node('span', '', name)));
    detail.replaceChildren(title, node('p', '', pool.focus || ''), node('small', '', `${(pool.employers || []).length} 个名录机构 · 不代表当前招聘数量`), list);
  }
  pools.forEach((pool, index) => {
    const angle = -Math.PI / 2 + index * Math.PI * 2 / pools.length;
    const radius = index % 2 ? 40 : 31;
    const x = 50 + Math.cos(angle) * radius, y = 50 + Math.sin(angle) * radius;
    const star = node('button', 'star-map-node'); star.type = 'button';
    star.style.left = `${x}%`; star.style.top = `${y}%`;
    star.setAttribute('aria-label', `${pool.name}，${(pool.employers || []).length} 个名录机构`);
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
  shell.append(header, field, node('p', 'star-map-hint', '点亮星域，展开真实机构名录'), legend, detail);
  container.appendChild(shell); select(0);
}
