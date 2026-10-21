import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { minorUnits } from "./pulse-settlement.js";

test("settlement converts monetary input without floating-point rounding", () => {
  assert.equal(minorUnits("0.29"), 29);
  assert.equal(minorUnits("1234.50"), 123450);
  assert.equal(minorUnits(" 10.1 "), 1010);
  for (const value of ["0", "-1", "1.005", "NaN", "Infinity", "1e3", "1000001", "", "<img>"]) {
    assert.throws(() => minorUnits(value));
  }
});

test("settlement desk uses real authenticated commands, source evidence and safe DOM", () => {
  const source = readFileSync(new URL("./pulse-settlement.js", import.meta.url), "utf8");
  const entry = readFileSync(new URL("./product-domains.js", import.meta.url), "utf8");
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(source, /request_id: pending.request_id/);
  assert.match(source, /pending\.signature !== signature/);
  assert.match(source, /confirmed = true/);
  assert.match(source, /epoch !== generation \|\| isDemo\(\)/);
  assert.match(source, /source_document|journal_id/);
  assert.match(source, /deposit-offset/);
  assert.match(source, /allocations\/\$\{encodeURIComponent\(entry.id\)\}\/reverse/);
  assert.match(source, /entry\.reversed_at/);
  assert.match(source, /reversal_journal_id/);
  assert.doesNotMatch(source, /innerHTML|setInterval|localStorage|bank-imports/);
  assert.match(entry, /settlementDesk.reset\(\)/);
  assert.match(entry, /api\(`\/pulse\/journals\/\$\{encodeURIComponent\(id\)\}`\)/);
  assert.match(html, /data-domain-tab="settlement"/);
  assert.match(html, /id="pulse-settlement-desk"/);
});

test("deposit uncertainty is connected to the business screen and gated by server status", () => {
  const entry = readFileSync(new URL("./product-domains.js", import.meta.url), "utf8");
  assert.match(entry, /uncertainty\.status === "ok" && uncertainty\.scenarios\.find/);
  assert.match(entry, /coverage_ratio_ci95/);
  assert.match(entry, /mean_uncovered_per_loss_order_cents_ci95/);
  assert.match(entry, /uncertainty\.message/);
  assert.match(entry, /uncertainty\.interpretation/);
});
