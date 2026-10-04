import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

export function globePoint(longitude, latitude, radius = 1) {
  const lon = longitude * Math.PI / 180, lat = latitude * Math.PI / 180;
  return new THREE.Vector3(radius * Math.cos(lat) * Math.cos(lon), radius * Math.sin(lat), -radius * Math.cos(lat) * Math.sin(lon));
}

export function createRadarGlobe(host, onSelect) {
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, 1, .01, 100);
  camera.position.copy(globePoint(105, 32, 3.3));
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.domElement.setAttribute('aria-label', '三维招聘地球：拖拽自由旋转，滚轮缩放，点击发光地点查看岗位');
  host.append(renderer.domElement);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true; controls.enablePan = false;
  controls.minDistance = 1.3; controls.maxDistance = 5; controls.saveState();
  scene.add(new THREE.AmbientLight(0xffffff, 2));
  const globe = new THREE.Mesh(new THREE.SphereGeometry(1, 80, 48), new THREE.MeshPhongMaterial({ color: 0x071e36, shininess: 70, transparent: true, opacity: .96 }));
  scene.add(globe);
  const light = new THREE.DirectionalLight(0x62dfff, 3); light.position.set(3, 4, 2); scene.add(light);
  const lines = new THREE.Group(), markers = new THREE.Group(); scene.add(lines, markers);
  const line = (points, color, opacity = .6) => new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  for (let lat = -60; lat <= 60; lat += 20) lines.add(line(Array.from({ length: 181 }, (_, i) => globePoint(i * 2, lat, 1.002)), 0x237795, .25));
  for (let lon = 0; lon < 360; lon += 20) lines.add(line(Array.from({ length: 91 }, (_, i) => globePoint(lon, i * 2 - 90, 1.002)), 0x237795, .25));
  const boundaries = new THREE.Group(), relations = new THREE.Group(), labels = new THREE.Group(); scene.add(boundaries, relations, labels);
  const tooltip = document.createElement('div'); tooltip.className = 'radar-globe-tooltip'; tooltip.hidden = true; host.append(tooltip);
  const adminLabels = document.createElement('div'); adminLabels.className = 'radar-globe-admin-labels'; host.append(adminLabels);
  let administrative = [];
  function labeledNode(node, position, color, radius = .018) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 12, 8), new THREE.MeshBasicMaterial({color}));
    mesh.position.copy(position); mesh.userData.node = node; mesh.userData.radius = radius; markers.add(mesh);
    if (node.kind !== 'location') return mesh;
    const canvas = document.createElement('canvas'); canvas.width = 512; canvas.height = 64;
    const context = canvas.getContext('2d'); context.fillStyle = '#071e36'; context.fillRect(0, 0, 512, 64);
    context.font = '36px sans-serif'; context.fillStyle = '#e5faff'; context.fillText(node.label.slice(0, 16), 10, 44);
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({map: new THREE.CanvasTexture(canvas), depthTest: true}));
    sprite.position.copy(position).add(new THREE.Vector3(0, .045, 0)); sprite.scale.set(.24, .03, 1); labels.add(sprite);
    return mesh;
  }
  function connect(a, b, color) {
    const mid = a.clone().add(b).multiplyScalar(.5); mid.normalize().multiplyScalar(Math.max(a.length(), b.length()) + .045);
    relations.add(line(new THREE.QuadraticBezierCurve3(a, mid, b).getPoints(20), color, .65));
  }
  const disposeGroup = group => { for (const child of [...group.children]) { child.geometry?.dispose(); child.material?.map?.dispose(); child.material?.dispose(); group.remove(child); } };
  const resize = () => { const width = host.clientWidth || 800, height = Math.max(420, Math.min(620, width * .66)); renderer.setSize(width, height); camera.aspect = width / height; camera.updateProjectionMatrix(); };
  const observer = new ResizeObserver(resize); observer.observe(host); resize();
  const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2(); let down = null;
  renderer.domElement.addEventListener('pointerdown', event => { down = [event.clientX, event.clientY]; });
  renderer.domElement.addEventListener('pointerup', event => {
    if (!down || Math.hypot(event.clientX - down[0], event.clientY - down[1]) > 5) return;
    const rect = renderer.domElement.getBoundingClientRect(); pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(markers.children).find(hit => hit.distance < (raycaster.intersectObject(globe)[0]?.distance ?? Infinity));
    if (hit) onSelect(hit.object.userData.node);
  });
  renderer.domElement.addEventListener('pointermove', event => {
    const rect = renderer.domElement.getBoundingClientRect(); pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const hit = raycaster.intersectObjects(markers.children).find(hit => hit.distance < (raycaster.intersectObject(globe)[0]?.distance ?? Infinity));
    tooltip.hidden = !hit; renderer.domElement.style.cursor = hit ? 'pointer' : 'grab';
    if (hit) tooltip.textContent = hit.object.userData.node.label;
  });
  let active = false, motion = true;
  renderer.setAnimationLoop(time => {
    if (!active) return;
    controls.update();
    // Keep markers readable without covering cities when the camera zooms in.
    const height = renderer.domElement.clientHeight || 620;
    markers.children.forEach(marker => {
      const pixels = marker.userData.node.kind === 'location' ? 3.5 : 3;
      const worldPerPixel = 2 * camera.position.distanceTo(marker.position) * Math.tan(camera.fov * Math.PI / 360) / height;
      const pulse = motion ? 1 + .06 * Math.sin(time * .003) : 1;
      marker.scale.setScalar(pixels * worldPerPixel / marker.userData.radius * pulse);
    });
    renderer.render(scene, camera);
    const occupied = [];
    for (const {position, button} of administrative) {
      const normal = position.clone().normalize(), towardCamera = camera.position.clone().sub(position).normalize();
      const projected = position.clone().project(camera);
      const x = (projected.x + 1) * .5 * renderer.domElement.clientWidth, y = (1 - projected.y) * .5 * height;
      const width = Math.max(50, button.textContent.length * 13), box = {x, y, width};
      const visible = normal.dot(towardCamera) > .08 && Math.abs(projected.x) < .96 && Math.abs(projected.y) < .96
        && !occupied.some(other => Math.abs(other.x - x) < (other.width + width) / 2 + 4 && Math.abs(other.y - y) < 25);
      button.hidden = !visible;
      if (visible) { occupied.push(box); button.style.left = `${x}px`; button.style.top = `${y}px`; }
    }
  });
  return {
    update(features, places, enabled, animate, jobs = []) {
      active = enabled; motion = animate; host.hidden = !enabled;
      if (!enabled) return;
      disposeGroup(boundaries); disposeGroup(markers); disposeGroup(relations); disposeGroup(labels);
      adminLabels.replaceChildren(); administrative = [];
      for (const feature of features) {
        const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.type === 'MultiPolygon' ? feature.geometry.coordinates : [];
        for (const polygon of polygons) for (const ring of polygon) boundaries.add(line(ring.map(([lon, lat]) => globePoint(lon, lat, 1.005)), 0x39d9eb, .85));
        if (feature.mapContext || feature.properties.level === 'auxiliary') continue;
        const center = feature.properties.center || (feature.geometry.type === 'Point' ? feature.geometry.coordinates : null);
        if (center) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = feature.properties.name;
          button.setAttribute('aria-label', `${feature.properties.name}，查看下级行政区`);
          button.addEventListener('click', () => onSelect({kind: 'administrative', feature}));
          adminLabels.append(button); administrative.push({position: globePoint(center[0], center[1], 1.01), button});
        }
      }
      for (const place of places) {
        const anchor = globePoint(place.longitude, place.latitude, 1.018);
        labeledNode({id: `location:${place.id}`, kind: 'location', label: `${place.name} · ${place.count} 岗位`, place}, anchor, 0xffd466);
        const localJobs = jobs.filter(job => job.places.some(p => p.id === place.id));
        const companies = [...new Map(localJobs.map(job => [job.employerId, job])).values()];
        // The satellites express recruitment relationships, not office coordinates.
        for (const [i, company] of companies.slice(0, 12).entries()) {
          if (markers.children.length >= 240) break;
          const position = globePoint(place.longitude + (i - (Math.min(companies.length, 12) - 1) / 2) * 2, place.latitude, 1.10);
          labeledNode({id: company.employerId, kind: 'employer', label: company.employer}, position, 0x65e6fa);
          connect(anchor, position, 0x65e6fa);
          for (const [j, job] of localJobs.filter(job => job.employerId === company.employerId).slice(0, 8).entries()) {
            if (markers.children.length >= 240) break;
            const jobPosition = globePoint(place.longitude + (i - (Math.min(companies.length, 12) - 1) / 2) * 2 + (j - 3) * .6, place.latitude + 2, 1.18);
            labeledNode({id: `opportunity:${job.id}`, kind: 'opportunity', label: job.title}, jobPosition, 0xb9a3ff, .014); connect(position, jobPosition, 0xb9a3ff);
            if (jobs.length <= 8) for (const [k, skill] of job.skills.slice(0, 6).entries()) {
              const skillPosition = globePoint(place.longitude + (k - 2.5) * 1.2, place.latitude + 4, 1.26);
              labeledNode({id: `skill:${skill.toLowerCase()}`, kind: 'skill', label: skill}, skillPosition, 0x67f4ab, .01); connect(jobPosition, skillPosition, 0x67f4ab);
            }
          }
        }
      }
      resize();
    },
    reset() { controls.reset(); },
    focus(place) { camera.position.copy(globePoint(place.longitude, place.latitude, place.level === 'district' ? 1.3 : place.level === 'city' ? 1.4 : 1.7)); controls.update(); },
    destroy() { observer.disconnect(); renderer.setAnimationLoop(null); controls.dispose(); scene.traverse(object => { object.geometry?.dispose(); object.material?.dispose(); }); renderer.dispose(); host.replaceChildren(); }
  };
}
