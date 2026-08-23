import test from "node:test";
import assert from "node:assert/strict";

import { buildMusicBlueprint, normalizeMusicCreation } from "./music-creator.js";

test("creation blueprint preserves the setting without asking for artist imitation", () => {
  const blueprint = buildMusicBlueprint({
    title: "远岸信号",
    style: "电影电子",
    mood: "辽阔",
    tempo: 96,
    texture: "玻璃音色与低频脉冲",
    description: "一束信号穿过无人海面。",
    instruments: ["piano", "strings", "bass"],
    vocalProfile: "warm",
  });
  assert.match(blueprint, /远岸信号/);
  assert.match(blueprint, /96 BPM/);
  assert.match(blueprint, /一束信号穿过无人海面/);
  assert.match(blueprint, /星穹钢琴、弦乐群、深空贝斯/);
  assert.match(blueprint, /温暖中声线/);
  assert.match(blueprint, /不模仿任何具体艺人/);
});

test("creation settings reject unknown instruments and never require an external music app", () => {
  const creation = normalizeMusicCreation({
    tempo: 999,
    instruments: ["piano", "unknown", "piano", "drums"],
    vocalProfile: "crystal",
  });
  assert.equal(creation.tempo, 160);
  assert.deepEqual(creation.instruments, ["piano", "drums"]);
  assert.equal(creation.vocalProfile, "crystal");
});
