/**
 * TTFS Web Audio widget — shared by index.html, embed.html, and inlined hosts (e.g. t2sbg).
 * Same-window: window.TTFS.cloneMicForConsumer()
 * In-iframe parent: iframe.contentWindow.TTFS.cloneMicForConsumer()
 * Optional host TTS tap: TTFS.attachPlaybackMediaStream(dest.stream) from createMediaStreamDestination()
 * Latency (latMs): time from end of mic utterance (Hang ms below Mic threshold) to first playback onset (Pb RMS rising edge), TTFS AudioContext clock; debounced ~120ms after utterance end.
 * Auto mic: await TTFS.startMic() — idempotent if tracks already live.
 * Notifications: postMessage { source: 'ttfs-embed', type, ... } when embedded in iframe.
 * Same-window: CustomEvent `ttfs-utterance-end` (mic hang satisfied) and `ttfs-e2e-latency` (playback onset) with detail.perfNow for host alignment.
 */
(function () {
  const PARENT_TARGET = document.body.dataset.ttfsParentOrigin || '*';

  /**
   * Host pages (e.g. t2sbg) may duplicate generic ids like btnStop — use prefixed ids there.
   * Standalone embed.html keeps legacy ids; resolver tries prefixed first, then legacy.
   */
  const TTFS_DOM_IDS = {
    btnMic: 'ttfs-btn-mic',
    btnShare: 'ttfs-btn-share',
    btnStop: 'ttfs-btn-stop',
    chkVoice: 'ttfs-chk-voice',
    stateLab: 'ttfs-state-lab',
    latMs: 'ttfs-lat-ms',
    labMicT: 'ttfs-lab-mic-t',
    labPbT: 'ttfs-lab-pb-t',
    labHang: 'ttfs-lab-hang',
    rngMic: 'ttfs-rng-mic',
    rngPb: 'ttfs-rng-pb',
    rngHang: 'ttfs-rng-hang',
    waveformCanvasMic: 'ttfs-waveform-mic',
    waveformCanvasPlayback: 'ttfs-waveform-pb',
    timelineCanvas: 'ttfs-timeline',
    panelMedia: 'ttfs-panel-media',
    keepAlive: 'ttfs-keep-alive',
    log: 'ttfs-log',
  };

  function $(legacyId) {
    const alt = TTFS_DOM_IDS[legacyId];
    if (alt) {
      const scoped = document.getElementById(alt);
      if (scoped) return scoped;
    }
    return document.getElementById(legacyId);
  }

  function pm(msg) {
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({ source: 'ttfs-embed', version: 1, ...msg }, PARENT_TARGET);
      }
    } catch (_) {}
  }

  /** Same-window hosts (e.g. t2sbg) align WS milestones via performance.now(). */
  function ttfsEmitHost(name, detail) {
    const d = detail && typeof detail === 'object' ? detail : {};
    try {
      window.dispatchEvent(new CustomEvent(name, { detail: d }));
    } catch (_) {}
    try {
      pm(Object.assign({ type: name }, d));
    } catch (_) {}
  }

  const log = (msg, err) => {
    const el = $('log');
    if (!el) return;
    el.textContent = msg;
    el.classList.toggle('err', !!err);
  };

  const btnMic = $('btnMic');
  const btnShare = $('btnShare');
  const btnStop = $('btnStop');
  const stateLab = $('stateLab');
  const keepAlive = $('keepAlive');

  const cvMic = $('waveformCanvasMic');
  const cvPb = $('waveformCanvasPlayback');
  const cvTl = $('timelineCanvas');

  const rngMic = $('rngMic');
  const rngPb = $('rngPb');
  const rngHang = $('rngHang');
  const labMicT = $('labMicT');
  const labPbT = $('labPbT');
  const labHang = $('labHang');

  function syncLabels() {
    if (labMicT) labMicT.textContent = Number(rngMic.value).toFixed(3);
    if (labPbT) labPbT.textContent = Number(rngPb.value).toFixed(3);
    if (labHang) labHang.textContent = rngHang.value;
  }
  if (rngMic) rngMic.oninput = rngPb.oninput = rngHang.oninput = syncLabels;
  syncLabels();

  /** @type {MediaStream | null} */
  let streamMic = null;
  /** @type {AudioContext | null} */
  let ctx = null;

  window.TTFS = {
    version: 1,
    getMicStream() {
      return streamMic;
    },
    cloneMicForConsumer() {
      if (!streamMic) return null;
      try {
        return streamMic.clone();
      } catch (e) {
        console.warn('[TTFS] cloneMicForConsumer failed', e);
        return null;
      }
    },
    getAudioContext() {
      return ctx;
    },
    /** Parent can call to stop capture from host page (same origin). */
    stop() {
      stopAll();
    },
    /**
     * Feed AI/host TTS into the Playback analyser without getDisplayMedia.
     * Stream should come from another AudioContext's MediaStreamDestination.
     */
    attachPlaybackMediaStream(stream) {
      attachPlaybackFromStream(stream);
    },
    /** Clear playback tap / tab-share playback path (e.g. when host TTS AudioContext closes). */
    detachPlaybackMediaStream() {
      teardownPlaybackAnalyser();
    },
  };

  /** @type {AnalyserNode | null} */
  let anMic = null;
  /** @type {AnalyserNode | null} */
  let anPb = null;
  /** @type {MediaStreamAudioSourceNode | null} */
  let srcPb = null;
  /** @type {MediaStream | null} */
  let streamShare = null;

  function teardownPlaybackAnalyser() {
    if (srcPb) {
      try {
        srcPb.disconnect();
      } catch (_) {}
      srcPb = null;
    }
    anPb = null;
    if (streamShare) {
      streamShare.getTracks().forEach((t) => t.stop());
      streamShare = null;
    }
    if (keepAlive) keepAlive.srcObject = null;
    histPb.length = 0;
    tlPb.length = 0;
    if (btnShare) btnShare.disabled = false;
  }

  function attachPlaybackFromStream(stream) {
    if (!stream || !stream.getAudioTracks || !stream.getAudioTracks().length) return;
    teardownPlaybackAnalyser();
    ensureCtx();
    try {
      srcPb = ctx.createMediaStreamSource(stream);
      anPb = ctx.createAnalyser();
      anPb.smoothingTimeConstant = 0.35;
      anPb.minDecibels = -90;
      anPb.maxDecibels = -10;
      srcPb.connect(anPb);
      if (ctx.state === 'suspended') ctx.resume().catch(() => {});
      log('');
      pm({ type: 'ttfs-playback-ready' });
      ensureLoop();
    } catch (e) {
      log('Playback tap 失败 / tap error: ' + e, true);
      pm({ type: 'ttfs-error', message: String(e) });
    }
  }

  const STRIP_CAP = 240;
  const histMic = [];
  const histPb = [];
  const tlMic = [];
  const tlPb = [];
  const TL_CAP = 2400;
  const TL_WIN_S = 5;

  let raf = 0;

  const Phase = { WAIT: 0, SPEAK: 1, WAIT_PB: 2 };
  let phase = Phase.WAIT;
  let quietMs = 0;
  let tSilenceAudio = null;
  let lastPbFire = -1e9;
  /** Playback RMS previous frame — rising edge detects «speaker starts» after mic segment ends */
  let prevRPb = 0;
  /** TTFS AudioContext time: do not accept playback triggers before this (debounce after utterance end) */
  let waitPbArmUntil = -Infinity;
  const WAIT_PB_DEBOUNCE_S = 0.12;

  function ensureCtx() {
    if (!ctx) ctx = new AudioContext({ latencyHint: 'interactive' });
    return ctx;
  }

  function fitCanvasEl(c) {
    if (!c) return;
    const r = c.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.floor(r.width * dpr));
    const h = Math.max(1, Math.floor(r.height * dpr));
    if (c.width !== w || c.height !== h) {
      c.width = w;
      c.height = h;
    }
  }

  function rmsFromAnalyser(an) {
    const n = an.fftSize;
    const buf = new Float32Array(n);
    an.getFloatTimeDomainData(buf);
    let s = 0;
    for (let i = 0; i < n; i++) s += buf[i] * buf[i];
    return Math.sqrt(s / n);
  }

  function peakFromAnalyser(an) {
    const n = an.fftSize;
    const buf = new Float32Array(n);
    an.getFloatTimeDomainData(buf);
    let p = 0;
    for (let i = 0; i < n; i++) p = Math.max(p, Math.abs(buf[i]));
    return p;
  }

  function pushRing(arr, v, cap) {
    arr.push(v);
    if (arr.length > cap) arr.splice(0, arr.length - cap);
  }

  function drawVerticalStrip(canvas, hist, color) {
    if (!canvas) return;
    fitCanvasEl(canvas);
    const g = canvas.getContext('2d');
    const w = canvas.width;
    const h = canvas.height;
    g.fillStyle = '#0a0c10';
    g.fillRect(0, 0, w, h);
    const cx = w * 0.5;
    const n = Math.min(hist.length, STRIP_CAP);
    if (n < 2) return;
    g.strokeStyle = color;
    g.lineWidth = Math.max(1, w / 80);
    g.beginPath();
    for (let i = 0; i < n; i++) {
      const v = hist[hist.length - n + i];
      const y = h - ((i + 1) / n) * h;
      const half = Math.min(w * 0.46, v * w * 2.2);
      g.moveTo(cx - half, y);
      g.lineTo(cx + half, y);
    }
    g.stroke();
    g.strokeStyle = '#2a3142';
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(cx, 0);
    g.lineTo(cx, h);
    g.stroke();
  }

  let markerSilence = null;
  let markerPb = null;

  function drawTimeline(nowAudio) {
    if (!cvTl) return;
    fitCanvasEl(cvTl);
    const g = cvTl.getContext('2d');
    const w = cvTl.width;
    const h = cvTl.height;
    g.fillStyle = '#0a0c10';
    g.fillRect(0, 0, w, h);

    const pad = 28;
    const plotW = w - pad * 2;
    const plotH = (h - pad * 2) / 2 - 6;
    const yMic = pad;
    const yPb = pad + plotH + 18;

    function drawBand(title, y0, hist, color) {
      g.fillStyle = '#8b93a7';
      g.font = `${Math.max(10, h * 0.045)}px ui-sans-serif`;
      g.fillText(title, 8, y0 + 12);
      g.strokeStyle = '#222938';
      for (let i = 0; i <= 4; i++) {
        const x = pad + (i / 4) * plotW;
        g.beginPath();
        g.moveTo(x, y0);
        g.lineTo(x, y0 + plotH);
        g.stroke();
      }
      const tMax = nowAudio;
      const tMin = tMax - TL_WIN_S;
      g.strokeStyle = color;
      g.lineWidth = 1.25;
      g.beginPath();
      let started = false;
      for (let i = 0; i < hist.length; i++) {
        const p = hist[i];
        if (p.t < tMin) continue;
        if (p.t > tMax) break;
        const x = pad + ((p.t - tMin) / TL_WIN_S) * plotW;
        const yy = y0 + plotH * (1 - Math.min(1, p.v * 4));
        if (!started) {
          g.moveTo(x, yy);
          started = true;
        } else {
          g.lineTo(x, yy);
        }
      }
      g.stroke();
    }

    drawBand('Mic', yMic, tlMic, '#5eead4');
    drawBand('Playback', yPb, tlPb, '#a78bfa');

    function drawMarker(tAud, _y0, col, dash) {
      if (tAud == null) return;
      if (tAud < nowAudio - TL_WIN_S || tAud > nowAudio) return;
      const x = pad + ((tAud - (nowAudio - TL_WIN_S)) / TL_WIN_S) * plotW;
      g.setLineDash(dash || []);
      g.strokeStyle = col;
      g.lineWidth = 1.5;
      g.beginPath();
      g.moveTo(x, pad);
      g.lineTo(x, h - pad);
      g.stroke();
      g.setLineDash([]);
    }
    drawMarker(markerSilence, yMic, '#fbbf24', [6, 6]);
    drawMarker(markerPb, yMic, '#34d399', [4, 4]);

    g.fillStyle = '#6b728c';
    g.font = `${Math.max(9, h * 0.038)}px ui-monospace`;
    for (let i = 0; i <= 5; i++) {
      const sec = (-TL_WIN_S + (i / 5) * TL_WIN_S).toFixed(1);
      const x = pad + (i / 5) * plotW - 10;
      g.fillText(sec + 's', x, h - 8);
    }
  }

  function stepLatency(rMic, rPb, nowAudio, dtFrame) {
    const micTh = Number(rngMic.value);
    const pbTh = Number(rngPb.value);
    const hangMs = Number(rngHang.value);

    if (phase === Phase.WAIT) {
      if (rMic >= micTh) {
        phase = Phase.SPEAK;
        markerSilence = markerPb = null;
      }
      quietMs = 0;
      prevRPb = rPb;
      return;
    }
    if (phase === Phase.SPEAK) {
      if (rMic < micTh) quietMs += dtFrame * 1000;
      else quietMs = 0;
      if (quietMs >= hangMs) {
        phase = Phase.WAIT_PB;
        tSilenceAudio = nowAudio;
        markerSilence = nowAudio;
        quietMs = 0;
        prevRPb = 0;
        waitPbArmUntil = nowAudio + WAIT_PB_DEBOUNCE_S;
        const perfNow = typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
        ttfsEmitHost('ttfs-utterance-end', {
          perfNow,
          hangMs,
          nowAudio,
        });
        return;
      }
      prevRPb = rPb;
      return;
    }
    if (phase === Phase.WAIT_PB) {
      const cd = 0.85;
      const armed = nowAudio >= waitPbArmUntil;
      const playbackStarts = prevRPb < pbTh && rPb >= pbTh;
      if (
        armed &&
        playbackStarts &&
        tSilenceAudio != null &&
        nowAudio - lastPbFire > cd
      ) {
        const ms = (nowAudio - tSilenceAudio) * 1000;
        markerPb = nowAudio;
        lastPbFire = nowAudio;
        const el = $('latMs');
        if (el) el.textContent = ms.toFixed(0) + ' ms';
        const perfNow = typeof performance !== 'undefined' && performance.now ? performance.now() : Date.now();
        pm({ type: 'ttfs-latency-ms', ms: Math.round(ms), perfNow });
        ttfsEmitHost('ttfs-e2e-latency', {
          ms: Math.round(ms),
          perfNow,
          nowAudioPlayback: nowAudio,
        });
        phase = Phase.WAIT;
        tSilenceAudio = null;
      }
    }
    prevRPb = rPb;
  }

  let lastT = null;
  function loop(tWall) {
    raf = requestAnimationFrame(loop);
    const ac = ctx;
    if (!ac) return;
    if (ac.state === 'suspended') {
      ac.resume().catch(() => {});
      return;
    }

    if (lastT == null) lastT = tWall;
    const dtFrame = Math.min(0.05, Math.max(0, (tWall - lastT) / 1000));
    lastT = tWall;

    const nowAudio = ac.currentTime;

    let rMic = 0;
    let rPb = 0;
    if (anMic) {
      anMic.fftSize = 2048;
      rMic = rmsFromAnalyser(anMic);
      const pk = peakFromAnalyser(anMic);
      pushRing(histMic, pk, STRIP_CAP);
      pushRing(tlMic, { t: nowAudio, v: rMic }, TL_CAP);
    }
    if (anPb) {
      anPb.fftSize = 2048;
      rPb = rmsFromAnalyser(anPb);
      const pk = peakFromAnalyser(anPb);
      pushRing(histPb, pk, STRIP_CAP);
      pushRing(tlPb, { t: nowAudio, v: rPb }, TL_CAP);
    }

    stepLatency(rMic, rPb, nowAudio, dtFrame);

    drawVerticalStrip(cvMic, histMic, '#5eead4');
    drawVerticalStrip(cvPb, histPb, '#a78bfa');
    drawTimeline(nowAudio);

    const live = [];
    if (anMic) live.push('Mic');
    if (anPb) live.push('Playback');
    if (stateLab) stateLab.textContent = live.length ? '运行中 · ' + live.join(' + ') : '未启动';
  }

  function ensureLoop() {
    if (!raf) {
      lastT = null;
      raf = requestAnimationFrame(loop);
    }
  }

  function micAudioConstraints() {
    const voice = $('chkVoice') && $('chkVoice').checked;
    return {
      echoCancellation: !!voice,
      noiseSuppression: !!voice,
      autoGainControl: !!voice,
    };
  }

  async function captureMic() {
    if (streamMic) {
      const tracks = streamMic.getAudioTracks();
      if (tracks.length && tracks.every((t) => t.readyState === 'live')) {
        ensureLoop();
        return;
      }
      streamMic.getTracks().forEach((t) => t.stop());
      streamMic = null;
    }
    try {
      ensureCtx();
      streamMic = await navigator.mediaDevices.getUserMedia({
        audio: micAudioConstraints(),
        video: false,
      });
      const src = ctx.createMediaStreamSource(streamMic);
      anMic = ctx.createAnalyser();
      anMic.smoothingTimeConstant = 0.35;
      anMic.minDecibels = -90;
      anMic.maxDecibels = -10;
      src.connect(anMic);
      if (ctx.state === 'suspended') await ctx.resume();
      log('');
      if (btnMic) btnMic.disabled = true;
      const cv = $('chkVoice');
      if (cv) cv.disabled = true;
      const ev = new CustomEvent('ttfs-mic-ready', {
        detail: {
          stream: streamMic,
          cloneMicForConsumer: () => window.TTFS.cloneMicForConsumer(),
          audioContext: ctx,
        },
      });
      window.dispatchEvent(ev);
      pm({ type: 'ttfs-mic-ready' });
      ensureLoop();
    } catch (e) {
      log('麦克风权限失败 / Mic error: ' + e, true);
      pm({ type: 'ttfs-error', message: String(e) });
      throw e;
    }
  }

  window.TTFS.startMic = captureMic;

  async function startShare() {
    try {
      teardownPlaybackAnalyser();
      ensureCtx();
      if (!navigator.mediaDevices.getDisplayMedia) {
        log('浏览器不支持 getDisplayMedia / not supported', true);
        return;
      }
      streamShare = await navigator.mediaDevices.getDisplayMedia({
        video: { width: 320, height: 180, frameRate: 5 },
        audio: true,
      });
      if (keepAlive) {
        keepAlive.srcObject = streamShare;
        await keepAlive.play().catch(() => {});
      }

      const aTracks = streamShare.getAudioTracks();
      if (!aTracks.length) {
        log('共享流里没有音频轨 — 请勾选「共享音频」或换 Chrome / 换共享目标。', true);
        streamShare.getTracks().forEach((t) => t.stop());
        streamShare = null;
        return;
      }

      srcPb = ctx.createMediaStreamSource(streamShare);
      anPb = ctx.createAnalyser();
      anPb.smoothingTimeConstant = 0.35;
      anPb.minDecibels = -90;
      anPb.maxDecibels = -10;
      srcPb.connect(anPb);

      streamShare.getVideoTracks().forEach((t) => {
        t.onended = () => stopAll();
      });
      aTracks.forEach((t) => {
        t.onended = () => stopAll();
      });

      if (ctx.state === 'suspended') await ctx.resume();
      log('');
      if (btnShare) btnShare.disabled = true;
      pm({ type: 'ttfs-playback-ready' });
      ensureLoop();
    } catch (e) {
      log('共享取消或失败 / Share error: ' + e, true);
      pm({ type: 'ttfs-error', message: String(e) });
    }
  }

  function stopAll() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    lastT = null;

    if (streamMic) streamMic.getTracks().forEach((t) => t.stop());
    teardownPlaybackAnalyser();
    streamMic = null;
    if (keepAlive) keepAlive.srcObject = null;

    anMic = null;
    histMic.length = histPb.length = 0;
    tlMic.length = tlPb.length = 0;
    phase = Phase.WAIT;
    markerSilence = markerPb = null;
    prevRPb = 0;
    waitPbArmUntil = -Infinity;
    lastPbFire = -1e9;
    const latReset = $('latMs');
    if (latReset) latReset.textContent = '—';

    if (ctx) {
      ctx.close().catch(() => {});
      ctx = null;
    }

    if (btnMic) btnMic.disabled = false;
    if (btnShare) btnShare.disabled = false;
    const cv = $('chkVoice');
    if (cv) cv.disabled = false;
    if (stateLab) stateLab.textContent = '未启动';
    log('已停止。');
    window.dispatchEvent(new CustomEvent('ttfs-mic-stopped', { detail: {} }));
    pm({ type: 'ttfs-mic-stopped' });
  }

  if (btnMic) btnMic.onclick = () => captureMic().catch(() => {});
  if (btnShare) btnShare.onclick = () => startShare();
  if (btnStop) btnStop.onclick = () => stopAll();

  window.addEventListener('resize', () => {
    drawVerticalStrip(cvMic, histMic, '#5eead4');
    drawVerticalStrip(cvPb, histPb, '#a78bfa');
    if (ctx) drawTimeline(ctx.currentTime);
  });
})();
