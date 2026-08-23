export const MUSIC_INSTRUMENTS = {
  piano: { label: "星穹钢琴", wave: "triangle", role: "melody", octave: 0, peak: 0.052, release: 1.5 },
  strings: { label: "弦乐群", wave: "sawtooth", role: "pad", octave: 0, peak: 0.022, release: 2.7 },
  synth: { label: "空间合成器", wave: "sine", role: "melody", octave: 12, peak: 0.036, release: 1.8 },
  guitar: { label: "脉冲吉他", wave: "triangle", role: "rhythm", octave: 0, peak: 0.042, release: 0.65 },
  bass: { label: "深空贝斯", wave: "sine", role: "bass", octave: -12, peak: 0.065, release: 1.15 },
  drums: { label: "电子鼓组", wave: "sine", role: "drums", octave: -24, peak: 0.08, release: 0.22 },
  flute: { label: "气流长笛", wave: "sine", role: "melody", octave: 12, peak: 0.034, release: 1.2 },
  bells: { label: "晶体钟琴", wave: "sine", role: "accent", octave: 24, peak: 0.032, release: 1.9 },
};

export const MUSIC_VOCAL_PROFILES = {
  none: { label: "纯器乐", description: "不加入人声层" },
  crystal: { label: "晶透高声线", description: "明亮、轻盈、具有空气感", formants: [820, 1320], octave: 12 },
  warm: { label: "温暖中声线", description: "柔和、亲近、叙事感", formants: [640, 1120], octave: 0 },
  deep: { label: "深沉低声线", description: "低沉、稳定、具有空间纵深", formants: [420, 850], octave: -12 },
  airy: { label: "雾化气声线", description: "朦胧、疏离、像远处的回声", formants: [980, 1780], octave: 12 },
  choir: { label: "星云合唱层", description: "多层、宽阔、具有仪式感", formants: [560, 1480], octave: 0 },
};

export const MUSIC_CREATION_TEMPLATES = {
  cosmos: {
    label: "星际叙事", title: "未命名星图", style: "电影氛围", mood: "辽阔而克制", tempo: 72,
    texture: "空气合成器、低频脉冲、遥远钟声", scale: "minor_pentatonic",
    instruments: ["piano", "strings", "bass", "bells"], vocalProfile: "none",
  },
  frost: {
    label: "冰晶回声", title: "零度回声", style: "极简电子", mood: "冷静、透明、疏离", tempo: 64,
    texture: "玻璃音色、稀疏钢琴、冰层噪声", scale: "major_pentatonic",
    instruments: ["piano", "flute", "bells"], vocalProfile: "airy",
  },
  aurora: {
    label: "极光漂移", title: "极光航线", style: "梦幻电子", mood: "流动、温柔、未知", tempo: 86,
    texture: "柔和琶音、宽阔铺底、微光颗粒", scale: "lydian",
    instruments: ["synth", "strings", "bass", "drums"], vocalProfile: "crystal",
  },
  ember: {
    label: "烈焰脉冲", title: "赤色引擎", style: "未来节拍", mood: "坚定、炽热、向前", tempo: 108,
    texture: "脉冲贝斯、金属敲击、上升合成器", scale: "minor_pentatonic",
    instruments: ["guitar", "synth", "bass", "drums"], vocalProfile: "deep",
  },
};

const SCALES = {
  minor_pentatonic: [0, 3, 5, 7, 10, 12],
  major_pentatonic: [0, 2, 4, 7, 9, 12],
  lydian: [0, 2, 4, 6, 7, 9, 11, 12],
};

export function normalizeMusicCreation(creation = {}) {
  const instruments = [...new Set((creation.instruments || []).filter((id) => id in MUSIC_INSTRUMENTS))];
  const vocalProfile = creation.vocalProfile in MUSIC_VOCAL_PROFILES ? creation.vocalProfile : "none";
  return {
    ...creation,
    tempo: Math.min(160, Math.max(48, Number(creation.tempo) || 72)),
    scale: creation.scale in SCALES ? creation.scale : "minor_pentatonic",
    instruments: instruments.length ? instruments : ["piano", "synth", "bass"],
    vocalProfile,
  };
}

export function buildMusicBlueprint(creation) {
  const normalized = normalizeMusicCreation(creation);
  const safeTitle = normalized.title?.trim() || "未命名声音世界";
  const safeStyle = normalized.style?.trim() || "氛围电子";
  const safeMood = normalized.mood?.trim() || "克制而有空间感";
  const safeTexture = normalized.texture?.trim() || "柔和合成器、低频脉冲与空气颗粒";
  const safeDescription = normalized.description?.trim() || "从寂静中出现，逐步形成清晰的情绪轨迹。";
  const instrumentLabels = normalized.instruments.map((id) => MUSIC_INSTRUMENTS[id].label).join("、");
  const voice = MUSIC_VOCAL_PROFILES[normalized.vocalProfile];
  return [
    `作品：${safeTitle}`,
    `方向：以${safeStyle}为核心，整体情绪保持${safeMood}。`,
    `速度：${normalized.tempo} BPM；保留呼吸感，不要让节奏填满全部空间。`,
    `编制：${instrumentLabels}；以${safeTexture}建立前景、远景与可辨认的声音标记。`,
    `声线：${voice.label}；${voice.description}。`,
    `叙事：${safeDescription}`,
    "结构：引入—展开—转折—余韵；保持原创，不模仿任何具体艺人或受版权保护的作品。",
  ].join("\n");
}

function midiToFrequency(midi) {
  return 440 * (2 ** ((midi - 69) / 12));
}

export class BrowserSoundscapeEngine {
  constructor() {
    this.context = null;
    this.master = null;
    this.timer = null;
    this.playing = false;
    this.config = null;
    this.sequenceStep = 0;
    this.noiseBuffer = null;
  }

  async start(config) {
    await this.destroy();
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) throw new Error("当前浏览器不支持本地声音合成。");
    this.context = new AudioContext();
    this.master = this.context.createGain();
    this.master.gain.value = Math.min(0.32, Math.max(0, Number(config.volume) || 0.18));
    this.master.connect(this.context.destination);
    this.config = normalizeMusicCreation(config);
    this.sequenceStep = 0;
    this.noiseBuffer = this.createNoiseBuffer();
    await this.context.resume();
    this.playing = true;
    this.schedulePhrase();
  }

  createNoiseBuffer() {
    const length = Math.max(1, Math.floor(this.context.sampleRate * 0.2));
    const buffer = this.context.createBuffer(1, length, this.context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let index = 0; index < length; index += 1) data[index] = Math.random() * 2 - 1;
    return buffer;
  }

  schedulePhrase() {
    if (!this.context || !this.playing) return;
    const stepDuration = (60 / this.config.tempo) * 0.5;
    const scale = SCALES[this.config.scale];
    const startAt = this.context.currentTime + 0.08;
    const phraseLength = 16;
    for (let index = 0; index < phraseLength; index += 1) {
      const scaleIndex = (this.sequenceStep + index * 2 + (index % 3)) % scale.length;
      const rootMidi = 52 + scale[scaleIndex] + (index % 7 === 0 ? -12 : 0);
      this.config.instruments.forEach((instrumentId) => {
        this.scheduleInstrument(instrumentId, rootMidi, startAt + index * stepDuration, stepDuration, index);
      });
      if (this.config.vocalProfile !== "none" && index % 2 === 0) {
        this.scheduleVocal(rootMidi, startAt + index * stepDuration, stepDuration * 1.85, index);
      }
    }
    this.sequenceStep = (this.sequenceStep + 3) % scale.length;
    const phraseMs = phraseLength * stepDuration * 1000;
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this.schedulePhrase();
    }, Math.max(500, phraseMs - 160));
  }

  scheduleInstrument(instrumentId, rootMidi, startAt, stepDuration, index) {
    const profile = MUSIC_INSTRUMENTS[instrumentId];
    if (!profile) return;
    if (profile.role === "drums") {
      this.scheduleDrum(startAt, index);
      return;
    }
    if (profile.role === "bass" && index % 2) return;
    if (profile.role === "pad" && index % 4) return;
    if (profile.role === "accent" && index % 4 !== 2) return;
    if (profile.role === "rhythm" && index % 2 === 0) return;
    const midi = profile.role === "bass" ? 40 + ((rootMidi - 52) % 12) : rootMidi + profile.octave;
    const duration = stepDuration * profile.release;
    this.scheduleTone(midiToFrequency(midi), startAt, duration, profile, index);
    if (profile.role === "pad") {
      this.scheduleTone(midiToFrequency(midi + 7), startAt, duration, { ...profile, peak: profile.peak * 0.7 }, index + 1);
    }
  }

  scheduleTone(frequency, startAt, duration, profile, index) {
    const oscillator = this.context.createOscillator();
    const gain = this.context.createGain();
    const filter = this.context.createBiquadFilter();
    oscillator.type = profile.wave;
    oscillator.frequency.setValueAtTime(frequency, startAt);
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(950 + (index % 4) * 380, startAt);
    filter.Q.value = profile.role === "accent" ? 2.4 : 0.7;
    gain.gain.setValueAtTime(0.0001, startAt);
    gain.gain.exponentialRampToValueAtTime(profile.peak, startAt + Math.min(0.14, duration * 0.22));
    gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
    oscillator.connect(filter);
    filter.connect(gain);
    gain.connect(this.master);
    oscillator.start(startAt);
    oscillator.stop(startAt + duration + 0.05);
  }

  scheduleDrum(startAt, index) {
    if (index % 4 === 0) {
      const kick = this.context.createOscillator();
      const gain = this.context.createGain();
      kick.type = "sine";
      kick.frequency.setValueAtTime(120, startAt);
      kick.frequency.exponentialRampToValueAtTime(42, startAt + 0.16);
      gain.gain.setValueAtTime(0.09, startAt);
      gain.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.2);
      kick.connect(gain);
      gain.connect(this.master);
      kick.start(startAt);
      kick.stop(startAt + 0.22);
    }
    if (index % 2 === 1 && this.noiseBuffer) {
      const noise = this.context.createBufferSource();
      const filter = this.context.createBiquadFilter();
      const gain = this.context.createGain();
      noise.buffer = this.noiseBuffer;
      filter.type = "highpass";
      filter.frequency.value = 5200;
      gain.gain.setValueAtTime(0.028, startAt);
      gain.gain.exponentialRampToValueAtTime(0.0001, startAt + 0.08);
      noise.connect(filter);
      filter.connect(gain);
      gain.connect(this.master);
      noise.start(startAt);
      noise.stop(startAt + 0.09);
    }
  }

  scheduleVocal(rootMidi, startAt, duration, index) {
    const profile = MUSIC_VOCAL_PROFILES[this.config.vocalProfile];
    const layers = this.config.vocalProfile === "choir" ? [-5, 0, 7] : [0];
    layers.forEach((interval, layerIndex) => {
      const oscillator = this.context.createOscillator();
      const formantOne = this.context.createBiquadFilter();
      const formantTwo = this.context.createBiquadFilter();
      const gain = this.context.createGain();
      oscillator.type = "sawtooth";
      oscillator.frequency.setValueAtTime(midiToFrequency(rootMidi + profile.octave + interval), startAt);
      oscillator.detune.value = (layerIndex - 1) * 5;
      formantOne.type = "bandpass";
      formantOne.frequency.value = profile.formants[0] + (index % 3) * 30;
      formantOne.Q.value = 5;
      formantTwo.type = "bandpass";
      formantTwo.frequency.value = profile.formants[1];
      formantTwo.Q.value = 4;
      gain.gain.setValueAtTime(0.0001, startAt);
      gain.gain.exponentialRampToValueAtTime(0.014 / layers.length, startAt + 0.2);
      gain.gain.exponentialRampToValueAtTime(0.0001, startAt + duration);
      oscillator.connect(formantOne);
      oscillator.connect(formantTwo);
      formantOne.connect(gain);
      formantTwo.connect(gain);
      gain.connect(this.master);
      oscillator.start(startAt);
      oscillator.stop(startAt + duration + 0.05);
    });
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
    const volume = Math.min(0.32, Math.max(0, Number(value) || 0));
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
    this.noiseBuffer = null;
  }
}

export const soundscapeEngine = new BrowserSoundscapeEngine();
