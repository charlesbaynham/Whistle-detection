"use strict";
// Recorder page. Shared helpers live in common.js.

persistInputs(["dataset", "label", "fmax", "range", "nfft"], "wl.", (id) => {
  if (id === "dataset") refreshList();
  else redraw();
});
$("peak").addEventListener("change", redraw);

const status = (s) => ($("status").textContent = s);
const specOpts = () => ({ nfft: +$("nfft").value || 1024, fmax: +$("fmax").value, range: +$("range").value || 70,
                          peak: $("peak").checked });
function redraw() { if (current) drawSpectrogram($("spec"), current.samples, current.sr, specOpts()); }

// ---------- recording ----------
let mic = null;      // live recording
let current = null;  // {samples, sr, settings, unsaved, dataset?, id?}

$("rec").onclick = () => (mic ? stopRec() : startRec());

async function startRec() {
  try { mic = await startMic(); } catch (e) { status(micErrorText(e)); return; }
  $("rec").textContent = "■ Stop";
  $("rec").classList.add("on");
  setSaveEnabled(false);
  liveInit($("spec"));
  requestAnimationFrame(liveDraw);
}

function liveDraw() {
  if (!mic) return;
  liveColumn($("spec"), mic, +$("fmax").value, +$("range").value);
  status(`recording… ${((performance.now() - mic.t0) / 1000).toFixed(1)} s`);
  requestAnimationFrame(liveDraw);
}

async function stopRec() {
  const m = mic;
  mic = null;
  const rec = await m.finish();
  $("rec").textContent = "● Record";
  $("rec").classList.remove("on");
  current = { ...rec, unsaved: true };
  let pk = 0;
  for (const x of rec.samples) pk = Math.max(pk, Math.abs(x));
  status(`${(rec.samples.length / rec.sr).toFixed(2)} s @ ${rec.sr} Hz, peak ${(20 * Math.log10(pk + 1e-12)).toFixed(1)} dBFS`);
  setPlayer($("player"), rec.samples, rec.sr);
  redraw();
  setSaveEnabled(true);
  highlight(null);
}

function setSaveEnabled(on) { $("save").disabled = $("discard").disabled = !on; }

// ---------- save / discard ----------
$("save").onclick = async () => {
  if (!current?.unsaved) return;
  setSaveEnabled(false);
  let meta;
  try {
    meta = await uploadRecording(current, $("dataset").value.trim(), $("label").value, $("notes").value);
  } catch (e) { status("Save failed: " + e.message); setSaveEnabled(true); return; }
  Object.assign(current, { unsaved: false, dataset: meta.dataset, id: meta.id });
  $("notes").value = "";
  status(`Saved ${meta.dataset}/${meta.id} [${meta.label || "unlabelled"}]`);
  await refreshList();
  highlight(meta.id);
};

$("discard").onclick = () => {
  current = null;
  setSaveEnabled(false);
  liveInit($("spec"));
  status("Discarded");
};

// ---------- recordings list ----------
async function refreshList() {
  const ds = $("dataset").value.trim();
  const r = await fetch("api/recordings" + (ds ? "?dataset=" + encodeURIComponent(ds) : ""));
  const recs = r.ok ? await r.json() : [];
  $("count").textContent = `(${recs.length} in ${ds || "all"})`;
  const t = $("list");
  t.innerHTML = "";
  for (const m of recs) {
    const tr = t.insertRow();
    tr.dataset.id = m.id;
    tr.insertCell().textContent = m.label || "—";
    tr.insertCell().textContent = m.duration_s.toFixed(1) + " s";
    tr.insertCell().textContent = new Date(m.created).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
    const x = tr.insertCell(); x.className = "x";
    const del = document.createElement("button");
    del.textContent = "✕";
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete ${m.label || m.id}?`)) return;
      await fetch(`api/recordings/${m.dataset}/${m.id}`, { method: "DELETE" });
      refreshList();
    };
    x.append(del);
    tr.onclick = () => load(m);
  }
  if (current?.id) highlight(current.id);
}

async function load(m) {
  if (current?.unsaved && !confirm("Discard unsaved recording?")) return;
  const r = await fetch(`api/recordings/${m.dataset}/${m.id}.wav`);
  const { samples, sr } = decodeWav(await r.arrayBuffer());
  current = { samples, sr, unsaved: false, dataset: m.dataset, id: m.id };
  setSaveEnabled(false);
  setPlayer($("player"), samples, sr);
  redraw();
  status(`${m.dataset}/${m.id} [${m.label || "unlabelled"}] ${m.duration_s.toFixed(2)} s @ ${sr} Hz`);
  highlight(m.id);
}

function highlight(id) {
  for (const tr of $("list").rows) tr.classList.toggle("sel", tr.dataset.id === id);
}

liveInit($("spec"));
refreshList();
