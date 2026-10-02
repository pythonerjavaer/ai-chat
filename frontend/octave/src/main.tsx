import { StrictMode, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  Download,
  Headphones,
  Layers3,
  Pause,
  Play,
  Plus,
  RotateCcw,
  SlidersHorizontal,
  Sparkles,
  Volume2,
  WandSparkles,
} from "lucide-react";
import { WorkletSynthesizer } from "spessasynth_lib";
import "./styles.css";

const OCTAVE_ASSET_BASE = "/octave/";

const steps = 16;
const noteNames = ["C4", "D4", "E4", "G4", "A4", "C5", "D5", "E5"];
const palettes = ["Neon Rain", "Glass Tide", "Night Drive"];
const drumPatterns: Record<string, number[][]> = {
  Rock: [[0, 8], [4, 12], [0, 2, 4, 6, 8, 10, 12, 14]],
  Pop: [[0, 6, 8, 14], [4, 12], [0, 2, 4, 6, 8, 10, 12, 14]],
  "Half-time": [[0, 10], [8], [0, 4, 8, 12]],
  Shuffle: [[0, 7, 10], [4, 12], [0, 3, 4, 7, 8, 11, 12, 15]],
  Disco: [[0, 4, 8, 12], [4, 12], Array.from({ length: 16 }, (_, i) => i)],
};
const cleanCurve = Float32Array.from(
  { length: 2048 },
  (_, i) => (i * 2) / 2047 - 1,
);
const driveCurve = Float32Array.from({ length: 2048 }, (_, i) =>
  Math.tanh(((i * 2) / 2047 - 1) * 4),
);
type Instrument =
  | "synth"
  | "piano"
  | "guitarAcoustic"
  | "guitarElectric"
  | "bass"
  | "violin"
  | "cello"
  | "drums";
type ImportedNote = {
  midiNote: number;
  start: number;
  length: number;
  velocity: number;
};
type ImportedMidi = {
  name: string;
  duration: number;
  channels: { channel: number; notes: ImportedNote[] }[];
  selectedChannel: number;
  notes: ImportedNote[];
};

function App() {
  const [view, setView] = useState<"create" | "generation" | "arrangement">(
    "arrangement",
  );
  const [playing, setPlaying] = useState(false);
  const [importedMidi, setImportedMidi] = useState<ImportedMidi | null>(null);
  const [midiPosition, setMidiPosition] = useState(0);
  const [bpm, setBpm] = useState(112);
  const [activeStep, setActiveStep] = useState(0);
  const [selectedBar, setSelectedBar] = useState(1);
  const [selectedTrack, setSelectedTrack] = useState("melody");
  const [mixerExpanded, setMixerExpanded] = useState(true);
  const [extraTracks, setExtraTracks] = useState<Instrument[]>([]);
  const [exportFormat, setExportFormat] = useState("WAV");
  const [exportMessage, setExportMessage] = useState("");
  const [trackVolumes, setTrackVolumes] = useState<Record<string, number>>({
    Master: 82,
    synth: 65,
    chords: 48,
    drums: 72,
  });
  const [mutedTracks, setMutedTracks] = useState<string[]>([]);
  const [soloTrack, setSoloTrack] = useState<string | null>(null);
  const [mood, setMood] = useState(42);
  const [key, setKey] = useState("C minor");
  const [signature, setSignature] = useState("4/4");
  const [looping, setLooping] = useState(true);
  const [monitorOn, setMonitorOn] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [addTrackOpen, setAddTrackOpen] = useState(false);
  const [selectedChord, setSelectedChord] = useState(0);
  const [selectedStyle, setSelectedStyle] = useState("Ambient");
  const [selectedPalette, setSelectedPalette] = useState("Neon Rain");
  const [instrument, setInstrument] = useState<Instrument>("synth");
  const [selectedDrumPattern, setSelectedDrumPattern] = useState("Rock");
  const [drumGrid, setDrumGrid] = useState(() =>
    Array.from({ length: 8 }, (_, row) =>
      Array.from({ length: steps }, (_, col) =>
        row < 3 && drumPatterns.Rock[row].includes(col),
      ),
    ),
  );
  const [electricDrive, setElectricDrive] = useState(false);
  const [loadedInstruments, setLoadedInstruments] = useState<Instrument[]>([
    "synth",
    "drums",
  ]);
  const [failedInstruments, setFailedInstruments] = useState<Instrument[]>([]);
  const instrumentInfo: Record<
    Instrument,
    { name: string; label: string; color: string }
  > = {
    synth: { name: "SYNTH", label: "Sample · Synth Pluck", color: "lime" },
    piano: { name: "PIANO", label: "Sample · Grand Piano", color: "lime" },
    guitarAcoustic: {
      name: "ACOUSTIC GUITAR",
      label: "Sample · Folk Guitar",
      color: "purple",
    },
    guitarElectric: {
      name: "ELECTRIC GUITAR",
      label: "Sample · Electric Guitar",
      color: "purple",
    },
    bass: { name: "BASS", label: "Sample · Electric Bass", color: "teal" },
    violin: { name: "VIOLIN", label: "Sample · Solo Violin", color: "purple" },
    cello: { name: "CELLO", label: "Sample · Solo Cello", color: "teal" },
    drums: { name: "DRUMS", label: "Acoustic Kit · Kick / Snare / Hats", color: "orange" },
  };
  const [pattern, setPattern] = useState(() =>
    Array.from({ length: 8 }, (_, row) =>
      Array.from({ length: steps }, (_, col) => (row + col * 3) % 7 === 0),
    ),
  );
  const visiblePattern = selectedTrack === "drums" ? drumGrid : pattern;
  const audioRef = useRef<AudioContext | null>(null);
  const soundFontSynth = useRef<WorkletSynthesizer | null>(null);
  const soundFontInit = useRef<Promise<WorkletSynthesizer> | null>(null);
  const soundFontGain = useRef<GainNode | null>(null);
  const soundFontShaper = useRef<WaveShaperNode | null>(null);
  const soundFontCache = useRef(new Set<string>());
  const soundFontLoads = useRef(new Map<string, Promise<void>>());
  const sampleCache = useRef(new Map<string, AudioBuffer>());
  const timerRef = useRef<number | null>(null);
  const midiNoteTimers = useRef<number[]>([]);
  const midiStopTimer = useRef<number | null>(null);
  const midiStartedAt = useRef(0);

  useEffect(() => {
    if (!playing) {
      if (timerRef.current) window.clearInterval(timerRef.current);
      if (importedMidi) setMidiPosition(0);
      return;
    }
    if (importedMidi) {
      timerRef.current = window.setInterval(() => {
        const elapsed = (performance.now() - midiStartedAt.current) / 1000;
        setMidiPosition(Math.min(1, elapsed / Math.max(importedMidi.duration, 0.1)));
      }, 50);
      return () => {
        if (timerRef.current) window.clearInterval(timerRef.current);
      };
    }
    timerRef.current = window.setInterval(
      () =>
        setActiveStep((step) => {
          if (step + 1 >= steps && !looping && !importedMidi) {
            setPlaying(false);
            return steps - 1;
          }
          return (step + 1) % steps;
        }),
      (60 / bpm / 4) * 1000,
    );
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
    };
  }, [playing, bpm, looping, importedMidi]);

  useEffect(() => {
    if (!playing || importedMidi) return;
    const activeNotes = pattern
      .map((row, rowIndex) => (row[activeStep] ? rowIndex : -1))
      .filter((rowIndex) => rowIndex >= 0);
    if (
      !mutedTracks.includes("melody") &&
      !mutedTracks.includes(instrument) &&
      (!soloTrack || soloTrack === "melody" || soloTrack === instrument)
    ) {
      activeNotes.forEach((rowIndex) =>
        tone(noteFrequency(noteNames[rowIndex]), 0.22, instrument),
      );
    }
    if (
      !mutedTracks.includes("drums") &&
      (!soloTrack || soloTrack === "drums")
    ) {
      drumGrid.slice(0, 3).forEach((rowPattern, row) => {
        if (rowPattern[activeStep]) {
          const drumNotes = [36, 38, 42];
          void tone(440 * Math.pow(2, (drumNotes[row] - 69) / 12), 0.18, "drums");
        }
      });
    }
  }, [
    activeStep,
    playing,
    instrument,
    pattern,
    mutedTracks,
    soloTrack,
    trackVolumes,
    selectedDrumPattern,
    drumGrid,
    importedMidi,
  ]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (
        event.code !== "Space" ||
        event.repeat ||
        (event.target instanceof HTMLElement &&
          ["INPUT", "TEXTAREA", "BUTTON"].includes(event.target.tagName))
      )
        return;
      event.preventDefault();
      void togglePlay();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  });

  const sampleMap: Partial<Record<Instrument, string[]>> = {
    piano: ["C3", "C4", "C5"],
    guitarAcoustic: [
      "A2",
      "C3",
      "D3",
      "E3",
      "G3",
      "A3",
      "C4",
      "D4",
      "E4",
      "G4",
      "A4",
      "C5",
    ],
    guitarElectric: [
      "E2",
      "Fs2",
      "A2",
      "C3",
      "Fs3",
      "A3",
      "C4",
      "Fs4",
      "A4",
      "C5",
    ],
    bass: [
      "E1",
      "G1",
      "As1",
      "Cs2",
      "E2",
      "G2",
      "As2",
      "Cs3",
      "E3",
      "G3",
      "As3",
    ],
    violin: ["G3", "C4", "G4", "C5", "A5"],
    cello: ["C2", "G2", "C3", "G3", "C4"],
  };
  const soundFontFiles: Partial<Record<Instrument, string>> = {
    guitarAcoustic: `${OCTAVE_ASSET_BASE}soundfonts/folk-acoustic.sf2`,
    guitarElectric: `${OCTAVE_ASSET_BASE}soundfonts/electric-clean.sf2`,
    bass: `${OCTAVE_ASSET_BASE}soundfonts/bass-finger.sf2`,
  };
  const noteFrequency = (note: string) => {
    const match = note.match(/^([A-G])(s?)(\d)$/);
    if (!match) return 261.63;
    const semitones: Record<string, number> = {
      C: 0,
      D: 2,
      E: 4,
      F: 5,
      G: 7,
      A: 9,
      B: 11,
    };
    const midi =
      (Number(match[3]) + 1) * 12 + semitones[match[1]] + (match[2] ? 1 : 0);
    return 440 * Math.pow(2, (midi - 69) / 12);
  };
  const playSample = async (
    frequency: number,
    duration: number,
    voice: Instrument,
    ctx: AudioContext,
    velocity = 96,
  ) => {
    const notes = sampleMap[voice];
    if (!notes) return false;
    const note = notes.reduce((closest, candidate) =>
      Math.abs(noteFrequency(candidate) - frequency) <
      Math.abs(noteFrequency(closest) - frequency)
        ? candidate
        : closest,
    );
    const rootFrequency = noteFrequency(note);
    const url = `${OCTAVE_ASSET_BASE}samples/${voice}/${note}.mp3`;
    let buffer = sampleCache.current.get(url);
    try {
      if (!buffer) {
        const response = await fetch(url);
        if (!response.ok) throw new Error(`Sample unavailable: ${url}`);
        buffer = await ctx.decodeAudioData(await response.arrayBuffer());
        sampleCache.current.set(url, buffer);
        setLoadedInstruments((current) =>
          current.includes(voice) ? current : [...current, voice],
        );
        setFailedInstruments((current) =>
          current.filter((item) => item !== voice),
        );
      }
      const source = ctx.createBufferSource();
      const gain = ctx.createGain();
      source.buffer = buffer;
      source.playbackRate.value = frequency / rootFrequency;
      gain.gain.value =
        voice === "bass" ? 0.72 : voice === "guitarElectric" ? 0.48 : 0.62;
      gain.gain.value *=
        (trackVolumes.Master * (trackVolumes[voice] ?? 100)) / 10000;
      gain.gain.value *= velocity / 96;
      source.connect(gain).connect(ctx.destination);
      source.start();
      source.stop(ctx.currentTime + duration + 0.6);
      return true;
    } catch {
      setFailedInstruments((current) =>
        current.includes(voice) ? current : [...current, voice],
      );
      return false;
    }
  };
  const playSoundFont = async (
    frequency: number,
    duration: number,
    voice: Instrument,
    ctx: AudioContext,
    velocity = 96,
  ) => {
    const path = soundFontFiles[voice];
    if (!path) return false;
    try {
      if (!soundFontInit.current) {
        soundFontInit.current = (async () => {
          await ctx.audioWorklet.addModule(`${OCTAVE_ASSET_BASE}spessasynth_processor.min.js`);
          const synth = new WorkletSynthesizer(ctx);
          soundFontSynth.current = synth;
          soundFontGain.current = ctx.createGain();
          soundFontShaper.current = ctx.createWaveShaper();
          soundFontShaper.current.oversample = "4x";
          soundFontShaper.current.connect(ctx.destination);
          soundFontGain.current.connect(soundFontShaper.current);
          synth.connect(soundFontGain.current);
          await synth.isReady;
          return synth;
        })().catch((error) => {
          soundFontInit.current = null;
          throw error;
        });
      }
      const synth = await soundFontInit.current;
      if (soundFontShaper.current)
        soundFontShaper.current.curve =
          voice === "guitarElectric" && electricDrive ? driveCurve : cleanCurve;
      if (soundFontGain.current)
        soundFontGain.current.gain.value =
          (trackVolumes.Master * (trackVolumes[voice] ?? 100)) / 10000;
      if (!soundFontCache.current.has(path)) {
        let load = soundFontLoads.current.get(path);
        if (!load) {
          load = (async () => {
            const response = await fetch(path);
            if (!response.ok) throw new Error("SoundFont file unavailable");
            await synth.soundBankManager.addSoundBank(
              await response.arrayBuffer(),
              path,
            );
            soundFontCache.current.add(path);
          })();
          soundFontLoads.current.set(path, load);
        }
        try {
          await load;
        } catch (error) {
          soundFontLoads.current.delete(path);
          throw error;
        }
        setLoadedInstruments((current) =>
          current.includes(voice) ? current : [...current, voice],
        );
        setFailedInstruments((current) =>
          current.filter((item) => item !== voice),
        );
      }
      synth.soundBankManager.priorityOrder = [
        path,
        ...synth.soundBankManager.priorityOrder.filter((id) => id !== path),
      ];
      const soundFontPrograms: Partial<Record<Instrument, number>> = {
        guitarAcoustic: 24,
        guitarElectric: electricDrive ? 29 : 27,
        bass: 33,
      };
      synth.programChange(0, soundFontPrograms[voice] ?? 0);
      let midiNote = Math.max(
        0,
        Math.min(127, Math.round(69 + 12 * Math.log2(frequency / 440))),
      );
      const playableRanges: Partial<Record<Instrument, [number, number]>> = {
        guitarAcoustic: [33, 86],
        guitarElectric: [35, 86],
        bass: [26, 45],
      };
      const range = playableRanges[voice];
      if (range) {
        while (midiNote > range[1]) midiNote -= 12;
        while (midiNote < range[0]) midiNote += 12;
      }
      synth.noteOn(
        0,
        midiNote,
        Math.max(1, Math.min(127, Math.round(velocity))),
      );
      window.setTimeout(
        () => synth.noteOff(0, midiNote),
        Math.max(90, duration * 1000),
      );
      return true;
    } catch {
      setFailedInstruments((current) =>
        current.includes(voice) ? current : [...current, voice],
      );
      return false;
    }
  };
  const tone = async (
    frequency: number,
    duration = 0.16,
    voice: Instrument = "synth",
    velocity = 96,
    preservePitch = false,
  ) => {
    if (
      mutedTracks.includes("Master") ||
      mutedTracks.includes(voice) ||
      (soloTrack && soloTrack !== voice && soloTrack !== "melody")
    )
      return;
    const AudioCtx =
      window.AudioContext ||
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (!AudioCtx) return;
    audioRef.current ??= new AudioCtx();
    const ctx = audioRef.current;
    if (soundFontFiles[voice]) {
      await playSoundFont(frequency, duration, voice, ctx, velocity);
      return;
    }
    if (sampleMap[voice]) {
      const sampleFrequency = voice === "cello" && !preservePitch ? frequency / 2 : frequency;
      await playSample(sampleFrequency, duration, voice, ctx, velocity);
      return;
    }
    if (voice === "drums") {
      const midi = Math.round(69 + 12 * Math.log2(frequency / 440));
      const level = (trackVolumes.Master * (trackVolumes.drums ?? 100) * velocity) / 960000;
      const noiseHit = (seconds: number, cutoff: number, amount: number) => {
        const buffer = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * seconds), ctx.sampleRate);
        const data = buffer.getChannelData(0);
        for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;
        const source = ctx.createBufferSource();
        const filter = ctx.createBiquadFilter();
        const gain = ctx.createGain();
        source.buffer = buffer;
        filter.type = "highpass";
        filter.frequency.value = cutoff;
        gain.gain.setValueAtTime(Math.max(0.0001, level * amount), ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + seconds);
        source.connect(filter).connect(gain).connect(ctx.destination);
        source.start();
      };
      if (midi === 36 || midi === 35) {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = "sine";
        osc.frequency.setValueAtTime(145, ctx.currentTime);
        osc.frequency.exponentialRampToValueAtTime(42, ctx.currentTime + 0.14);
        gain.gain.setValueAtTime(Math.max(0.0001, level * 1.5), ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.22);
        osc.connect(gain).connect(ctx.destination);
        osc.start();
        osc.stop(ctx.currentTime + 0.23);
        noiseHit(0.025, 5000, 0.26);
      } else if (midi === 38 || midi === 40) {
        noiseHit(0.19, 1500, 1.4);
        const body = ctx.createOscillator();
        const gain = ctx.createGain();
        body.type = "triangle";
        body.frequency.value = 185;
        gain.gain.setValueAtTime(Math.max(0.0001, level * 0.55), ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.09);
        body.connect(gain).connect(ctx.destination);
        body.start();
        body.stop(ctx.currentTime + 0.1);
      } else if ([42, 44, 46].includes(midi)) {
        noiseHit(midi === 46 ? 0.45 : 0.055, midi === 46 ? 6500 : 8000, midi === 46 ? 0.7 : 0.48);
      } else {
        noiseHit(0.12, 1100, 0.8);
      }
      return;
    }
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    const filter = ctx.createBiquadFilter();
    const isGuitar = voice === "guitarAcoustic" || voice === "guitarElectric";
    osc.type =
      voice === "piano"
        ? "triangle"
        : isGuitar
          ? "triangle"
          : voice === "bass"
            ? "sawtooth"
            : "square";
    osc.frequency.value = voice === "bass" ? frequency / 2 : frequency;
    filter.type = "lowpass";
    filter.frequency.value = isGuitar ? 2200 : voice === "bass" ? 800 : 4200;
    const attack = voice === "piano" ? 0.01 : isGuitar ? 0.006 : 0.015;
    const peak = voice === "bass" ? 0.24 : 0.18;
    gain.gain.setValueAtTime(0.0001, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(
      Math.max(
        0.0001,
        (peak *
          (trackVolumes.Master * (trackVolumes[voice] ?? 100) * velocity)) /
          960000,
      ),
      ctx.currentTime + attack,
    );
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + duration);
    osc.connect(filter).connect(gain).connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + duration + 0.02);
  };

  const generate = () => {
    const densityCutoff = 0.8 - mood / 200;
    setPattern(
      Array.from({ length: 8 }, (_, row) =>
        Array.from(
          { length: steps },
          (_, col) =>
            Math.random() > densityCutoff &&
            (col < 3 || Math.random() > 0.25) &&
            (row + col) % 3 !== 0,
        ),
      ),
    );
  };
  const togglePlay = async () => {
    if (playing) {
      midiNoteTimers.current.forEach((timer) => window.clearTimeout(timer));
      midiNoteTimers.current = [];
      if (midiStopTimer.current) window.clearTimeout(midiStopTimer.current);
      midiStopTimer.current = null;
      setPlaying(false);
      return;
    }
    const AudioCtx =
      window.AudioContext ||
      (window as typeof window & { webkitAudioContext?: typeof AudioContext })
        .webkitAudioContext;
    if (AudioCtx) {
      audioRef.current ??= new AudioCtx();
      if (audioRef.current.state === "suspended")
        await audioRef.current.resume();
    }
    if (importedMidi && AudioCtx) {
      midiStartedAt.current = performance.now();
      setMidiPosition(0);
      const toFrequency = (note: number) => 440 * Math.pow(2, (note - 69) / 12);
      const firstNote = importedMidi.notes[0];
      if (firstNote)
        await tone(toFrequency(firstNote.midiNote), 0.005, instrument, 1, true);
      midiNoteTimers.current = importedMidi.notes.map((note) =>
        window.setTimeout(
          () =>
            void tone(
              toFrequency(note.midiNote),
              Math.max(0.05, note.length),
              instrument,
              note.velocity,
              true,
            ),
          Math.max(0, note.start) * 1000,
        ),
      );
      midiStopTimer.current = window.setTimeout(
        () => setPlaying(false),
        Math.max(0.5, importedMidi.duration) * 1000 + 250,
      );
    }
    setPlaying(true);
  };
  const toggleCell = (row: number, col: number) => {
    const activeGrid = selectedTrack === "drums" ? drumGrid : pattern;
    if (selectedTrack === "drums") {
      setDrumGrid((current) => current.map((r, ri) =>
        r.map((v, ci) => (ri === row && ci === col ? !v : v)),
      ));
    } else {
      setPattern((current) => current.map((r, ri) =>
        r.map((v, ci) => (ri === row && ci === col ? !v : v)),
      ));
    }
    if (!activeGrid[row][col]) {
      if (selectedTrack === "drums") {
        const drumNotes = [36, 38, 42];
        void tone(440 * Math.pow(2, (drumNotes[row % 3] - 69) / 12), 0.18, "drums");
      } else {
        void tone(noteFrequency(noteNames[row]), 0.2, instrument);
      }
    }
  };

  if (view === "create")
    return <CreateView onNavigate={setView} onMidiLoaded={setImportedMidi} />;
  if (view === "generation") return <GenerationView onNavigate={setView} />;

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Activity size={20} />
          </div>
          <div>
            <div className="brand-name">SONIC FOUNDRY</div>
            <div className="brand-sub">GENERATIVE MUSIC WORKSPACE</div>
          </div>
        </div>
        <nav className="top-nav">
          <button onClick={() => setView("create")}>Create</button>
          <button onClick={() => setView("generation")}>Generation</button>
          <button className="active" onClick={() => setView("arrangement")}>
            Arrangement
          </button>
        </nav>
        <div className="project-name">
          <span className="status-dot" /> Untitled session{" "}
          <span className="muted">/</span>{" "}
          <span className="muted">Draft 01</span>
        </div>
        <div className="top-actions">
          <button
            className={`icon-btn ${monitorOn ? "active" : ""}`}
            onClick={() => {
              setMonitorOn((v) => !v);
              if (!monitorOn) tone(440, 0.3, instrument);
            }}
            title="耳机监听"
            aria-pressed={monitorOn}
          >
            <Headphones size={17} />
          </button>
          <button
            className={`icon-btn ${mixerExpanded ? "active" : ""}`}
            onClick={() => setMixerExpanded((v) => !v)}
            title="显示/隐藏混音器"
            aria-pressed={mixerExpanded}
          >
            <SlidersHorizontal size={17} />
          </button>
          <button
            className="export-btn"
            onClick={() => {
              setExportMessage("");
              setExportOpen(true);
            }}
          >
            <Download size={15} /> Export
          </button>
          <div className="avatar">F</div>
        </div>
      </header>
      <main className="workspace">
        <aside className="sidebar left-panel">
          <div className="panel-heading">
            <span>生成设置</span>
            <span className="live-label">
              <i /> READY
            </span>
          </div>
          <label className="field-label">
            风格 <span>STYLE</span>
          </label>
          <div className="style-grid">
            {["Ambient", "Cinematic", "Lo-fi", "House"].map((style) => (
              <button
                key={style}
                onClick={() => setSelectedStyle(style)}
                className={
                  selectedStyle === style ? "style-btn selected" : "style-btn"
                }
              >
                {style}
              </button>
            ))}
          </div>
          <label className="field-label">
            情绪 <span>MOOD</span>
          </label>
          <div className="range-wrap">
            <div className="range-label">
              <span>Calm</span>
              <b>{mood}</b>
              <span>Intense</span>
            </div>
            <input
              aria-label="情绪强度"
              type="range"
              min="0"
              max="100"
              value={mood}
              onChange={(e) => setMood(Number(e.target.value))}
            />
          </div>
          <label className="field-label">
            调性与速度 <span>KEY / TEMPO</span>
          </label>
          <div className="select-row">
            <button
              className="select-btn"
              onClick={() =>
                setKey(
                  (current) =>
                    ["C minor", "A minor", "G major", "D minor"][
                      (["C minor", "A minor", "G major", "D minor"].indexOf(
                        current,
                      ) +
                        1) %
                        4
                    ],
                )
              }
            >
              {key} <span>⌄</span>
            </button>
            <button
              className="select-btn"
              onClick={() =>
                setSignature(
                  (current) =>
                    ["4/4", "3/4", "6/8"][
                      (["4/4", "3/4", "6/8"].indexOf(current) + 1) % 3
                    ],
                )
              }
            >
              {signature} <span>⌄</span>
            </button>
          </div>
          <div className="bpm-control">
            <span>BPM</span>
            <strong>{bpm}</strong>
            <input
              type="range"
              min="70"
              max="160"
              value={bpm}
              onChange={(e) => setBpm(Number(e.target.value))}
            />
          </div>
          <label className="field-label">
            音色 <span>PALETTE</span>
          </label>
          <div className="palette-list">
            {palettes.map((palette, i) => (
              <button
                key={palette}
                onClick={() => setSelectedPalette(palette)}
                className={
                  selectedPalette === palette ? "palette selected" : "palette"
                }
              >
                <span className={`swatch swatch-${i}`} />
                {palette}
                <span className="check">
                  {selectedPalette === palette ? "✓" : ""}
                </span>
              </button>
            ))}
          </div>
          <label className="field-label">
            乐器 <span>INSTRUMENT</span>
          </label>
          <div className="instrument-picker">
            {(
              [
                ["synth", "合成器"],
                ["piano", "钢琴"],
                ["guitarAcoustic", "民谣吉他"],
                ["guitarElectric", "电吉他"],
                ["bass", "贝斯"],
                ["violin", "小提琴"],
                ["cello", "大提琴"],
                ["drums", "鼓组"],
              ] as [Instrument, string][]
            ).map(([value, label]) => (
              <button
                key={value}
                onClick={() => {
                  setInstrument(value);
                  setSelectedTrack("melody");
                  void tone(noteFrequency("C4"), 0.55, value);
                }}
                className={instrument === value ? "selected" : ""}
              >
                {label}
                {instrument === value && (
                  <small
                    className={failedInstruments.includes(value) ? "error" : ""}
                  >
                    {failedInstruments.includes(value)
                      ? "ERROR"
                      : loadedInstruments.includes(value)
                        ? "READY"
                        : "LOAD"}
                  </small>
                )}
              </button>
            ))}
          </div>
          {instrument === "guitarElectric" && (
            <button
              className={`drive-toggle ${electricDrive ? "selected" : ""}`}
              aria-pressed={electricDrive}
              onClick={() => setElectricDrive((current) => !current)}
            >
              电吉他效果：{electricDrive ? "轻度过载" : "清音"}
            </button>
          )}
          <button className="generate-btn" onClick={generate}>
            <WandSparkles size={17} /> Generate new pattern <span>⌘ ↵</span>
          </button>
        </aside>
        <section className="main-canvas">
          <div className="canvas-header">
            <div>
              <div className="eyebrow">
                PATTERN 001 <span>•</span> {selectedStyle.toUpperCase()}
              </div>
              <h1>
                {importedMidi ? (
                  importedMidi.name
                ) : (
                  <>
                    Midnight <em>Current</em>
                  </>
                )}
              </h1>
              <p>
                {importedMidi
                  ? `${importedMidi.notes.length} 个音符 · MIDI 通道 ${importedMidi.selectedChannel + 1} · ${importedMidi.duration.toFixed(1)} 秒 · 当前乐器重演`
                  : `4-bar generative sketch · ${key} · ${signature} · ${bpm} BPM`}
              </p>
            </div>
            <button className="regen-btn" onClick={generate}>
              <RotateCcw size={15} /> Regenerate
            </button>
          </div>
          <div className="transport">
            <button className="play-btn" onClick={togglePlay}>
              {playing ? (
                <Pause size={19} fill="currentColor" />
              ) : (
                <Play size={19} fill="currentColor" />
              )}
            </button>
            <div className="transport-wave">
              <div className="wave-label">
                <span>{importedMidi ? "MIDI REPLAY" : "LIVE PREVIEW"}</span>
                <span>{playing ? "PLAYING" : importedMidi ? "MIDI READY" : "PAUSED"}</span>
              </div>
              <div className="waveform">
                {Array.from({ length: 54 }, (_, i) => (
                  <button
                    aria-label={`跳转到波形位置 ${i + 1}`}
                    key={i}
                    style={{
                      height: `${20 + Math.abs(Math.sin(i * 1.7)) * 48 + ((i * 13) % 13)}%`,
                    }}
                    className={
                      importedMidi
                        ? i < midiPosition * 54 ? "played" : ""
                        : playing && i < (activeStep / steps) * 54 ? "played" : ""
                    }
                    onClick={() => {
                      if (importedMidi) {
                        const position = i / 53;
                        setMidiPosition(position);
                        if (playing) {
                          midiNoteTimers.current.forEach((timer) => window.clearTimeout(timer));
                          const elapsed = position * importedMidi.duration;
                          midiNoteTimers.current = importedMidi.notes
                            .filter((note) => note.start >= elapsed)
                            .map((note) => window.setTimeout(
                              () => void tone(
                                440 * Math.pow(2, (note.midiNote - 69) / 12),
                                Math.max(0.05, note.length),
                                instrument,
                                note.velocity,
                                true,
                              ),
                              Math.max(0, note.start - elapsed) * 1000,
                            ));
                          if (midiStopTimer.current) window.clearTimeout(midiStopTimer.current);
                          midiStartedAt.current = performance.now() - elapsed * 1000;
                          midiStopTimer.current = window.setTimeout(
                            () => setPlaying(false),
                            Math.max(0, importedMidi.duration - elapsed) * 1000 + 250,
                          );
                        }
                        return;
                      }
                      const step = Math.min(15, Math.floor((i / 54) * 16));
                      setActiveStep(step);
                      setSelectedBar(Math.floor(step / 4) + 1);
                    }}
                  />
                ))}
              </div>
            </div>
            <div className="transport-meta">
              <span>01:16</span>
              <button
                className={`text-toggle ${looping ? "selected" : ""}`}
                onClick={() => setLooping((v) => !v)}
                aria-pressed={looping}
              >
                LOOP {looping ? "ON" : "OFF"}
              </button>
            </div>
          </div>
          <div className="sequence-section">
            <div className="section-title">
              <span>ARRANGEMENT</span>
              <span className="bar-count">
                BARS <b>04</b>
              </span>
            </div>
            <div className="drum-patterns" aria-label="架子鼓节拍预设">
              <span>鼓组节拍</span>
              {Object.keys(drumPatterns).map((name) => (
                <button
                  key={name}
                  className={selectedDrumPattern === name ? "selected" : ""}
                  aria-pressed={selectedDrumPattern === name}
                  onClick={() => {
                    setSelectedDrumPattern(name);
                    setDrumGrid(Array.from({ length: 8 }, (_, row) =>
                      Array.from({ length: steps }, (_, col) =>
                        row < 3 && drumPatterns[name][row].includes(col),
                      ),
                    ));
                  }}
                  onDoubleClick={() => {
                    setSelectedDrumPattern(name);
                    void togglePlay();
                  }}
                  title={`${name} · 播放试听`}
                >
                  {name}
                </button>
              ))}
              <small>每小节 16 步 · 点击节拍可切换；播放后可试听</small>
            </div>
            <div className="bar-grid">
              {[1, 2, 3, 4].map((bar) => (
                <button
                  aria-label={`选择第 ${bar} 小节`}
                  aria-pressed={selectedBar === bar}
                  className={`bar ${selectedBar === bar ? "selected" : ""}`}
                  key={bar}
                  onClick={() => {
                    setSelectedBar(bar);
                    setActiveStep((bar - 1) * 4);
                  }}
                >
                  <span>0{bar}</span>
                  <div className="bar-line" />
                </button>
              ))}
            </div>
            <div className="sequencer">
              <div className="note-labels">
                {noteNames.map((n) => (
                  <span key={n}>{selectedTrack === "drums" ? ["KICK", "SNARE", "HI-HAT"][noteNames.indexOf(n) % 3] : n}</span>
                ))}
              </div>
              <div className="step-grid">
                {visiblePattern.map((row, ri) =>
                  row.map((on, ci) => (
                    <button
                      key={`${ri}-${ci}`}
                      aria-label={`${selectedTrack === "drums" ? ["底鼓", "军鼓", "踩镲"][ri % 3] : noteNames[ri]} 第${ci + 1}步`}
                      onClick={() => toggleCell(ri, ci)}
                      className={`step ${on ? "on" : ""} ${activeStep === ci && playing ? "current" : ""} ${Math.floor(ci / 4) + 1 === selectedBar ? "in-selected-bar" : ""}`}
                    >
                      <span />
                    </button>
                  )),
                )}
              </div>
              <div className="step-numbers">
                {Array.from({ length: 16 }, (_, i) => (
                  <button
                    key={i}
                    aria-label={`跳转到第 ${i + 1} 步`}
                    onClick={() => {
                      setActiveStep(i);
                      setSelectedBar(Math.floor(i / 4) + 1);
                    }}
                  >
                    {String(i + 1).padStart(2, "0")}
                  </button>
                ))}
              </div>
            </div>
          </div>
          <div className="lower-row">
            <div className="mini-panel">
              <div className="mini-header">
                CHORD PROGRESSION <span>4 bars</span>
              </div>
              <div className="chords">
                {["Cm⁷", "A♭maj⁷", "E♭", "B♭sus₂"].map((chord, i) => (
                  <button
                    aria-label={`试听和弦 ${chord}`}
                    aria-pressed={selectedChord === i}
                    className={`chord ${selectedChord === i ? "selected" : ""}`}
                    key={chord}
                    onClick={() => {
                      setSelectedChord(i);
                      [
                        [0, 3, 7, 10],
                        [8, 12, 15, 19],
                        [3, 7, 10, 15],
                        [10, 12, 17, 22],
                      ][i].forEach((semitone) =>
                        tone(
                          261.63 * Math.pow(2, (i * 2 + semitone) / 12),
                          0.45,
                          instrument,
                        ),
                      );
                    }}
                  >
                    <span>0{i + 1}</span>
                    <strong>{chord}</strong>
                    <div className="chord-bars">
                      {Array.from({ length: 4 }, (_, j) => (
                        <i
                          key={j}
                          style={{
                            height: `${35 + ((i * 17 + j * 21) % 50)}%`,
                          }}
                        />
                      ))}
                    </div>
                  </button>
                ))}
              </div>
            </div>
            <div className="mini-panel">
              <div className="mini-header">
                ENERGY CURVE <span>EVOLUTION</span>
              </div>
              <button
                className="energy-chart"
                aria-label="选择能量曲线位置"
                onClick={(e) => {
                  const rect = e.currentTarget.getBoundingClientRect();
                  setMood(
                    Math.round(((e.clientX - rect.left) / rect.width) * 100),
                  );
                }}
              >
                <svg viewBox="0 0 440 92" preserveAspectRatio="none">
                  <path
                    d="M0 75 C50 74 52 47 98 56 S140 72 173 36 S220 31 250 47 S295 69 330 25 S390 39 440 12"
                    fill="none"
                    stroke="#c7f65a"
                    strokeWidth="2"
                  />
                  <path
                    d="M0 75 C50 74 52 47 98 56 S140 72 173 36 S220 31 250 47 S295 69 330 25 S390 39 440 12 V92 H0Z"
                    fill="url(#fade)"
                    opacity=".22"
                  />
                  <defs>
                    <linearGradient id="fade" x1="0" x2="0" y1="0" y2="1">
                      <stop stopColor="#c7f65a" />
                      <stop offset="1" stopColor="#c7f65a" stopOpacity="0" />
                    </linearGradient>
                  </defs>
                </svg>
              </button>
            </div>
          </div>
        </section>
        <aside className="sidebar right-panel">
          <div className="panel-heading">
            <span>轨道</span>
            <button
              className="tiny-btn"
              title="新增乐器轨道"
              onClick={() => setAddTrackOpen(true)}
            >
              <Plus size={14} />
            </button>
          </div>
          <div className="track-list">
            <Track
              color={instrumentInfo[instrument].color}
              name={instrumentInfo[instrument].name}
              label={instrumentInfo[instrument].label}
              active={selectedTrack === "melody"}
              onSelect={() => setSelectedTrack("melody")}
              muted={mutedTracks.includes(instrument)}
              solo={soloTrack === instrument}
              onMute={() =>
                setMutedTracks((v) =>
                  v.includes(instrument)
                    ? v.filter((x) => x !== instrument)
                    : [...v, instrument],
                )
              }
              onSolo={() =>
                setSoloTrack((v) => (v === instrument ? null : instrument))
              }
            />
            <Track
              color="purple"
              name="CHORDS"
              label="MIDI · Soft Keys"
              active={selectedTrack === "chords"}
              onSelect={() => setSelectedTrack("chords")}
              muted={mutedTracks.includes("chords")}
              solo={soloTrack === "chords"}
              onMute={() =>
                setMutedTracks((v) =>
                  v.includes("chords")
                    ? v.filter((x) => x !== "chords")
                    : [...v, "chords"],
                )
              }
              onSolo={() =>
                setSoloTrack((v) => (v === "chords" ? null : "chords"))
              }
            />
            <Track
              color="orange"
              name="DRUMS"
              label="Kick · Snare · Hi-hat"
              active={selectedTrack === "drums"}
              onSelect={() => setSelectedTrack("drums")}
              muted={mutedTracks.includes("drums")}
              solo={soloTrack === "drums"}
              onMute={() =>
                setMutedTracks((v) =>
                  v.includes("drums")
                    ? v.filter((x) => x !== "drums")
                    : [...v, "drums"],
                )
              }
              onSolo={() =>
                setSoloTrack((v) => (v === "drums" ? null : "drums"))
              }
            />
            {extraTracks.map((track, i) => (
              <Track
                key={`${track}-${i}`}
                color={instrumentInfo[track].color}
                name={instrumentInfo[track].name}
                label={instrumentInfo[track].label}
                active={selectedTrack === `extra-${i}`}
                onSelect={() => {
                  setSelectedTrack(`extra-${i}`);
                  setInstrument(track);
                }}
                muted={mutedTracks.includes(`extra-${i}`)}
                solo={soloTrack === `extra-${i}`}
                onMute={() =>
                  setMutedTracks((v) =>
                    v.includes(`extra-${i}`)
                      ? v.filter((x) => x !== `extra-${i}`)
                      : [...v, `extra-${i}`],
                  )
                }
                onSolo={() =>
                  setSoloTrack((v) =>
                    v === `extra-${i}` ? null : `extra-${i}`,
                  )
                }
              />
            ))}
          </div>
          {mixerExpanded && (
            <div className="mix-section">
              <div className="panel-heading">
                <span>混音器</span>
                <span className="muted">MASTER</span>
              </div>
              {[
                "Master",
                instrumentInfo[instrument].name,
                "Soft Keys",
                "Drum Rack",
              ].map((name, i) => {
                const trackKey =
                  i === 0
                    ? "Master"
                    : i === 1
                      ? instrument
                      : i === 2
                        ? "chords"
                        : "drums";
                return (
                  <div className="mixer-row" key={name}>
                    <span className={`mix-dot d${i}`} />
                    <span className="mix-name">{name}</span>
                    <input
                      aria-label={`${name} 音量`}
                      className="mixer-volume"
                      type="range"
                      min="0"
                      max="100"
                      value={trackVolumes[trackKey] ?? [82, 65, 48, 72][i]}
                      onChange={(event) =>
                        setTrackVolumes((current) => ({
                          ...current,
                          [trackKey]: Number(event.target.value),
                        }))
                      }
                    />
                    <button
                      className={`mute ${mutedTracks.includes(trackKey) ? "selected" : ""}`}
                      aria-label={`静音 ${name}`}
                      aria-pressed={mutedTracks.includes(trackKey)}
                      onClick={() =>
                        setMutedTracks((v) =>
                          v.includes(trackKey)
                            ? v.filter((x) => x !== trackKey)
                            : [...v, trackKey],
                        )
                      }
                    >
                      M
                    </button>
                    <button
                      className={`mute ${soloTrack === trackKey ? "selected" : ""}`}
                      aria-label={`独奏 ${name}`}
                      aria-pressed={soloTrack === trackKey}
                      onClick={() =>
                        setSoloTrack((v) => (v === trackKey ? null : trackKey))
                      }
                    >
                      S
                    </button>
                  </div>
                );
              })}
            </div>
          )}
          <div className="tips">
            <Sparkles size={15} />
            <div>
              <strong>生成提示</strong>
              <p>试试把情绪滑向「Intense」，发现更有张力的和声。</p>
            </div>
          </div>
        </aside>
      </main>
      {addTrackOpen && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setAddTrackOpen(false);
          }}
        >
          <section
            className="action-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="add-track-title"
          >
            <div className="modal-head">
              <h2 id="add-track-title">新增乐器轨道</h2>
              <button
                className="icon-btn"
                onClick={() => setAddTrackOpen(false)}
                aria-label="关闭"
              >
                ×
              </button>
            </div>
            <div className="modal-options">
              {(
                [
                  "synth",
                  "piano",
                  "guitarAcoustic",
                  "guitarElectric",
                  "bass",
                  "violin",
                  "cello",
                  "drums",
                ] as Instrument[]
              ).map((value) => (
                <button
                  key={value}
                  onClick={() => {
                    setExtraTracks((current) => [...current, value]);
                    setInstrument(value);
                    setSelectedTrack(`extra-${extraTracks.length}`);
                    setAddTrackOpen(false);
                  }}
                >
                  {instrumentInfo[value].name}
                  <small>{instrumentInfo[value].label}</small>
                </button>
              ))}
            </div>
          </section>
        </div>
      )}
      {exportOpen && (
        <div
          className="modal-backdrop"
          onMouseDown={(e) => {
            if (e.target === e.currentTarget) setExportOpen(false);
          }}
        >
          <section
            className="action-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="export-title"
          >
            <div className="modal-head">
              <h2 id="export-title">导出作品</h2>
              <button
                className="icon-btn"
                onClick={() => setExportOpen(false)}
                aria-label="关闭"
              >
                ×
              </button>
            </div>
            <p>选择导出格式</p>
            <div className="format-options">
              {["WAV", "MP3", "MIDI", "分轨"].map((format) => (
                <button
                  key={format}
                  className={exportFormat === format ? "selected" : ""}
                  onClick={() => {
                    setExportFormat(format);
                    setExportMessage("");
                  }}
                >
                  {format}
                </button>
              ))}
            </div>
            <p className="export-note">
              当前版本可以编曲和试听；成品音频、MIDI 与分轨文件导出尚未接入。
            </p>
            {exportMessage && <p role="status">{exportMessage}</p>}
            <button
              className="generate-btn"
              onClick={() =>
                setExportMessage(
                  `${exportFormat} 导出目前尚未实现，工程内容没有被更改。`,
                )
              }
            >
              导出 {exportFormat}
            </button>
          </section>
        </div>
      )}
      <footer className="footer">
        <span>
          <span className="status-dot green" /> LOCAL AUDIO ENGINE
        </span>
        <span>SPACE 播放/暂停　·　点击网格编辑音符</span>
        <span className="muted">v0.1.0</span>
      </footer>
    </div>
  );
}

function CreateView({
  onNavigate,
  onMidiLoaded,
}: {
  onNavigate: (view: "create" | "generation" | "arrangement") => void;
  onMidiLoaded: (midi: ImportedMidi | null) => void;
}) {
  const [prompt, setPrompt] = useState("");
  const [pickedFile, setPickedFile] = useState("");
  const [recording, setRecording] = useState(false);
  const [recordedUrl, setRecordedUrl] = useState("");
  const [midiImport, setMidiImport] = useState<ImportedMidi | null>(null);
  const [midiError, setMidiError] = useState("");
  const [showAll, setShowAll] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const recordChunks = useRef<Blob[]>([]);
  const selectFile = async (file?: File) => {
    if (!file) return;
    onMidiLoaded(null);
    setMidiImport(null);
    const extension = file.name.toLowerCase().split(".").pop();
    if (!extension || !["mid", "midi", "rmi", "smf"].includes(extension)) {
      setMidiError("请选择 .mid、.midi 或 .rmi 格式的 MIDI 文件。");
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      setPickedFile("文件超过 50MB");
      return;
    }
    setPickedFile(file.name);
    setMidiError("");
    try {
      const { BasicMIDI } = await import("spessasynth_core");
      const parsed = await BasicMIDI.fromFile(file);
      const channels = parsed
        .getNoteTimes()
        .map((notes, channel) => ({ channel, notes }))
        .filter((item) => item.notes.length > 0);
      if (!channels.length) throw new Error("MIDI 文件里没有可播放的音符。");
      const melodyChannels = channels.filter((item) => item.channel !== 9);
      const defaultChannel = [
        ...(melodyChannels.length ? melodyChannels : channels),
      ].sort((a, b) => b.notes.length - a.notes.length)[0];
      const midi: ImportedMidi = {
        name: file.name,
        duration: parsed.duration,
        channels,
        selectedChannel: defaultChannel.channel,
        notes: defaultChannel.notes,
      };
      if (midi.duration > 900 || midi.notes.length > 20000)
        throw new Error("文件过长或音符数量过多，请使用 15 分钟以内、最多 20,000 个音符的 MIDI。");
      setMidiImport(midi);
      onMidiLoaded(midi);
    } catch (error) {
      setMidiImport(null);
      onMidiLoaded(null);
      setMidiError(
        error instanceof Error ? error.message : "MIDI 文件无法读取。",
      );
    }
  };
  const toggleRecording = async () => {
    if (recorderRef.current?.state === "recording") {
      recorderRef.current.stop();
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      recordChunks.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size) recordChunks.current.push(event.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(recordChunks.current, {
          type: recorder.mimeType || "audio/webm",
        });
        if (recordedUrl) URL.revokeObjectURL(recordedUrl);
        setRecordedUrl(URL.createObjectURL(blob));
        setPickedFile(
          `哼唱录音.${recorder.mimeType.includes("mp4") ? "m4a" : "webm"}`,
        );
        stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
      };
      recorderRef.current = recorder;
      recorder.start();
      setRecording(true);
    } catch {
      setPickedFile("麦克风不可用或未获授权");
    }
  };
  return (
    <div className="app-shell stitch-view">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Activity size={20} />
          </div>
          <div>
            <div className="brand-name">SONIC FOUNDRY</div>
            <div className="brand-sub">GENERATIVE MUSIC WORKSPACE</div>
          </div>
        </div>
        <nav className="top-nav">
          <button className="active">Create</button>
          <button onClick={() => onNavigate("generation")}>Generation</button>
          <button onClick={() => onNavigate("arrangement")}>Arrangement</button>
        </nav>
        <div className="top-actions">
          <button
            className="export-btn"
            onClick={() => onNavigate("arrangement")}
          >
            <Download size={15} /> Export
          </button>
          <div className="avatar">F</div>
        </div>
      </header>
      <main className="create-page">
        <div className="stitch-kicker">
          <span className="status-dot" /> NEURAL AUDIO ENGINE V3.4 ACTIVE{" "}
          <span>/</span> SESSION #SF-8849-DX
        </div>
        <div className="create-title-row">
          <div>
            <h1>
              新建音乐创作 <em>/ New Creation</em>
            </h1>
            <p>从灵感描述、旋律哼唱或底层音乐参数开始构建你的音乐工程。</p>
          </div>
          <button
            className="empty-project"
            onClick={() => onNavigate("arrangement")}
          >
            <Plus size={16} /> 新建空白工程
          </button>
        </div>
        <div className="modality-grid">
          <div className="modality-card">
            <div className="modality-icon green">
              <Sparkles size={20} />
            </div>
            <div className="modality-heading">
              <strong>文字描述灵感</strong>
              <span>SEMANTIC PROMPT</span>
            </div>
            <p>输入音乐情绪、风格或场景，由生成引擎解析成可编辑的音乐草稿。</p>
            <textarea
              value={prompt}
              onChange={(event) => setPrompt(event.target.value)}
              placeholder="例如：夜晚城市感、低速、电影氛围的电子音乐"
              rows={4}
            />
            <div className="tag-row">
              {["夜幕氛围", "复古合成器", "赛博低保真"].map((tag) => (
                <button
                  key={tag}
                  onClick={() =>
                    setPrompt((value) => `${value}${value ? "，" : ""}${tag}`)
                  }
                >
                  {tag}
                </button>
              ))}
            </div>
            <button
              className="card-action"
              onClick={() => onNavigate("generation")}
            >
              基于描述配置生成 <span>→</span>
            </button>
          </div>
          <div className="modality-card">
            <div className="modality-icon purple">
              <Headphones size={20} />
            </div>
            <div className="modality-heading">
              <strong>旋律输入与哼唱</strong>
              <span>ACOUSTIC & PITCH-TO-MIDI</span>
            </div>
            <p>导入 MIDI 音符，用当前选择的采样乐器重演旋律。</p>
            <input
              ref={fileRef}
              className="visually-hidden"
              type="file"
              accept=".mid,.midi,.rmi,.smf,audio/midi,audio/x-midi"
              onChange={(event) => {
                const file = event.target.files?.[0];
                event.currentTarget.value = "";
                void selectFile(file);
              }}
            />
            <button
              className="drop-zone"
              onClick={() => fileRef.current?.click()}
              onDragOver={(event) => event.preventDefault()}
              onDrop={(event) => {
                event.preventDefault();
                selectFile(event.dataTransfer.files[0]);
              }}
            >
              ↑<b>拖入 MIDI 文件</b>
              <small>{pickedFile || "支持 MID、MIDI、RMI · 最大 50MB"}</small>
            </button>
            {midiError && (
              <p className="midi-error" role="alert">
                {midiError}
              </p>
            )}
            {midiImport && (
              <div className="midi-summary">
                <strong>
                  {midiImport.notes.length} 个音符 ·{" "}
                  {Math.ceil(midiImport.duration)} 秒
                </strong>
                <label>
                  旋律通道
                  <select
                    value={midiImport.selectedChannel}
                    onChange={(event) => {
                      const selectedChannel = Number(event.target.value);
                      const notes =
                        midiImport.channels.find(
                          (item) => item.channel === selectedChannel,
                        )?.notes ?? [];
                      const updated = { ...midiImport, selectedChannel, notes };
                      setMidiImport(updated);
                      onMidiLoaded(updated);
                    }}
                  >
                    {midiImport.channels.map((item) => (
                      <option key={item.channel} value={item.channel}>
                        通道 {item.channel + 1} · {item.notes.length} 音符
                      </option>
                    ))}
                  </select>
                </label>
              </div>
            )}
            {recordedUrl && (
              <audio className="recorded-audio" controls src={recordedUrl} />
            )}
            <button
              className={`record-row ${recording ? "recording" : ""}`}
              onClick={toggleRecording}
            >
              <span>●</span> {recording ? "停止录音" : "点击录制一段哼唱"}{" "}
              <small>{recording ? "正在录音" : "麦克风输入"}</small>
            </button>
            <button
              className="card-action"
              onClick={() =>
                onNavigate(midiImport ? "arrangement" : "generation")
              }
            >
              {midiImport ? "用当前乐器播放 MIDI" : "先导入 MIDI"}{" "}
              <span>→</span>
            </button>
          </div>
          <div className="modality-card">
            <div className="modality-icon orange">
              <SlidersHorizontal size={20} />
            </div>
            <div className="modality-heading">
              <strong>参数化创作</strong>
              <span>PARAMETRIC COMPOSITION</span>
            </div>
            <p>从风格、速度、调性和乐器开始，精确控制音乐的基础结构。</p>
            <div className="param-preview">
              <div>
                <span>STYLE</span>
                <b>Ambient</b>
              </div>
              <div>
                <span>BPM</span>
                <b>112</b>
              </div>
              <div>
                <span>KEY</span>
                <b>C minor</b>
              </div>
              <div>
                <span>MOOD</span>
                <b>Calm</b>
              </div>
            </div>
            <button
              className="card-action"
              onClick={() => onNavigate("generation")}
            >
              打开生成设置 <span>→</span>
            </button>
          </div>
        </div>
        <section className="recent-section">
          <div className="section-title">
            <span>最近创作 / RECENT PROJECTS</span>
            <button
              className="view-all"
              onClick={() => setShowAll((value) => !value)}
            >
              {showAll ? "收起 ↑" : "VIEW ALL →"}
            </button>
          </div>
          <div className="recent-grid">
            <button onClick={() => onNavigate("arrangement")}>
              <span className="recent-wave">∿ ∿ ∿</span>
              <b>Midnight Current</b>
              <small>C minor · 112 BPM · 4 bars</small>
            </button>
            {showAll && (
              <button onClick={() => onNavigate("arrangement")}>
                <span className="recent-wave">∿ ∿ ∿</span>
                <b>Saved sketch</b>
                <small>Local project · recent</small>
              </button>
            )}
            <button onClick={() => onNavigate("arrangement")}>
              <span className="recent-wave purple-wave">∿ ∿ ∿</span>
              <b>Soft Motion</b>
              <small>A minor · 96 BPM · 8 bars</small>
            </button>
            <button onClick={() => onNavigate("arrangement")}>
              <span className="recent-wave orange-wave">∿ ∿ ∿</span>
              <b>Untitled sketch</b>
              <small>Draft · edited 2 min ago</small>
            </button>
          </div>
        </section>
      </main>
    </div>
  );
}

function GenerationView({
  onNavigate,
}: {
  onNavigate: (view: "create" | "generation" | "arrangement") => void;
}) {
  const [style, setStyle] = useState("Ambient");
  const [tempo, setTempo] = useState(112);
  const [key, setKey] = useState("C minor");
  const [signature, setSignature] = useState("4/4");
  const [length, setLength] = useState(16);
  const [instruments, setInstruments] = useState([
    "Synth Pluck",
    "Soft Keys",
    "Kit 04",
  ]);
  return (
    <div className="app-shell stitch-view">
      <header className="topbar">
        <div className="brand">
          <div className="brand-mark">
            <Activity size={20} />
          </div>
          <div>
            <div className="brand-name">SONIC FOUNDRY</div>
            <div className="brand-sub">GENERATIVE MUSIC WORKSPACE</div>
          </div>
        </div>
        <nav className="top-nav">
          <button onClick={() => onNavigate("create")}>Create</button>
          <button className="active">Generation</button>
          <button onClick={() => onNavigate("arrangement")}>Arrangement</button>
        </nav>
        <div className="top-actions">
          <button
            className="icon-btn"
            onClick={() => onNavigate("create")}
            title="返回"
          >
            <RotateCcw size={17} />
          </button>
          <div className="avatar">F</div>
        </div>
      </header>
      <main className="generation-page">
        <div className="stitch-kicker">
          <span className="status-dot" /> CONFIGURE GENERATION <span>/</span>{" "}
          STEP 01 OF 02
        </div>
        <h1>
          生成设置 <em>/ Generation Setup</em>
        </h1>
        <p className="page-intro">
          把灵感整理成一份可编辑的音乐草稿，生成后会进入编曲工作台。
        </p>
        <div className="generation-layout">
          <section className="generation-form">
            <label>
              音乐描述 <span>SEMANTIC PROMPT</span>
            </label>
            <textarea
              defaultValue="夜晚城市感、低速、带电影氛围的电子音乐"
              rows={5}
            />
            <label>
              风格与情绪 <span>STYLE / MOOD</span>
            </label>
            <div className="choice-row">
              {["Ambient", "Cinematic", "Lo-fi", "House"].map((item) => (
                <button
                  key={item}
                  className={`choice ${style === item ? "selected" : ""}`}
                  onClick={() => setStyle(item)}
                >
                  {item}
                </button>
              ))}
            </div>
            <label>
              基础参数 <span>CORE PARAMETERS</span>
            </label>
            <div className="form-grid">
              <label>
                <small>BPM · {tempo}</small>
                <input
                  aria-label="速度 BPM"
                  type="range"
                  min="70"
                  max="160"
                  value={tempo}
                  onChange={(event) => setTempo(Number(event.target.value))}
                />
              </label>
              <button
                onClick={() =>
                  setKey(
                    (value) =>
                      ["C minor", "A minor", "G major", "D minor"][
                        (["C minor", "A minor", "G major", "D minor"].indexOf(
                          value,
                        ) +
                          1) %
                          4
                      ],
                  )
                }
              >
                <small>KEY</small>
                <b>{key}</b>
              </button>
              <button
                onClick={() =>
                  setSignature(
                    (value) =>
                      ["4/4", "3/4", "6/8"][
                        (["4/4", "3/4", "6/8"].indexOf(value) + 1) % 3
                      ],
                  )
                }
              >
                <small>SIGNATURE</small>
                <b>{signature}</b>
              </button>
              <label>
                <small>LENGTH · {length} bars</small>
                <input
                  aria-label="小节数"
                  type="range"
                  min="4"
                  max="64"
                  step="4"
                  value={length}
                  onChange={(event) => setLength(Number(event.target.value))}
                />
              </label>
            </div>
            <label>
              乐器与音色 <span>INSTRUMENTS</span>
            </label>
            <div className="instrument-list">
              {[
                ["Synth Pluck", "lime"],
                ["Soft Keys", "purple"],
                ["Folk Guitar", "purple"],
                ["Electric Guitar", "purple"],
                ["Bass", "lime"],
                ["Violin", "purple"],
                ["Cello", "orange"],
                ["Kit 04", "orange"],
              ].map(([name, color]) => (
                <button
                  key={name}
                  className={instruments.includes(name) ? "selected" : ""}
                  aria-pressed={instruments.includes(name)}
                  onClick={() =>
                    setInstruments((current) =>
                      current.includes(name)
                        ? current.filter((item) => item !== name)
                        : [...current, name],
                    )
                  }
                >
                  <span className={`track-color ${color}`} /> {name}{" "}
                  <b>{instruments.includes(name) ? "✓" : "+"}</b>
                </button>
              ))}
            </div>
            <button
              className="generate-btn"
              onClick={() => onNavigate("arrangement")}
            >
              <WandSparkles size={17} /> Generate editable sketch <span>→</span>
            </button>
          </section>
          <aside className="generation-preview">
            <div className="preview-label">PREVIEW OUTPUT</div>
            <div className="preview-screen">
              <div className="preview-grid" />
              <div className="preview-wave">∿∿∿∿∿∿∿</div>
              <b>Midnight Current</b>
              <span>
                {length} bars · {key} · {tempo} BPM
              </span>
            </div>
            <div className="preview-steps">
              <span>01</span>
              <i />
              <i />
              <i />
              <i />
              <span>16</span>
            </div>
            <p>
              生成后，你可以在编曲工作台中分别修改旋律、和弦、鼓点、音色和段落结构。
            </p>
          </aside>
        </div>
      </main>
    </div>
  );
}

function Track({
  color,
  name,
  label,
  active = false,
  muted = false,
  solo = false,
  onSelect,
  onMute,
  onSolo,
}: {
  color: string;
  name: string;
  label: string;
  active?: boolean;
  muted?: boolean;
  solo?: boolean;
  onSelect: () => void;
  onMute: () => void;
  onSolo: () => void;
}) {
  return (
    <div
      className={`track ${active ? "active" : ""} ${muted ? "muted-track" : ""}`}
    >
      <button className="track-select" onClick={onSelect} aria-pressed={active}>
        <div className={`track-color ${color}`} />
        <div className="track-info">
          <strong>{name}</strong>
          <span>{label}</span>
        </div>
      </button>
      <button
        className={`track-control ${muted ? "selected" : ""}`}
        aria-label={`静音 ${name}`}
        aria-pressed={muted}
        onClick={onMute}
        title="静音"
      >
        <Volume2 size={14} />
      </button>
      <button
        className={`track-control ${solo ? "selected" : ""}`}
        onClick={onSolo}
        aria-label={`独奏 ${name}`}
        aria-pressed={solo}
        title="独奏"
      >
        S
      </button>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
