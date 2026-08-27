export function resolveStartupProduct({ queuedProductLaunch = null, pendingLaunch = null } = {}) {
  return queuedProductLaunch || pendingLaunch || null;
}
