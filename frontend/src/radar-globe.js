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
  const boundaries = new THREE.Group(); scene.add(boundaries);
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
    if (hit) onSelect(hit.object.userData.place);
  });
  let active = false, motion = true;
  renderer.setAnimationLoop(time => { if (!active) return; controls.update(); markers.children.forEach(marker => marker.scale.setScalar(motion ? 1 + .15 * Math.sin(time * .003) : 1)); renderer.render(scene, camera); });
  return {
    update(features, places, enabled, animate) {
      active = enabled; motion = animate; host.hidden = !enabled;
      if (!enabled) return;
      disposeGroup(boundaries); disposeGroup(markers);
      for (const feature of features) {
        const polygons = feature.geometry.type === 'Polygon' ? [feature.geometry.coordinates] : feature.geometry.type === 'MultiPolygon' ? feature.geometry.coordinates : [];
        for (const polygon of polygons) for (const ring of polygon) boundaries.add(line(ring.map(([lon, lat]) => globePoint(lon, lat, 1.005)), 0x39d9eb, .85));
      }
      for (const place of places) {
        const marker = new THREE.Mesh(new THREE.SphereGeometry(.012 + Math.min(place.count, 20) * .0008, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffd466 }));
        marker.position.copy(globePoint(place.longitude, place.latitude, 1.018)); marker.userData.place = place; markers.add(marker);
      }
      resize();
    },
    reset() { controls.reset(); },
    destroy() { observer.disconnect(); renderer.setAnimationLoop(null); controls.dispose(); scene.traverse(object => { object.geometry?.dispose(); object.material?.dispose(); }); renderer.dispose(); host.replaceChildren(); }
  };
}
