import test from "node:test";
import assert from "node:assert/strict";

import { MusicShuffleQueue, createShuffleQueue, fisherYatesShuffle } from "./music-provider.js";
import { buildMusicBlueprint } from "./music-creator.js";

test("Fisher–Yates keeps every track exactly once", () => {
  const tracks = ["a", "b", "c", "d", "e"];
  const shuffled = fisherYatesShuffle(tracks, () => 0.31);
  assert.deepEqual([...shuffled].sort(), tracks);
  assert.equal(new Set(shuffled).size, tracks.length);
});

test("a new shuffled round never starts with the previous round's last track", () => {
  const queue = createShuffleQueue(["a", "b", "c", "d"], "b", () => 0);
  assert.notEqual(queue[0], "b");
  assert.equal(new Set(queue).size, 4);
});

test("shuffle queue does not repeat within a round", () => {
  const queue = new MusicShuffleQueue(() => 0);
  queue.setTracks(["a", "b", "c", "d"], { shuffle: true });
  const round = [queue.next(), queue.next(), queue.next(), queue.next()];
  assert.equal(new Set(round).size, 4);
  const previousRoundLast = round.at(-1);
  assert.notEqual(queue.next(), previousRoundLast);
});

test("previous follows playback history and next returns to the interrupted track", () => {
  const queue = new MusicShuffleQueue();
  queue.setTracks(["a", "b", "c"], { shuffle: false });
  assert.equal(queue.next(), "a");
  assert.equal(queue.next(), "b");
  assert.equal(queue.previous(), "a");
  assert.equal(queue.next(), "b");
});

test("creation blueprint preserves the user's setting without asking for artist imitation", () => {
  const blueprint = buildMusicBlueprint({
    title: "远岸信号",
    style: "电影电子",
    mood: "辽阔",
    tempo: 96,
    texture: "玻璃音色与低频脉冲",
    description: "一束信号穿过无人海面。",
  });
  assert.match(blueprint, /远岸信号/);
  assert.match(blueprint, /96 BPM/);
  assert.match(blueprint, /一束信号穿过无人海面/);
  assert.match(blueprint, /避免模仿任何具体艺人/);
});
