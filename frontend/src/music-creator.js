export const MUSIC_CREATION_TEMPLATES = {
  cosmos: {
    label: "星际叙事",
    title: "未命名星图",
    style: "电影氛围",
    mood: "辽阔而克制",
    tempo: 72,
    texture: "空气合成器、低频脉冲、遥远钟声",
    scale: "minor_pentatonic",
    wave: "sine",
  },
  frost: {
    label: "冰晶回声",
    title: "零度回声",
    style: "极简电子",
    mood: "冷静、透明、疏离",
    tempo: 64,
    texture: "玻璃音色、稀疏钢琴、冰层噪声",
    scale: "major_pentatonic",
    wave: "sine",
  },
  aurora: {
    label: "极光漂移",
    title: "极光航线",
    style: "梦幻电子",
    mood: "流动、温柔、未知",
    tempo: 86,
    texture: "柔和琶音、宽阔铺底、微光颗粒",
    scale: "lydian",
    wave: "triangle",
  },
  ember: {
    label: "烈焰脉冲",
    title: "赤色引擎",
    style: "未来节拍",
    mood: "坚定、炽热、向前",
    tempo: 108,
    texture: "脉冲贝斯、金属敲击、上升合成器",
    scale: "minor_pentatonic",
    wave: "sawtooth",
  },
};

export function buildMusicBlueprint({ title, style, mood, tempo, texture, description }) {
  const safeTitle = title?.trim() || "未命名声音世界";
  const safeStyle = style?.trim() || "氛围电子";
  const safeMood = mood?.trim() || "克制而有空间感";
  const safeTempo = Math.min(160, Math.max(48, Number(tempo) || 72));
  const safeTexture = texture?.trim() || "柔和合成器、低频脉冲与空气颗粒";
  const safeDescription = description?.trim() || "从寂静中出现，逐步形成清晰的情绪轨迹。";
  return [
    `作品：${safeTitle}`,
    `方向：以${safeStyle}为核心，整体情绪保持${safeMood}。`,
    `速度：${safeTempo} BPM；保留呼吸感，不要让节奏填满全部空间。`,
    `声音：使用${safeTexture}，建立前景、远景与可辨认的声音标记。`,
    `叙事：${safeDescription}`,
    "结构：引入—展开—转折—余韵；避免模仿任何具体艺人或受版权保护的作品。",
  ].join("\n");
}

const SCALES = {
  minor_pentatonic: [0, 3, 5, 7, 10, 12],
  major_pentatonic: [0, 2, 4, 7, 9, 12],
  lydian: [0, 2, 4, 6, 7, 9, 11, 12],
};

export class BrowserSoundscapeEngine {
  constructor() {
    this.context = null;
    this.master = null;
    this.timer = null;
    this.playing = false;
    this.config = null;
    this.sequenceStep = 0;
  }

  async start(config) {
    await this.destroy();
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error("当前浏览器不支持本地声音合成。");
    this.context = new AudioContext();
    this.master = this.context.createGain();
    this.master.gain.value = Math.min(0.24, Math.max(0, Number(config.volume) || 0.18));
    this.master.connect(this.context.destination);
    this.config = config;
    this.sequenceStep = 0;
    await this.context.resume();
    this.playing = true;
    this.schedulePhrase();
  }

  schedulePhrase() {
    if (!this.context || !this.playing) return;
    const tempo = Math.min(160, Math.max(48, Number(this.config.tempo) || 72));
    const stepDuration = (60 / tempo) * 0.75;
    const scale = SCALES[this.config.scale] || SCALES.minor_pentatonic;
    const startAt = this.context.currentTime + 0.08;
    const phraseLength = 12;
    for (let index = 0; index < phraseLength; index += 1) {
      const scaleIndex = (this.sequenceStep + index * 2 + (index % 3)) % scale.length;
      const octaveOffset = index % 5 === 0 ? -12 : 0;
      const midi = 52 + scale[scaleIndex] + octaveOffset;
      const frequency = 440 * (2 ** ((midi - 69) / 12));
      this.scheduleNote(frequency, startAt + index * stepDuration, stepDuration * 1.65, index);
    }
    this.sequenceStep = (this.sequenceStep + 3) % scale.length;
    const phraseMs = phraseLength * stepDuration * 1000;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.schedulePhrase();
    }, Math.max(500, phraseMs - 180));
  }

  scheduleNote(frequency, startAt, duration, index) {
    const oscillator = this.context.createOscillator();
    const gain = this.context.createGain();
    const filter = this.context.createBiquadFilter();
    oscillator.type = this.config.wave || "sine";
    oscillator.frequency.setValueAtTime(frequency, startAt);
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(900 + (index % 4) * 420, startAt);
    filter.Q.value = 0.6;
    const peak = oscillator.type === "sawtooth" ? 0.025 : 0.045;
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(peak, startAt + Math.min(0.18, duration * 0.25));
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
    oscillator.connect(filter);
    filter.connect(gain);
    gain.connect(this.master);
    oscillator.start(startAt);
    oscillator.stop(startAt + duration + 0.05);
  }

  async pause() {
    if (!this.context) return;
    await this.context.suspend();
    this.playing = false;
  }

  async resume() {
    if (!this.context) throw new Error("请先生成一个声音世界。");
    await this.context.resume();
    this.playing = true;
    if (!this.timer) this.schedulePhrase();
  }

  setVolume(value) {
    if (!this.master || !this.context) return;
    const volume = Math.min(0.24, Math.max(0, Number(value) || 0));
    this.master.gain.setTargetAtTime(volume, this.context.currentTime, 0.04);
  }

  async destroy() {
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = null;
    this.playing = false;
    if (this.context) {
      try { await this.context.close(); } catch (_) {}
    }
    this.context = null;
    this.master = null;
  }
}

export const soundscapeEngine = new BrowserSoundscapeEngine();
