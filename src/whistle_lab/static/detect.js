"use strict";
// Detector page: record, send to /api/detect, show verdict, tracks and compute metrics.

const SPEC = { nfft: 1024, fmax: 4500, range: 70 };
const COLORS = ["#fff", "#f55", "#5cf", "#fc5", "#c7f"];
const status = (s) => ($("status").textContent = s);
let mic = null, clip = null;

persistInputs(["tplds", "saveds"], "wl.det.", (id) => id === "tplds" && loadTemplates());

const loadTemplates = () => showPatterns($("templates"), $("tplds").value.trim());

$("rec").onclick = () => (mic ? stop() : start());

async function start() {
  try { mic = await startMic(); } catch (e) { status(micErrorText(e)); return; }
  $("rec").textContent = "■ Stop and detect";
  $("rec").classList.add("on");
  $("save").disabled = true;
  liveInit($("spec"));
  requestAnimationFrame(live);
}

function live() {
  if (!mic) return;
  liveColumn($("spec"), mic, SPEC.fmax, SPEC.range);
  status(`listening… ${((performance.now() - mic.t0) / 1000).toFixed(1)} s`);
  requestAnimationFrame(live);
}

async function stop() {
  const m = mic;
  mic = null;
  clip = await m.finish();
  $("rec").textContent = "● Listen";
  $("rec").classList.remove("on");
  setPlayer($("player"), clip.samples, clip.sr);
  const toXY = drawSpectrogram($("spec"), clip.samples, clip.sr, SPEC);
  status("detecting…");

  const fd = new FormData();
  fd.append("audio", encodeWav(clip.samples, clip.sr), "clip.wav");
  fd.append("dataset", $("tplds").value.trim() || "default");
  const t0 = performance.now();
  const r = await fetch("api/detect", { method: "POST", body: fd });
  const roundTrip = (performance.now() - t0) / 1000;
  if (!r.ok) { status("Detect failed: " + (await r.text())); return; }
  const res = await r.json();
  status("");
  showVerdict(res);
  drawTracks(toXY, res);
  showCandidates(res);
  showMetrics(res.metrics, roundTrip, clip);
  $("savelabel").value = res.label || "";
  $("save").disabled = false;
}

function showVerdict(res) {
  const v = $("verdict"), best = res.candidates[0];
  v.className = res.detected ? "yes" : "no";
  const sub = !best || best.rho === null ? "no completed track"
    : best.veto ? `${best.label} vetoed: ${best.veto} (ρ̂ = ${best.rho.toFixed(0)})`
    : `best: ${best.label}, ρ̂ = ${best.rho.toFixed(0)} (threshold ${res.threshold})`;
  v.innerHTML = (res.detected ? `✓ ${res.label}` : "No signal") + `<small>${sub}</small>`;
}

function drawTracks(toXY, res) {
  const g = $("spec").getContext("2d"), dpr = devicePixelRatio;
  res.candidates.forEach((c, i) => {
    if (c.rho === null || !c.track.length) return;
    g.strokeStyle = COLORS[i % COLORS.length];
    g.lineWidth = (i === 0 ? 2 : 1.5) * dpr;
    g.setLineDash(i === 0 ? [] : [4 * dpr, 4 * dpr]);
    g.beginPath();
    c.track.forEach(([t, f], j) => { const [x, y] = toXY(t, f); j ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  });
  g.setLineDash([]);
}

function showCandidates(res) {
  const t = $("cands");
  t.innerHTML = "<tr><th></th><th>pattern</th><th>ρ̂ vs threshold</th><th class=n>ρ̂</th><th class=n>χ²</th><th class=n>steady</th><th class=n>off-path</th><th class=n>step err</th><th class=n>err/step</th><th>notes (Hz)</th><th class=n>span</th><th>veto</th></tr>";
  const top = Math.max(res.threshold * 1.5, ...res.candidates.map((c) => c.rho ?? 0));
  res.candidates.forEach((c, i) => {
    const tr = t.insertRow();
    tr.insertCell().innerHTML = `<span style="color:${COLORS[i % COLORS.length]}">━</span>`;
    tr.insertCell().textContent = c.label;
    const w = Math.max(0, (c.rho ?? 0) / top) * 100, th = (res.threshold / top) * 100;
    tr.insertCell().innerHTML = `<div class="bar"><i class="${(c.rho ?? -1) >= res.threshold && !c.veto ? "pass" : ""}" style="width:${w}%"></i><b style="left:${th}%"></b></div>`;
    const num = (v, d) => { const td = tr.insertCell(); td.className = "n"; td.textContent = v === null ? "—" : v.toFixed(d); };
    num(c.rho, 0);
    num(c.chi2, 2);
    num(c.steadiness, 2);
    num(c.off_path, 2);
    num(c.step_error, 1);
    num(c.step_ratio, 2);
    tr.insertCell().textContent = c.notes_hz.map((f) => (f === null ? "—" : f.toFixed(0))).join(" → ");
    const sp = tr.insertCell(); sp.className = "n";
    sp.textContent = c.rho === null ? "—" : `${c.t_start.toFixed(2)}–${c.t_end.toFixed(2)} s`;
    tr.insertCell().textContent = c.veto ?? "";
  });
}

function showMetrics(m, roundTrip, clip) {
  const ms = (s) => (s * 1000).toFixed(1) + " ms";
  const rows = [
    ["audio", `${m.audio_s.toFixed(2)} s @ ${clip.sr} Hz → ${m.frames} frames (16 ms hop)`],
    ["server compute", `${ms(m.compute_s)}  (real-time factor ${m.real_time_factor.toFixed(3)})`],
    ...Object.entries(m.timings_s).map(([k, v]) => ["  " + k, ms(v)]),
    ["round trip (upload + compute)", ms(roundTrip)],
    ["HMM size", `${m.hmm_states.toLocaleString()} states, ${m.hmm_transitions_per_frame.toLocaleString()} transitions/frame`],
    ["est. ops / frame", `${Math.round(m.est_ops_per_frame).toLocaleString()} (FFT + scoring + max-plus)`],
    ["est. sustained load", `${(m.est_ops_per_s / 1e6).toFixed(1)} M ops/s ≈ ${(100 * m.est_esp32_core_fraction).toFixed(1)} % of one 240 MHz core at 1 op/cycle`],
  ];
  const t = $("metrics");
  t.innerHTML = "";
  for (const [k, v] of rows) {
    const tr = t.insertRow();
    tr.insertCell().textContent = k;
    const td = tr.insertCell(); td.className = "n"; td.textContent = v;
  }
}

$("save").onclick = async () => {
  if (!clip) return;
  $("save").disabled = true;
  try {
    const meta = await uploadRecording(clip, $("saveds").value.trim() || "detect-tests", $("savelabel").value,
                                       "detector page: " + $("verdict").textContent);
    status(`Saved ${meta.dataset}/${meta.id} [${meta.label || "unlabelled"}]`);
  } catch (e) { status("Save failed: " + e.message); $("save").disabled = false; }
};

liveInit($("spec"));
loadTemplates();
