"use strict";
// Shared by the recorder (index.html) and the detector (detect.html).
const $ = (id) => document.getElementById(id);

// Footer with the deployed commit hash, so a redeploy is visible.
fetch("api/version").then((r) => r.json()).then(({ version }) => {
  const f = document.createElement("footer");
  f.textContent = "build " + version;
  f.style.cssText = "margin-top:24px;color:#888;font-size:12px;text-align:center";
  document.body.appendChild(f);
}).catch(() => {});

// ---------- mic capture ----------
async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({
    // Off: AGC / NS / AEC all distort or gate pure tones.
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false, channelCount: 1 },
  });
  const ctx = new AudioContext();
  await ctx.audioWorklet.addModule("recorder-worklet.js");
  const src = ctx.createMediaStreamSource(stream);
  const node = new AudioWorkletNode(ctx, "recorder");
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 2048;
  analyser.smoothingTimeConstant = 0;
  const chunks = [];
  node.port.onmessage = (e) => chunks.push(e.data);
  src.connect(node).connect(ctx.destination); // node outputs silence; connection keeps it pulled
  src.connect(analyser);
  const track = stream.getAudioTracks()[0];
  const settings = track.getSettings();
  // Phones mute the mic when the screen locks: the worklet keeps running and records zeros. Keep the screen
  // on (the lock is dropped whenever the tab is hidden, so take it again on return) and flag any mute.
  let lock = null, done = false;
  const wake = async () => {
    if (!done && document.visibilityState === "visible" && navigator.wakeLock)
      lock = await navigator.wakeLock.request("screen").catch(() => null);
  };
  document.addEventListener("visibilitychange", wake);
  await wake();
  settings.wakeLock = !!lock;
  track.onmute = () => { settings.mutedDuringRecording = true; };
  return {
    analyser, sr: ctx.sampleRate, settings, t0: performance.now(),
    async finish() {
      done = true;
      document.removeEventListener("visibilitychange", wake);
      if (lock) lock.release().catch(() => {});
      stream.getTracks().forEach((t) => t.stop());
      await ctx.close();
      const n = chunks.reduce((a, c) => a + c.length, 0);
      const samples = new Float32Array(n);
      let o = 0;
      for (const c of chunks) { samples.set(c, o); o += c.length; }
      return { samples, sr: ctx.sampleRate, settings };
    },
  };
}

const micErrorText = (e) => "Mic error: " + e.message + (location.protocol !== "https:" ? " (needs HTTPS)" : "");

// ---------- live scrolling view (AnalyserNode) ----------
function liveInit(c) {
  c.width = c.clientWidth * devicePixelRatio;
  const g = c.getContext("2d");
  g.fillStyle = "#000";
  g.fillRect(0, 0, c.width, c.height);
}

function liveColumn(c, mic, fmax, range) {
  const g = c.getContext("2d");
  const bins = new Float32Array(mic.analyser.frequencyBinCount);
  mic.analyser.getFloatFrequencyData(bins);
  const nb = Math.min(bins.length, Math.floor(fmax / (mic.sr / mic.analyser.fftSize)));
  g.drawImage(c, -2, 0); // scroll left by 2px, draw new column on the right
  // running max with slow decay so silence stays dark
  mic.top = Math.max((mic.top ?? -100) - 0.05, ...bins.subarray(1, nb));
  for (let y = 0; y < c.height; y++) {
    const b = Math.floor((1 - y / c.height) * nb);
    g.fillStyle = cmapCss((bins[b] - (mic.top - range)) / range);
    g.fillRect(c.width - 2, y, 2, 1);
  }
}

// ---------- STFT ----------
function fft(re, im) { // in-place radix-2
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { [re[i], re[j]] = [re[j], re[i]]; [im[i], im[j]] = [im[j], im[i]]; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < len / 2; k++) {
        const a = i + k, b = a + len / 2;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti;
        re[a] += tr; im[a] += ti;
        [cr, ci] = [cr * wr - ci * wi, cr * wi + ci * wr];
      }
    }
  }
}

function stft(x, nfft, hop) {
  const win = new Float32Array(nfft).map((_, i) => 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / nfft));
  const nf = Math.max(1, Math.floor((x.length - nfft) / hop) + 1);
  const nb = nfft / 2 + 1;
  const S = new Float32Array(nf * nb); // dB, frame-major
  const re = new Float32Array(nfft), im = new Float32Array(nfft);
  const norm = nfft / 4; // Hann coherent gain -> full-scale sine ≈ 0 dB
  for (let f = 0; f < nf; f++) {
    im.fill(0);
    for (let i = 0; i < nfft; i++) re[i] = (x[f * hop + i] || 0) * win[i];
    fft(re, im);
    for (let k = 0; k < nb; k++)
      S[f * nb + k] = 10 * Math.log10((re[k] ** 2 + im[k] ** 2) / norm ** 2 + 1e-20);
  }
  return { S, nf, nb };
}

// ---------- drawing ----------
// viridis control points
const VIR = [[68,1,84],[72,40,120],[62,74,137],[49,104,142],[38,130,142],
             [31,158,137],[53,183,121],[109,205,89],[180,222,44],[253,231,37]];
function cmap(v) {
  v = Number.isFinite(v) ? Math.min(1, Math.max(0, v)) * (VIR.length - 1) : 0; // -Inf dB on silence
  const i = Math.min(VIR.length - 2, Math.floor(v)), t = v - i;
  return VIR[i].map((a, j) => a + (VIR[i + 1][j] - a) * t);
}
const cmapCss = (v) => { const [r, g, b] = cmap(v); return `rgb(${r|0},${g|0},${b|0})`; };

const MAX_FRAMES = 8192;

// Draws a spectrogram on canvas c; returns toXY(t_s, f_hz) -> canvas pixels, for overlays.
function drawSpectrogram(c, x, sr, { nfft = 1024, fmax = 6000, range = 70, peak = false } = {}) {
  nfft = 1 << Math.round(Math.log2(Math.max(128, nfft)));
  // One column per frame must fit a canvas (browsers cap width at ~32k px, Firefox returns no context past it)
  // and memory: long recordings are overviewed with a coarser hop rather than all 4x-overlapped frames.
  const hop = Math.max(nfft / 4, Math.ceil((x.length - nfft) / (MAX_FRAMES - 1)));
  fmax = Math.min(fmax || sr / 2, sr / 2);
  const { S, nf, nb } = stft(x, nfft, hop);
  const kmax = Math.min(nb - 1, Math.ceil((fmax * nfft) / sr));

  let top = -Infinity;
  for (let f = 0; f < nf; f++) for (let k = 1; k <= kmax; k++) top = Math.max(top, S[f * nb + k]);

  const img = new ImageData(nf, kmax + 1);
  for (let f = 0; f < nf; f++)
    for (let k = 0; k <= kmax; k++) {
      const [r, g, b] = cmap((S[f * nb + k] - (top - range)) / range);
      const p = ((kmax - k) * nf + f) * 4;
      img.data[p] = r; img.data[p + 1] = g; img.data[p + 2] = b; img.data[p + 3] = 255;
    }
  const off = document.createElement("canvas");
  off.width = nf; off.height = kmax + 1;
  off.getContext("2d").putImageData(img, 0, 0);

  const dpr = devicePixelRatio;
  c.width = c.clientWidth * dpr;
  const g = c.getContext("2d");
  const L = 44 * dpr, B = 20 * dpr, W = c.width - L - 4 * dpr, H = c.height - B;
  g.fillStyle = "#000"; g.fillRect(0, 0, c.width, c.height);
  g.imageSmoothingEnabled = false;
  g.drawImage(off, L, 0, W, H);

  if (peak) { // argmax per frame, only where > top - range/2
    g.fillStyle = "#f33";
    for (let f = 0; f < nf; f++) {
      let kb = 1;
      for (let k = 2; k <= kmax; k++) if (S[f * nb + k] > S[f * nb + kb]) kb = k;
      if (S[f * nb + kb] < top - range / 2) continue;
      g.fillRect(L + (f / nf) * W, H * (1 - kb / kmax) - dpr, 2 * dpr, 2 * dpr);
    }
  }

  const ftop = (kmax * sr) / nfft, dur = x.length / sr;
  g.fillStyle = "#aaa"; g.font = `${11 * dpr}px system-ui`;
  g.textAlign = "right"; g.textBaseline = "middle";
  const fstep = fmax > 8000 ? 2000 : 1000;
  for (let fq = 0; fq <= fmax; fq += fstep) {
    const y = H * (1 - fq / ftop);
    g.fillText(fq >= 1000 ? fq / 1000 + "k" : fq, L - 4 * dpr, Math.min(H - 6 * dpr, Math.max(6 * dpr, y)));
  }
  const tstep = dur > 10 ? 2 : dur > 4 ? 1 : 0.5;
  g.textAlign = "center"; g.textBaseline = "top";
  for (let t = 0; t <= dur; t += tstep) g.fillText(t + "s", L + (t / dur) * W, H + 4 * dpr);
  return (t, f) => [L + (t / dur) * W, H * (1 - f / ftop)];
}

// ---------- WAV ----------
function encodeWav(x, sr) {
  const buf = new ArrayBuffer(44 + x.length * 2), v = new DataView(buf);
  const s = (o, str) => [...str].forEach((ch, i) => v.setUint8(o + i, ch.charCodeAt(0)));
  s(0, "RIFF"); v.setUint32(4, 36 + x.length * 2, true); s(8, "WAVE");
  s(12, "fmt "); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, sr, true); v.setUint32(28, sr * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  s(36, "data"); v.setUint32(40, x.length * 2, true);
  for (let i = 0; i < x.length; i++) v.setInt16(44 + 2 * i, Math.max(-1, Math.min(1, x[i])) * 32767, true);
  return new Blob([buf], { type: "audio/wav" });
}

function decodeWav(ab) {
  const v = new DataView(ab);
  let sr, bits, ch, fmt, o = 12;
  while (o < v.byteLength - 8) {
    const id = String.fromCharCode(...new Uint8Array(ab, o, 4)), len = v.getUint32(o + 4, true);
    if (id === "fmt ") {
      fmt = v.getUint16(o + 8, true); ch = v.getUint16(o + 10, true);
      sr = v.getUint32(o + 12, true); bits = v.getUint16(o + 22, true);
    } else if (id === "data") {
      const n = Math.floor(len / (bits / 8) / ch), x = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const p = o + 8 + i * ch * (bits / 8);
        x[i] = fmt === 3 ? v.getFloat32(p, true) : v.getInt16(p, true) / 32768;
      }
      return { samples: x, sr };
    }
    o += 8 + len + (len & 1);
  }
  throw new Error("bad WAV");
}

function setPlayer(p, x, sr) {
  if (p.src) URL.revokeObjectURL(p.src);
  p.src = URL.createObjectURL(encodeWav(x, sr));
}

async function uploadRecording({ samples, sr, settings }, dataset, label, notes = "") {
  const fd = new FormData();
  fd.append("audio", encodeWav(samples, sr), "rec.wav");
  fd.append("dataset", dataset || "default");
  fd.append("label", label);
  fd.append("notes", notes);
  fd.append("client", JSON.stringify({ userAgent: navigator.userAgent, track: settings }));
  const r = await fetch("api/recordings", { method: "POST", body: fd });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function persistInputs(ids, prefix, onChange) {
  for (const id of ids) {
    try { const v = localStorage.getItem(prefix + id); if (v !== null) $(id).value = v; } catch {}
    $(id).addEventListener("change", () => {
      try { localStorage.setItem(prefix + id, $(id).value); } catch {}
      onChange?.(id);
    });
  }
}
