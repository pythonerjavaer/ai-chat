import test from "node:test";
import assert from "node:assert/strict";

import { resolveStartupProduct } from "./product-navigation.js";

test("a previously active product does not bypass the product compass on startup", () => {
  assert.equal(resolveStartupProduct({ activeProduct: "general" }), null);
  assert.equal(resolveStartupProduct({ activeProduct: "recruitment" }), null);
});

test("an explicit product choice made before authentication is resumed", () => {
  assert.equal(resolveStartupProduct({ pendingLaunch: "recruitment" }), "recruitment");
  assert.equal(
    resolveStartupProduct({ queuedProductLaunch: "music", pendingLaunch: "recruitment" }),
    "music",
  );
});
