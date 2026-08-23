const APPLE_MUSIC_TOKEN_ENDPOINT = "/api/music/apple-token";

export const APPLE_MUSIC_URL = "https://music.apple.com/";

function providerError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

export function fisherYatesShuffle(items, random = Math.random) {
  const shuffled = [...items];
  for (let index = shuffled.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(random() * (index + 1));
    [shuffled[index], shuffled[swapIndex]] = [shuffled[swapIndex], shuffled[index]];
  }
  return shuffled;
}

export function createShuffleQueue(trackIds, previousTrackId = null, random = Math.random) {
  const queue = fisherYatesShuffle(trackIds, random);
  if (queue.length > 1 && queue[0] === previousTrackId) {
    const replacementIndex = queue.findIndex((trackId) => trackId !== previousTrackId);
    [queue[0], queue[replacementIndex]] = [queue[replacementIndex], queue[0]];
  }
  return queue;
}

export class MusicShuffleQueue {
  constructor(random = Math.random) {
    this.random = random;
    this.trackIds = [];
    this.queue = [];
    this.history = [];
    this.currentTrackId = null;
    this.shuffle = true;
  }

  setTracks(trackIds, { shuffle = true, lastTrackId = null } = {}) {
    this.trackIds = [...new Set(trackIds.filter(Boolean))];
    this.shuffle = shuffle;
    this.history = [];
    this.currentTrackId = null;
    this.queue = this.makeRound(lastTrackId);
  }

  setShuffle(shuffle) {
    this.shuffle = Boolean(shuffle);
    const remaining = this.queue.filter((trackId) => trackId !== this.currentTrackId);
    this.queue = this.shuffle
      ? createShuffleQueue(remaining, this.currentTrackId, this.random)
      : this.trackIds.filter((trackId) => trackId !== this.currentTrackId);
  }

  makeRound(previousTrackId = this.currentTrackId) {
    if (!this.shuffle) return [...this.trackIds];
    return createShuffleQueue(this.trackIds, previousTrackId, this.random);
  }

  next() {
    if (!this.trackIds.length) return null;
    if (!this.queue.length) this.queue = this.makeRound(this.currentTrackId);
    const nextTrackId = this.queue.shift() || null;
    if (this.currentTrackId && nextTrackId !== this.currentTrackId) {
      this.history.push(this.currentTrackId);
    }
    this.currentTrackId = nextTrackId;
    return nextTrackId;
  }

  previous() {
    if (!this.history.length) return this.currentTrackId;
    if (this.currentTrackId) this.queue.unshift(this.currentTrackId);
    this.currentTrackId = this.history.pop();
    return this.currentTrackId;
  }
}

function normalizeTrack(track) {
  const attributes = track?.attributes || {};
  return {
    id: String(track?.id || ""),
    name: attributes.name || "未知曲目",
    artistName: attributes.artistName || "未知艺人",
    artwork: attributes.artwork || null,
  };
}

async function loadMusicKitScript() {
  if (window.MusicKit) return window.MusicKit;
  const existing = document.querySelector('script[data-frostfire-musickit="true"]');
  if (!existing) {
    const script = document.createElement("script");
    script.src = "https://js-cdn.music.apple.com/musickit/v3/musickit.js";
    script.async = true;
    script.dataset.frostfireMusickit = "true";
    document.head.appendChild(script);
  }
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(
      () => reject(providerError("initialization_failed", "Apple Music 初始化超时。")),
      12000,
    );
    const onReady = () => {
      window.clearTimeout(timeout);
      resolve(window.MusicKit);
    };
    if (window.MusicKit) return onReady();
    document.addEventListener("musickitloaded", onReady, { once: true });
  });
}

class AppleMusicProvider {
  constructor() {
    this.status = "idle";
    this.instance = null;
    this.authorized = false;
    this.tracks = [];
    this.albumId = null;
    this.storefront = "au";
    this.currentTrack = null;
    this.queueController = new MusicShuffleQueue();
    this.initializePromise = null;
    this.tokenEndpoint = APPLE_MUSIC_TOKEN_ENDPOINT;
  }

  setTokenEndpoint(tokenEndpoint) {
    this.tokenEndpoint = tokenEndpoint || APPLE_MUSIC_TOKEN_ENDPOINT;
  }

  snapshot(extra = {}) {
    return {
      provider: "apple_music",
      status: this.status,
      configured: Boolean(this.instance),
      authorized: this.authorized,
      tracks: [...this.tracks],
      queue: [...this.queueController.queue],
      history: [...this.queueController.history],
      currentTrack: this.currentTrack,
      ...extra,
    };
  }

  async initialize() {
    if (this.instance) return this.snapshot();
    if (this.initializePromise) return this.initializePromise;
    this.status = "loading";
    this.initializePromise = (async () => {
      let response;
      try {
        response = await fetch(this.tokenEndpoint, {
          method: "GET",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        });
      } catch (_) {
        this.status = "network_error";
        return this.snapshot({ error: "暂时无法连接 Apple Music 配置服务。" });
      }
      if (response.status === 404) {
        this.status = "not_configured";
        return this.snapshot();
      }
      if (!response.ok) {
        this.status = "initialization_failed";
        return this.snapshot({ error: "Apple Music 配置暂时不可用。" });
      }
      const configuration = await response.json().catch(() => ({}));
      if (!configuration.developer_token || !configuration.album_id) {
        this.status = "not_configured";
        return this.snapshot();
      }
      try {
        const MusicKit = await loadMusicKitScript();
        MusicKit.configure({
          developerToken: configuration.developer_token,
          storefrontId: configuration.storefront || "au",
          app: { name: "Frostfire Music Dimension", build: "1.0.0" },
        });
        this.instance = MusicKit.getInstance();
        this.albumId = String(configuration.album_id);
        this.storefront = configuration.storefront || "au";
        this.status = "ready";
        return this.snapshot();
      } catch (error) {
        this.status = "initialization_failed";
        return this.snapshot({ error: error.message || "Apple Music 初始化失败。" });
      }
    })();
    try {
      return await this.initializePromise;
    } finally {
      this.initializePromise = null;
    }
  }

  async authorize() {
    if (!this.instance) throw providerError("not_configured", "Apple Music 尚未配置。");
    try {
      await this.instance.authorize();
      this.authorized = true;
      this.status = "authorized";
      return this.snapshot();
    } catch (_) {
      this.authorized = false;
      this.status = "authorization_denied";
      throw providerError("authorization_denied", "Apple Music 授权未完成。");
    }
  }

  async loadAlbum({ shuffle = true, lastTrackId = null } = {}) {
    if (!this.instance || !this.albumId) throw providerError("not_configured", "Apple Music 尚未配置。");
    try {
      const response = await this.instance.api.music(
        `/v1/catalog/${this.storefront}/albums/${this.albumId}`,
        { include: "tracks" },
      );
      const album = response?.data?.data?.[0] || response?.data?.[0];
      const rawTracks = album?.relationships?.tracks?.data || [];
      this.tracks = rawTracks.map(normalizeTrack).filter((track) => track.id);
      if (!this.tracks.length) throw providerError("album_unavailable", "当前地区暂时无法读取这张专辑。");
      this.queueController.setTracks(this.tracks.map((track) => track.id), { shuffle, lastTrackId });
      this.status = "album_ready";
      return this.snapshot();
    } catch (error) {
      this.status = error.code || "album_unavailable";
      throw error.code ? error : providerError("album_unavailable", "当前地区暂时无法读取这张专辑。");
    }
  }

  trackById(trackId) {
    return this.tracks.find((track) => track.id === trackId) || null;
  }

  async playTrack(trackId) {
    if (!this.instance) throw providerError("not_configured", "Apple Music 尚未配置。");
    try {
      await this.instance.setQueue({ song: trackId });
      await this.instance.play();
      this.currentTrack = this.trackById(trackId);
      this.status = "playing";
      return this.snapshot();
    } catch (error) {
      const blocked = error?.name === "NotAllowedError" || /gesture|autoplay/i.test(error?.message || "");
      this.status = blocked ? "autoplay_blocked" : "playback_failed";
      throw providerError(
        this.status,
        blocked ? "点击继续播放" : "Apple Music 暂时无法播放这首歌曲。",
      );
    }
  }

  async pause() {
    if (!this.instance) return this.snapshot();
    await this.instance.pause();
    this.status = "paused";
    return this.snapshot();
  }

  async resume() {
    if (!this.instance) throw providerError("not_configured", "Apple Music 尚未配置。");
    try {
      await this.instance.play();
      this.status = "playing";
      return this.snapshot();
    } catch (_) {
      this.status = "autoplay_blocked";
      throw providerError("autoplay_blocked", "点击继续播放");
    }
  }

  async next() {
    const trackId = this.queueController.next();
    return trackId ? this.playTrack(trackId) : this.snapshot();
  }

  async previous() {
    const trackId = this.queueController.previous();
    return trackId ? this.playTrack(trackId) : this.snapshot();
  }

  async setShuffle(shuffle) {
    this.queueController.setShuffle(shuffle);
    return this.snapshot();
  }

  async setVolume(value) {
    if (this.instance) this.instance.volume = Math.min(1, Math.max(0, Number(value)));
    return this.snapshot();
  }

  async getPlaybackState() {
    return this.snapshot();
  }

  async destroy() {
    if (this.instance) {
      try { await this.instance.stop(); } catch (_) {}
    }
    this.currentTrack = null;
    this.status = this.instance ? "ready" : "not_configured";
    return this.snapshot();
  }
}

export const musicProvider = new AppleMusicProvider();
