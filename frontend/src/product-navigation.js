export const PRODUCT_NAV_ITEMS = Object.freeze([
  { id: "legal", symbol: "§", label: "寒冰域", english: "FROST" },
  { id: "general", symbol: "✦", label: "极光域", english: "AURORA" },
  { id: "finance", symbol: "↗", label: "烈火域", english: "EMBER" },
  { id: "recruitment", symbol: "◉", label: "未来雷达", english: "FUTURE RADAR", dialogId: "recruitment-dialog" },
  { id: "forge", symbol: "＋", label: "造界", english: "WORLD FORGE", dialogId: "studio-dialog" },
  { id: "resonance", symbol: "≈", label: "共振", english: "RESONANCE", dialogId: "resonance-dialog" },
  { id: "trace", symbol: "◎", label: "溯源透镜", english: "TRACE LENS", dialogId: "trace-dialog" },
  { id: "music", symbol: "♫", label: "八度空间", english: "MUSIC DIMENSION", dialogId: "music-dimension-dialog" },
  { id: "photon", symbol: "◫", label: "光子魅影", english: "PHOTON PROJECTION", dialogId: "photon-projection-dialog" },
  { id: "oblivion", symbol: "◷", label: "遗忘史诗", english: "OBLIVION ARCHIVE", dialogId: "oblivion-archive-dialog" },
]);

const PRODUCT_IDS = new Set(PRODUCT_NAV_ITEMS.map((item) => item.id));

export function normalizeProductId(product) {
  return PRODUCT_IDS.has(product) ? product : null;
}

export function productDialogId(product) {
  return PRODUCT_NAV_ITEMS.find((item) => item.id === product)?.dialogId || null;
}

export function productDialogIdsToClose(nextProduct = null) {
  const retainedDialogId = productDialogId(nextProduct);
  return PRODUCT_NAV_ITEMS
    .map((item) => item.dialogId)
    .filter((dialogId) => dialogId && dialogId !== retainedDialogId);
}

export function resolveStartupProduct({ queuedProductLaunch = null, pendingLaunch = null } = {}) {
  return normalizeProductId(queuedProductLaunch) || normalizeProductId(pendingLaunch);
}
