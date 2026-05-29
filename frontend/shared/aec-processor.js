/**
 * Client-side AEC3 wrapper (webrtcaec3.js).
 * Feed TTS render reference via analyze(); process mic capture via process().
 */
(function (global) {
  'use strict';

  var DEFAULT_SAMPLE_RATE = 16000;

  function pcmS16ToFloat32(pcm) {
    var u8 = pcm instanceof Uint8Array ? pcm : new Uint8Array(pcm);
    var len = u8.length / 2;
    var out = new Float32Array(len);
    var view = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    for (var i = 0; i < len; i++) out[i] = view.getInt16(i * 2, true) / 32768;
    return out;
  }

  function float32ToPcmS16(f32) {
    var buf = new ArrayBuffer(f32.length * 2);
    var view = new DataView(buf);
    for (var i = 0; i < f32.length; i++) {
      var s = Math.max(-1, Math.min(1, f32[i]));
      view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var existing = document.querySelector('script[data-aec-lib="1"]');
      if (existing) {
        if (global.WebRtcAec3) return resolve();
        existing.addEventListener('load', function () { resolve(); });
        existing.addEventListener('error', reject);
        return;
      }
      var s = document.createElement('script');
      s.src = src;
      s.async = true;
      s.setAttribute('data-aec-lib', '1');
      s.onload = function () { resolve(); };
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function passthroughPipeline() {
    return {
      enabled: false,
      ready: true,
      feedRenderPcmS16: function () {},
      processCaptureFloat32: function (input) { return input; },
      processCaptureBlockToS16: function (inputF32) {
        return float32ToPcmS16(inputF32);
      },
      destroy: function () {},
    };
  }

  async function createPipeline(opts) {
    opts = opts || {};
    var sampleRate = opts.sampleRate || DEFAULT_SAMPLE_RATE;
    var scriptUrl = opts.webrtcAecScriptUrl || '/static/webrtcaec3-0.3.0.js';
    await loadScript(scriptUrl);
    if (typeof global.WebRtcAec3 !== 'function') {
      throw new Error('WebRtcAec3 not available after script load');
    }
    var mod = await global.WebRtcAec3();
    var aec = new mod.AEC3(sampleRate, 1, 1);
    return {
      enabled: true,
      ready: true,
      feedRenderPcmS16: function (pcmU8, renderSampleRate) {
        if (!pcmU8 || !pcmU8.length) return;
        var f32 = pcmS16ToFloat32(pcmU8);
        var options = renderSampleRate && renderSampleRate !== sampleRate
          ? { sampleRate: renderSampleRate }
          : undefined;
        aec.analyze([f32], options);
      },
      processCaptureFloat32: function (inputF32) {
        if (!inputF32 || !inputF32.length) return inputF32;
        var bufSz = aec.processSize([inputF32]);
        var outBuf = [new Float32Array(bufSz)];
        aec.process(outBuf, [inputF32]);
        return outBuf[0];
      },
      processCaptureBlockToS16: function (inputF32) {
        var cleaned = this.processCaptureFloat32(inputF32);
        return float32ToPcmS16(cleaned);
      },
      destroy: function () {
        try { if (aec && typeof aec.destroy === 'function') aec.destroy(); } catch (e) { /* ignore */ }
      },
    };
  }

  function parseEnabledFromUrl() {
    try {
      var p = new URLSearchParams(global.location.search);
      var v = (p.get('aec') || '').trim().toLowerCase();
      if (v === '1' || v === 'true' || v === 'yes' || v === 'on') return true;
      if (v === '0' || v === 'false' || v === 'no' || v === 'off') return false;
    } catch (e) { /* ignore */ }
    return null;
  }

  function resolveEnabled(audioConfig) {
    var fromUrl = parseEnabledFromUrl();
    if (fromUrl !== null) return fromUrl;
    if (audioConfig && typeof audioConfig.client_aec_enabled === 'boolean') {
      return audioConfig.client_aec_enabled;
    }
    return false;
  }

  global.ClientAec = {
    DEFAULT_SAMPLE_RATE: DEFAULT_SAMPLE_RATE,
    resolveEnabled: resolveEnabled,
    createPassthrough: passthroughPipeline,
    init: async function (opts) {
      var want = resolveEnabled(opts && opts.audioConfig);
      if (!want) return passthroughPipeline();
      try {
        var pipe = await createPipeline(opts || {});
        return pipe;
      } catch (err) {
        console.warn('[ClientAec] init failed, fallback passthrough:', err);
        return passthroughPipeline();
      }
    },
  };
})(typeof window !== 'undefined' ? window : globalThis);
