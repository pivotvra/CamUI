"use strict";
// drop-rpi NDVI + Calibration tabs for CamUI
const Drop = (() => {
  // ------------------------------------------------------------------ helpers
  async function api(url, data) {
    const opt = data === undefined ? {} :
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) };
    let r, j;
    try {
      r = await fetch(url, opt);
      j = await r.json();
    } catch (e) {
      return { ok: false, message: r ? `${r.status} ${r.statusText}` : String(e) };
    }
    if (!r.ok && j.ok === undefined) j.ok = false;
    return j;
  }

  function toast(msg, kind = "secondary") {
    const box = document.getElementById("dropToasts");
    if (!box) return alert(msg);
    const el = document.createElement("div");
    el.className = `alert alert-${kind} py-2 px-3 mb-2 shadow-sm small`;
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => el.remove(), kind === "danger" ? 9000 : 4000);
  }

  function report(res, okMsg) {
    if (!res || res.ok === false) toast((res && res.message) || "Error", "danger");
    else if (okMsg) toast(typeof okMsg === "function" ? okMsg(res) : okMsg, "success");
    return res;
  }

  const isNum = (v) => typeof v === "number" && Number.isFinite(v);
  // refresh an input unless the user is editing it
  const setVal = (el, v) => { if (el && el !== document.activeElement) el.value = v; };
  // profile/session names: letters, digits, '_', '.', '-' (spaces become '_')
  const slug = (v) => String(v || "").trim().replace(/\s+/g, "_").replace(/[^A-Za-z0-9_.-]/g, "").slice(0, 64);
  const fmt = (v, d = 2) => (v === null || v === undefined) ? "–" :
    (typeof v === "number" ? (Number.isFinite(v) ? v.toFixed(d) : "–") : String(v));
  const pct = (v) => isNum(v) ? (100 * v).toFixed(v < 0.1 ? 1 : 0) + "%" : "–";
  const get = (obj, path) => path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  const $ = (id) => document.getElementById(id);

  function poll(fn, ms) {
    let stopped = false;
    const loop = async () => {
      if (stopped) return;
      if (!document.hidden) {
        try { await fn(); } catch (e) { console.warn(e); }
      }
      setTimeout(loop, ms);
    };
    loop();
    return () => { stopped = true; };
  }

  function busy(btn, on) {
    if (!btn) return;
    btn.disabled = on;
    if (on) {
      btn.dataset.html = btn.innerHTML;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> ' + btn.textContent.trim();
    } else if (btn.dataset.html) {
      btn.innerHTML = btn.dataset.html;
    }
  }

  // ------------------------------------------------------------------ views
  function reconnect(img) {
    setTimeout(() => {
      const u = new URL(img.src, location.href);
      u.searchParams.set("t", Date.now());
      img.src = u.toString();
    }, 2000);
  }

  function initViews() {
    document.querySelectorAll(".drop-view").forEach((view) => {
      view.addEventListener("click", (e) => {
        if (e.target.closest(".drop-noclick")) return;
        if (document.fullscreenElement || document.webkitFullscreenElement) {
          (document.exitFullscreen || document.webkitExitFullscreen).call(document);
        } else {
          (view.requestFullscreen || view.webkitRequestFullscreen).call(view);
        }
      });
      const img = view.querySelector("img");
      img.addEventListener("error", () => reconnect(img));
    });
    const sel = $("procViewSelect");
    if (sel) {
      sel.addEventListener("click", (e) => e.stopPropagation());
      sel.addEventListener("change", () => setProcView(sel.value));
    }
  }

  function setProcView(view) {
    const img = $("procView");
    img.src = `/drop/stream/${view}?model=${img.dataset.model}&t=${Date.now()}`;
    const sel = $("procViewSelect");
    if (sel) sel.value = view;
  }

  function contentRect(img, box) {
    const ir = img.getBoundingClientRect(), br = box.getBoundingClientRect();
    const nw = img.naturalWidth || ir.width, nh = img.naturalHeight || ir.height;
    const s = Math.min(ir.width / nw, ir.height / nh);
    const w = nw * s, h = nh * s;
    return { x: ir.left - br.left + (ir.width - w) / 2, y: ir.top - br.top + (ir.height - h) / 2, w, h };
  }

  // shapes: {type: "poly"|"points", pts: [[x,y],...] normalised, color, fill, width}
  function drawOverlay(role, shapes) {
    const view = document.querySelector(`.drop-view[data-role="${role}"]`);
    if (!view) return;
    const cv = view.querySelector("canvas"), img = view.querySelector("img");
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.round(view.clientWidth * dpr);
    cv.height = Math.round(view.clientHeight * dpr);
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, view.clientWidth, view.clientHeight);
    const r = contentRect(img, view);
    const P = (p) => [r.x + p[0] * r.w, r.y + p[1] * r.h];
    for (const s of shapes) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 1.5;
      if (s.type === "poly") {
        ctx.beginPath();
        s.pts.forEach((p, i) => { const [x, y] = P(p); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
        ctx.closePath();
        if (s.fill) { ctx.fillStyle = s.fill; ctx.fill(); }
        ctx.stroke();
      } else if (s.type === "points") {
        ctx.fillStyle = s.color;
        for (const p of s.pts) {
          const [x, y] = P(p);
          ctx.beginPath(); ctx.arc(x, y, s.radius || 2, 0, 2 * Math.PI); ctx.fill();
        }
      }
    }
  }

  // ------------------------------------------------------------------ camera panel
  function cameraPanel() {
    const root = $("cameraPanel");
    if (!root) return null;
    let info = null;
    const role = () => root.querySelector("input[name=camRole]:checked").value;
    const cur = () => (info ? (role() === "red" ? info.red : info.ir) : null);
    const range = (key, fallback) => (cur() && cur().ranges && cur().ranges[key]) || fallback;
    const expMin = () => Math.max(1, range("ExposureTime", [100])[0] || 100);
    const expMax = () => Math.min(1e6, range("ExposureTime", [0, 1e6])[1] || 1e6);
    const toSlider = (v) => 1000 * Math.log(v / expMin()) / Math.log(expMax() / expMin());
    const fromSlider = (s) => Math.round(expMin() * Math.pow(expMax() / expMin(), s / 1000));

    async function load() {
      const r = await api("/drop/api/cameras");
      if (!r.ok) return report(r);
      info = r;
      if (!r.ir || !r.red) toast(`IR or RED camera missing (available: ${r.available.join(", ") || "none"})`, "danger");
      fill();
    }

    function fill() {
      const c = cur();
      if (!c) return;
      const v = c.values || {};
      $("ctlAeEnable").checked = !!(v.AeEnable ?? 1);
      if (isNum(v.ExposureTime)) {
        setVal($("ctlExposureTime"), v.ExposureTime);
        setVal($("ctlExposureSlider"), toSlider(v.ExposureTime));
      }
      const g = range("AnalogueGain", [1, 16]);
      $("ctlGainSlider").min = g[0] || 1;
      $("ctlGainSlider").max = Math.min(g[1] || 16, 16);
      if (isNum(v.AnalogueGain)) { setVal($("ctlAnalogueGain"), v.AnalogueGain); setVal($("ctlGainSlider"), v.AnalogueGain); }
      if (isNum(v.ExposureValue)) { setVal($("ctlExposureValue"), v.ExposureValue); setVal($("ctlEvSlider"), v.ExposureValue); }
      $("ctlAwbEnable").checked = !!(v.AwbEnable ?? 1);
      if (Array.isArray(v.ColourGains)) { setVal($("ctlGainR"), v.ColourGains[0]); setVal($("ctlGainB"), v.ColourGains[1]); }
      const sel = $("ctlSensorMode");
      sel.innerHTML = "";
      for (const m of c.sensor_modes) {
        const o = document.createElement("option");
        o.value = m.index;
        o.textContent = `${m.index}: ${m.size[0]}×${m.size[1]} · ${m.fps} fps · ${m.bit_depth}-bit`;
        sel.appendChild(o);
      }
      if (c.sensor_mode !== null && c.sensor_mode !== undefined) sel.value = c.sensor_mode;
    }

    async function send(id, value, note) {
      const r = await api("/drop/api/camera/control", { role: role(), id, value });
      report(r, note);
      await load();
    }

    root.querySelectorAll("input[name=camRole]").forEach((el) => el.addEventListener("change", fill));
    $("ctlAeEnable").addEventListener("change", (e) => send("AeEnable", e.target.checked ? 1 : 0));
    $("ctlAwbEnable").addEventListener("change", (e) => send("AwbEnable", e.target.checked ? 1 : 0));
    $("ctlExposureSlider").addEventListener("input", (e) => { $("ctlExposureTime").value = fromSlider(+e.target.value); });
    $("ctlExposureSlider").addEventListener("change", (e) => send("ExposureTime", fromSlider(+e.target.value)));
    $("ctlExposureTime").addEventListener("change", (e) => send("ExposureTime", +e.target.value));
    $("ctlGainSlider").addEventListener("input", (e) => { $("ctlAnalogueGain").value = e.target.value; });
    $("ctlGainSlider").addEventListener("change", (e) => send("AnalogueGain", +e.target.value));
    $("ctlAnalogueGain").addEventListener("change", (e) => send("AnalogueGain", +e.target.value));
    $("ctlEvSlider").addEventListener("input", (e) => { $("ctlExposureValue").value = e.target.value; });
    $("ctlEvSlider").addEventListener("change", (e) => send("ExposureValue", +e.target.value));
    $("ctlExposureValue").addEventListener("change", (e) => send("ExposureValue", +e.target.value));
    $("ctlGainsApply").addEventListener("click", () => send("ColourGains", [+$("ctlGainR").value, +$("ctlGainB").value]));
    $("ctlSensorMode").addEventListener("change", async (e) => {
      toast("Switching sensor mode…");
      await send("sensor_mode", +e.target.value, "Sensor mode changed");
    });
    $("btnLockExposure").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      busy(btn, true);
      const r = await api("/drop/api/camera/lock_exposure", { role: role() });
      busy(btn, false);
      report(r, "Exposure locked");
      load();
    });
    $("btnCopyExposure").addEventListener("click", async () => {
      const r = await api("/drop/api/camera/copy_exposure", {});
      report(r, (x) => `RED set to ${x.values.ExposureTime} µs, gain ${fmt(x.values.AnalogueGain)}`);
      load();
    });
    $("btnSaveCamProfile").addEventListener("click", async () => {
      const r = await api("/drop/api/camera/save_profile", { role: role(), name: slug($("camProfileName").value) });
      report(r, "Saved as CamUI start-up profile");
    });

    poll(async () => {
      if (root.offsetParent === null) return;
      const r = await api("/drop/api/camera/metadata");
      if (!r.ok) return;
      root.querySelectorAll("[data-meta]").forEach((td) => {
        const key = td.dataset.meta;
        let v;
        if (key.endsWith(".stream")) {
          const s = info && info[key.split(".")[0]] && info[key.split(".")[0]].stream;
          v = s ? `${s.size[0]}×${s.size[1]} ${s.format}` : "–";
        } else if (key === "sync_ms") {
          v = isNum(r.sync_ms) ? `${fmt(r.sync_ms, 1)} ms between IR and RED` : "–";
        } else {
          const x = get(r, key);
          if (Array.isArray(x)) v = x.map((y) => (isNum(y) && !Number.isInteger(y) ? y.toFixed(2) : y)).join(", ");
          else if (key.endsWith("ExposureTime")) v = isNum(x) ? `${x} µs` : "–";
          else v = fmt(x, key.endsWith("Lux") ? 0 : 2);
        }
        td.textContent = v;
      });
    }, 1500);

    load();
    return { load };
  }

  // ------------------------------------------------------------------ tuning panel
  function tuningPanel(root) {
    if (!root) return { load() {}, setResidual() {} };
    const model = root.dataset.model;
    const q = (sel) => root.querySelector(sel);
    let state = null;

    function fill() {
      q("#tunDescribe").textContent = state.describe;
      const stereo = state.kind === "stereo";
      q("#tunPlaneRow").classList.toggle("d-none", !stereo);
      if (stereo && state.plane) {
        setVal(q("#tunDistance"), state.plane.distance_m);
        setVal(q("#tunPitch"), state.plane.pitch_deg ?? "");
      }
      setVal(q("#tunOffX"), fmt(state.offset_px[0], 1));
      setVal(q("#tunOffY"), fmt(state.offset_px[1], 1));
      const save = q("#tunSave");
      if (save) {
        const dirty = state.saved && JSON.stringify([state.saved.plane, state.saved.offset_px]) !==
          JSON.stringify([state.plane, state.offset_px]);
        save.classList.toggle("btn-warning", !!dirty);
        save.classList.toggle("btn-success", !dirty);
        save.disabled = !state.name;
        save.title = dirty ? "Unsaved changes" : "";
      }
    }

    async function load() {
      const r = await api(`/drop/api/model?model=${model}`);
      if (r.ok) { state = r.model; fill(); }
    }

    async function send(payload) {
      const r = await api(`/drop/api/tuning?model=${model}`, payload);
      report(r);
      if (r.ok) { state = r.model; fill(); }
    }

    q("#tunDistance").addEventListener("change", (e) => send({ distance_m: +e.target.value }));
    q("#tunPitch").addEventListener("change", (e) => send({ pitch_deg: e.target.value === "" ? "" : +e.target.value }));
    const sendOffset = () => send({ offset_px: [+q("#tunOffX").value, +q("#tunOffY").value] });
    q("#tunOffX").addEventListener("change", sendOffset);
    q("#tunOffY").addEventListener("change", sendOffset);
    root.querySelectorAll("[data-nudge]").forEach((b) => b.addEventListener("click", () => {
      const [dx, dy] = b.dataset.nudge.split(",").map(Number);
      q("#tunOffX").value = (+q("#tunOffX").value + dx).toFixed(1);
      q("#tunOffY").value = (+q("#tunOffY").value + dy).toFixed(1);
      sendOffset();
    }));
    q("#tunAuto").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      busy(btn, true);
      const r = await api(`/drop/api/tuning/auto_offset?model=${model}`, {});
      busy(btn, false);
      report(r, (x) => `Measured shift ${fmt(x.result.measured[0], 1)}, ${fmt(x.result.measured[1], 1)} px (${x.result.samples} frames) — offset updated`);
      if (r.ok) { state = r.model; fill(); }
    });
    if (q("#tunSave")) q("#tunSave").addEventListener("click", async () => {
      const r = await api("/drop/api/tuning/save", {});
      report(r, "Tuning saved to the profile");
      if (r.ok) { state = r.model; fill(); }
    });
    if (q("#tunRevert")) q("#tunRevert").addEventListener("click", async () => {
      const r = await api("/drop/api/tuning/revert", {});
      report(r, "Reverted to the saved profile");
      if (r.ok) { state = r.model; fill(); }
    });

    function setResidual(a) {
      const el = q("#tunResidual");
      if (!a || !a.tiles || !isNum(a.dx)) { el.textContent = "not measurable (not enough texture)"; return; }
      el.textContent = `dx ${fmt(a.dx, 1)}, dy ${fmt(a.dy, 1)} px · confidence ${fmt(a.confidence, 2)} · ${a.tiles} tiles`;
      el.className = Math.hypot(a.dx, a.dy) > 3 && a.confidence > 0.2 ? "text-warning-emphasis fw-semibold" : "";
    }
    return { load, setResidual };
  }

  // ------------------------------------------------------------------ NDVI page
  function ndviPage() {
    initViews();
    recordedPanel();
    cameraPanel();
    const tun = tuningPanel($("tuningPanel"));
    tun.load();

    let profiles = [];
    async function loadProfiles() {
      const r = await api("/drop/api/profiles");
      if (!r.ok) return report(r);
      profiles = r.profiles;
      const sel = $("profileSelect");
      sel.innerHTML = '<option value="">(none — RED only rescaled)</option>';
      for (const p of profiles) {
        const o = document.createElement("option");
        o.value = p.name;
        o.textContent = `${p.name} · ${p.type}` + (isNum(p.rms) ? ` · RMS ${fmt(p.rms, 2)}` : "") +
          (p.quality && p.quality.level ? ` · ${p.quality.level}` : "");
        sel.appendChild(o);
      }
      sel.value = r.active || "";
      showProfileInfo();
    }
    function showProfileInfo() {
      const p = profiles.find((x) => x.name === $("profileSelect").value);
      $("profileInfo").textContent = p ? `${p.notes || ""} ${p.updated ? "(updated " + p.updated + ")" : ""}` :
        "Without a profile the RED image is only rescaled to the IR crop.";
    }
    $("profileSelect").addEventListener("change", showProfileInfo);
    $("profileLoad").addEventListener("click", async () => {
      const r = await api("/drop/api/ndvi/profile", { name: $("profileSelect").value || null });
      report(r, "Calibration loaded");
      tun.load();
    });
    loadProfiles();

    const setRoot = $("ndviSettings");
    function fillSettings(s) {
      setRoot.querySelectorAll("[data-set]").forEach((el) => {
        if (el === document.activeElement && el.tagName === "INPUT" && el.type !== "checkbox") return;  // user is typing
        const v = s[el.dataset.set];
        if (el.type === "checkbox") el.checked = !!v;
        else el.value = el.dataset.scale ? +(v * el.dataset.scale).toFixed(2) : v;
      });
      setRoot.querySelector('[data-set="eto"]').disabled = !!s.eto_from_weather;
    }
    async function loadSettings() {
      const r = await api("/drop/api/ndvi/settings");
      if (r.ok) fillSettings(r.settings);
    }
    setRoot.querySelectorAll("[data-set]").forEach((el) => el.addEventListener("change", async () => {
      let v = el.type === "checkbox" ? el.checked : (el.tagName === "SELECT" ? el.value : +el.value);
      if (el.dataset.scale) v = v / +el.dataset.scale;
      const r = await api("/drop/api/ndvi/settings", { [el.dataset.set]: v });
      report(r);
      if (r.ok) fillSettings(r.settings);
    }));
    $("btnPanelGain").addEventListener("click", async () => {
      const r = await api("/drop/api/ndvi/red_gain_from_panel", {});
      report(r, (x) => `RED gain set to ${fmt(x.red_gain, 3)}`);
      loadSettings();
    });
    $("btnSnapshot").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      busy(btn, true);
      const r = await api("/drop/api/ndvi/snapshot", {});
      busy(btn, false);
      report(r, (x) => `Saved ${x.path}_ir.png / _red.png`);
    });
    loadSettings();
    imageMetaPanel();
    weatherPanel(loadSettings);

    poll(async () => {
      const r = await api("/drop/api/ndvi/stats");
      if (!r.ok) return;
      $("statusLine").textContent = r.status || "";
      const s = r.stats;
      if (!s) {
        for (const id of ["meanNdvi", "vegFrac", "validFrac", "maskPx"]) $(id).textContent = "–";
        $("zoneRows").replaceChildren();
        document.querySelectorAll("[data-q]").forEach((el) => { el.textContent = "–"; });
        $("qualityWarnings").replaceChildren();
        return;
      }
      $("meanNdvi").textContent = fmt(s.mean_ndvi, 3);
      $("vegFrac").textContent = pct(s.veg_fraction);
      $("validFrac").textContent = pct(s.valid_fraction);
      $("maskPx").textContent = `${s.mask_rows_px[0]}–${s.mask_rows_px[1]} of ${s.mask_rows_px[2]}`;
      $("maskPxConfig").textContent = `MASK_ROW_START = ${s.mask_rows_px[0]}, MASK_ROW_END = ${s.mask_rows_px[1]} (at ${s.mask_rows_px[2]} px height)`;
      $("zoneRows").innerHTML = s.zones.map((z) => `<tr><td>${z.zone}</td><td><b>${fmt(z.ndvi, 3)}</b></td>` +
        `<td>${pct(z.veg_fraction)}</td><td>${pct(z.valid_fraction)}</td><td>${fmt(z.fc, 2)}</td><td>${fmt(z.kc, 2)}</td>` +
        `<td>${fmt(z.etc, 2)}</td><td>${fmt(z.irg, 2)}</td></tr>`).join("");
      const Q = s.quality;
      const cell = (k) => document.querySelector(`[data-q="${k}"]`);
      for (const b of ["ir", "red"]) {
        cell(`${b}.mean`).textContent = fmt(Q[b].mean, 2);
        cell(`${b}.saturated`).textContent = pct(Q[b].saturated);
        cell(`${b}.sharpness`).textContent = fmt(Q[b].sharpness, 2);
      }
      const a = Q.alignment;
      cell("alignment").textContent = a && a.tiles ? `${fmt(a.shift, 1)} px (dx ${fmt(a.dx, 1)}, dy ${fmt(a.dy, 1)}; conf. ${fmt(a.confidence, 2)})` : "not measurable";
      cell("sync").textContent = isNum(Q.sync_ms) ? `${fmt(Q.sync_ms, 1)} ms` : "–";
      tun.setResidual(a);

      const warn = [];
      if (s.model.startsWith("no calibration")) warn.push("No calibration profile loaded — the bands are not aligned.");
      for (const b of ["ir", "red"]) {
        if (Q[b].saturated > 0.01) warn.push(`${b.toUpperCase()}: ${pct(Q[b].saturated)} saturated — lower the exposure.`);
        if (Q[b].mean < 0.08) warn.push(`${b.toUpperCase()} is very dark — raise the exposure.`);
      }
      if (isNum(Q.sync_ms) && Math.abs(Q.sync_ms) > 50) warn.push(`IR and RED frames are ${fmt(Math.abs(Q.sync_ms), 0)} ms apart — moving scenes will misalign.`);
      if (a && a.tiles >= 3 && a.confidence > 0.2 && a.shift > 3) warn.push(`Residual misalignment ${fmt(a.shift, 1)} px — try Auto offset or check the crop distance.`);
      $("qualityWarnings").innerHTML = warn.map((w) => `<li>${w}</li>`).join("");
    }, 1000);
  }

  function recordedPanel() {
    let state = null;
    let pending = false;
    let playing = false;
    let replayTimer = null;
    function stopReplay() {
      playing = false;
      clearTimeout(replayTimer);
      $("replayToggle").textContent = "Play";
    }
    function fill(s) {
      const sourceChanged = !state || state.source !== s.source;
      state = s;
      const recorded = s.mode === "recorded";
      const running = s.job.running || pending;
      const browsingLocked = pending || (s.job.running && s.job.kind !== "evaluate");
      if (!recorded || (s.job.running && s.job.kind !== "evaluate")) stopReplay();
      $("inputMode").value = s.mode;
      if (s.source && sourceChanged) {
        $("datasetSelect").value = s.source;
        $("includeSubdirectories").checked = s.recursive;
        $("recordedCrop").value = s.crop_mode;
      }
      $("recordedControls").classList.toggle("d-none", !recorded);
      $("inputMode").disabled = running;
      $("datasetLoad").disabled = running || !$("datasetSelect").value;
      $("datasetSelect").disabled = running;
      $("datasetRefresh").disabled = running;
      for (const id of ["includeSubdirectories", "recordedCrop", "uploadZip", "uploadFolder"]) $(id).disabled = running;
      $("replayToggle").disabled = browsingLocked || !s.total;
      $("pairPrev").disabled = browsingLocked || s.index <= 0;
      $("pairNext").disabled = browsingLocked || s.index >= s.total - 1;
      $("pairIndex").disabled = browsingLocked;
      $("pairIndex").max = s.total;
      setVal($("pairIndex"), s.index + 1);
      $("pairTotal").textContent = `of ${s.total}`;
      $("datasetEvaluate").disabled = running;
      $("datasetExport").disabled = running || !s.evaluated || !s.good;
      $("datasetCleanup").disabled = running || !s.evaluated || !s.bad;
      $("btnSnapshot").disabled = recorded;
      document.querySelectorAll("#cameraPanel input, #cameraPanel select, #cameraPanel button, [data-set='ir_cam'], [data-set='red_cam']").forEach((el) => {
        el.disabled = recorded;
      });
      document.querySelector("[data-set='proc_width']").disabled = recorded && running;
      $("recordedPair").textContent = s.pair ?
        `${s.pair.session} / ${s.pair.stem} — ${s.verdict || "not evaluated"}${s.reasons ? ": " + s.reasons : ""}` : "";
      $("recordedJob").textContent = s.job.error ? `Failed: ${s.job.error}` :
        s.job.running ? `${s.job.kind}: ${s.job.done} / ${s.job.total} pairs` +
          (s.job.kind === "evaluate" ? ` · folders ${s.job.folders_done}/${s.job.folders_total} · ${s.job.folder || ""} · ${s.good} good, ${s.bad} bad so far. Replay remains available.` : "") :
        s.job.quarantine ? `Cleanup complete: ${s.job.moved} files moved to ${s.job.quarantine}; ${s.job.skipped_pairs} pairs skipped (archived or unavailable). Reload the dataset to refresh its file list.` :
        s.job.export ? `Export complete: ${s.job.export} (${s.good} good pairs). Report: report.csv` :
        s.evaluated ? `${s.good} good, ${s.bad} bad. Evaluated with ${s.evaluation.model.describe}; crop: ${s.crop_mode}.` :
        "Evaluate the dataset before exporting. Changing bands or calibration requires re-evaluation.";
      $("evaluationReasons").textContent = Object.entries(s.reason_counts)
        .sort((a, b) => b[1] - a[1]).map(([reason, count]) => `${reason}: ${count}`).join(" · ");
    }
    async function sources() {
      const r = await api("/drop/api/ndvi/datasets");
      if (!r.ok) return report(r);
      $("datasetRoot").textContent = `FieldData location: ${r.root}` +
        (r.warnings && r.warnings.length ? ` — ${r.warnings.join("; ")}` : "");
      const sel = $("datasetSelect");
      const previous = sel.value;
      sel.replaceChildren();
      for (const source of r.sources) {
        const opt = document.createElement("option");
        opt.value = source.path;
        opt.textContent = source.path === "." ? "FieldData root" : `${source.path} (${source.kind})`;
        sel.appendChild(opt);
      }
      if ([...sel.options].some((o) => o.value === previous)) sel.value = previous;
      if (!r.sources.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "No folders or ZIP archives found in FieldData";
        sel.appendChild(opt);
      }
      $("datasetLoad").disabled = !sel.value;
    }
    async function change(url, data) {
      if (pending) return;
      pending = true;
      if (state) fill(state);
      const r = await api(url, data);
      pending = false;
      report(r);
      if (r.ok) fill(r.input);
      else {
        stopReplay();
        if (state) fill(state);
      }
      return r;
    }
    $("inputMode").addEventListener("change", () => {
      stopReplay();
      if ($("inputMode").value === "live") change("/drop/api/ndvi/input", { mode: "live" });
      else if ($("datasetSelect").value) change("/drop/api/ndvi/input", { mode: "recorded", source: $("datasetSelect").value, reload: true, recursive: $("includeSubdirectories").checked, crop_mode: $("recordedCrop").value });
      else {
        if (state) fill(state);
        toast("Add a folder or ZIP archive under FieldData first", "warning");
      }
    });
    $("datasetRefresh").addEventListener("click", sources);
    $("datasetLoad").addEventListener("click", () => {
      stopReplay();
      change("/drop/api/ndvi/input", {
        mode: "recorded", source: $("datasetSelect").value, reload: true,
        recursive: $("includeSubdirectories").checked,
        crop_mode: $("recordedCrop").value,
      });
    });
    const browse = (index) => state && change("/drop/api/ndvi/input", { mode: "recorded", source: state.source, index });
    $("pairPrev").addEventListener("click", () => { stopReplay(); browse(state.index - 1); });
    $("pairNext").addEventListener("click", () => { stopReplay(); browse(state.index + 1); });
    $("pairIndex").addEventListener("change", () => { stopReplay(); browse(Number($("pairIndex").value) - 1); });
    $("datasetEvaluate").addEventListener("click", () => { stopReplay(); change("/drop/api/ndvi/evaluate", {}); });
    $("datasetExport").addEventListener("click", () => { stopReplay(); change("/drop/api/ndvi/export", {}); });
    $("datasetCleanup").addEventListener("click", () => {
      stopReplay();
      if (confirm(`Move evaluated bad captures (${state.bad} pairs) to recoverable quarantine? Good captures stay untouched. ZIP members are skipped. A restore manifest is saved.`)) {
        change("/drop/api/ndvi/cleanup", { confirm: true });
      }
    });
    $("replayToggle").addEventListener("click", async () => {
      if (playing) return stopReplay();
      playing = true;
      $("replayToggle").textContent = "Pause";
      const step = async () => {
        if (!playing || document.hidden) return stopReplay();
        if (!state || state.mode !== "recorded" || (state.job.running && state.job.kind !== "evaluate")) return stopReplay();
        if (!pending) {
          if (state.index >= state.total - 1) return stopReplay();
          await browse(state.index + 1);
        }
        if (playing) replayTimer = setTimeout(step, 1000);
      };
      if (state.index >= state.total - 1) await browse(0);
      if (playing) replayTimer = setTimeout(step, 1000);
    });
    $("uploadZip").addEventListener("click", () => $("zipFiles").click());
    $("uploadFolder").addEventListener("click", () => $("folderFiles").click());
    async function upload(event) {
      const input = event.target;
      if (!input.files.length || pending) return;
      stopReplay();
      pending = true;
      if (state) fill(state);
      $("uploadStatus").textContent = "Uploading...";
      try {
        const data = new FormData();
        for (const file of input.files) data.append("files", file, file.webkitRelativePath || file.name);
        const response = await fetch("/drop/api/ndvi/upload", { method: "POST", body: data });
        const result = await response.json();
        if (!response.ok || !result.ok) throw new Error(result.message || `Upload failed (${response.status})`);
        await sources();
        $("datasetSelect").value = result.source;
        $("includeSubdirectories").checked = true;
        $("uploadStatus").textContent = "Uploaded. Click Load dataset to browse.";
      } catch (error) {
        $("uploadStatus").textContent = `Upload failed: ${error.message}`;
        toast(error.message, "danger");
      } finally {
        pending = false;
        input.value = "";
        if (state) fill(state);
      }
    }
    $("zipFiles").addEventListener("change", upload);
    $("folderFiles").addEventListener("change", upload);
    sources().then(() => poll(async () => {
      const r = await api("/drop/api/ndvi/input");
      if (r.ok && !pending) fill(r.input);
    }, 1000));
  }

  // ------------------------------------------------------------------ image metadata panel
  function imageMetaPanel() {
    const root = $("imageMeta");
    if (!root) return;
    let cams = null;
    let field = null;
    const num = (x, d = 2) => (isNum(x) ? (Number.isInteger(x) ? String(x) : x.toFixed(d)) : "–");
    const list = (x) => (Array.isArray(x) ? x.map((y) => num(y)).join(", ") : "–");
    const age = (s) => (!isNum(s) ? "–" : s < 90 ? `${s.toFixed(0)} s ago` : s < 5400 ? `${(s / 60).toFixed(0)} min ago` : `${(s / 3600).toFixed(1)} h ago`);

    async function loadCams() {
      const r = await api("/drop/api/cameras");
      if (r.ok) cams = r;
    }
    loadCams();

    function value(key, r) {
      const [role, k] = key.split(".");
      const md = r[role] || {};
      const cam = r.input_mode === "recorded" ? null : cams && cams[role];
      const f = r.frame;
      switch (k || key) {
        case "model": return cam ? `#${cam.num} ${cam.model || ""}` : "–";
        case "stream": return cam && cam.stream ? `${cam.stream.size[0]}×${cam.stream.size[1]} ${cam.stream.format}` : "–";
        case "size": return f ? f[`${role}_size`].join("×") : "–";
        case "crop": return f ? `${f[`${role}_crop`][2]}×${f[`${role}_crop`][3]} @ ${f[`${role}_crop`][0]},${f[`${role}_crop`][1]}` : "–";
        case "ExposureTime": return isNum(md.ExposureTime) ? `${md.ExposureTime} µs (1/${Math.round(1e6 / md.ExposureTime)} s)` : "–";
        case "AnalogueGain": case "DigitalGain": return num(md[k]);
        case "ColourGains": return list(md.ColourGains);
        case "ColourTemperature": return isNum(md.ColourTemperature) ? `${md.ColourTemperature} K` : "–";
        case "Lux": return num(md.Lux, 0);
        case "FrameDuration": return isNum(md.FrameDuration) ? `${(1e6 / md.FrameDuration).toFixed(1)} fps (${(md.FrameDuration / 1000).toFixed(1)} ms)` : "–";
        case "flip": return cam ? [cam.hflip && "H", cam.vflip && "V"].filter(Boolean).join(" + ") || "none" : "–";
        case "sync": return isNum(r.sync_ms) ? `${fmt(r.sync_ms, 1)} ms between IR and RED` : "–";
        case "captured": return r.input_mode === "recorded" ? "Recorded file (capture timestamp unavailable)" :
          f ? `${new Date(f.captured * 1000).toLocaleString()} (${age(f.age_s)})` : "–";
        case "position": {
          if (r.input_mode === "recorded") return "Recorded capture location unavailable";
          const l = field && field.location;
          if (!l) return "no fix";
          const alt = isNum(l.elevation) ? ` · ${l.elevation.toFixed(0)} m` : "";
          return `${l.latitude.toFixed(6)}, ${l.longitude.toFixed(6)}${alt} (${l.source})`;
        }
        case "gps": {
          if (r.input_mode === "recorded") return "Live GPS is not associated with recorded captures";
          const g = field && field.gps;
          if (!g) return "–";
          if (!g.available) return g.error || "not available";
          const fx = g.fix;
          const parts = fx ? [`fix ${fx.fix_quality ?? "ok"}`, isNum(fx.satellites) && `${fx.satellites} sats`,
            isNum(fx.hdop) && `HDOP ${fx.hdop}`, fx.utc_time && `UTC ${fx.utc_time}`, age(g.age_s)].filter(Boolean) : ["no fix"];
          if (g.error) parts.push(`last read: ${g.error}`);
          return parts.join(" · ");
        }
        case "profile": return (field && field.profile) || "–";
      }
      return "–";
    }

    poll(async () => {
      const r = await api("/drop/api/camera/metadata");
      if (!r.ok) return;
      $("metaFrame").textContent = r.frame ? `frame #${r.frame.id}` : "";
      root.querySelectorAll("[data-im]").forEach((td) => { td.textContent = value(td.dataset.im, r); });
    }, 1500);
    poll(async () => {
      const [f, m] = await Promise.all([api("/drop/api/field"), api("/drop/api/model?model=ndvi")]);
      if (f.ok) field = f;
      if (field && m.ok) field.profile = m.model.name ? `${m.model.name} (${m.model.kind})` : "none";
    }, 5000);
  }

  // ------------------------------------------------------------------ weather panel
  function weatherPanel(onEto) {
    if (!$("wxBody")) return;
    let filledLocation = false;
    const n = (v, d = 1, unit = "") => (isNum(v) ? `${v.toFixed(d)}${unit}` : "–");
    const hrs = (s) => (isNum(s) ? `${(s / 3600).toFixed(1)} h` : "–");
    const time = (t) => (t ? t.slice(11, 16) : "–");
    const compass = (deg) => (isNum(deg) ? ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(deg / 45) % 8] : "");
    const rows = (items) => items.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("");

    function render(st) {
      const w = st.weather;
      const l = st.location;
      $("wxWhere").textContent = l ? `· ${l.latitude.toFixed(4)}, ${l.longitude.toFixed(4)} (${l.source})` : "";
      $("wxAge").textContent = isNum(st.weather_age_s) ? `updated ${Math.round(st.weather_age_s / 60)} min ago` : "";
      if (!filledLocation && st.manual) {
        filledLocation = true;
        $("wxLat").value = st.manual.latitude;
        $("wxLon").value = st.manual.longitude;
        $("wxElev").value = st.manual.elevation ?? "";
        $("wxPrefer").checked = !!st.manual.prefer;
      }
      const status = [];
      if (!l) status.push(st.gps.error ? `No GPS fix (${st.gps.error}). Set the location manually with the pin button.` : "Waiting for a GPS fix…");
      if (st.weather_error) status.push(`Weather: ${st.weather_error}`);
      if (l && !w && !st.weather_error) status.push("Fetching weather…");
      $("wxStatus").textContent = status.join(" ");
      $("wxStatus").classList.toggle("d-none", !status.length);
      $("wxData").classList.toggle("d-none", !w);
      if (!w) return;

      const c = w.current, h = w.hourly_now || {}, d = w.today, x = w.next_24h || {};
      $("wxNow").innerHTML = `<tr><th colspan="2">Now · ${time(w.time)}</th></tr>` + rows([
        ["Air", `${n(c.temperature_2m, 1, " °C")} · RH ${n(c.relative_humidity_2m, 0, "%")} · dew ${n(c.dew_point_2m, 1, " °C")}`],
        ["VPD", n(c.vapour_pressure_deficit, 2, " kPa")],
        ["Wind", `${n(c.wind_speed_10m, 1, " m/s")} ${compass(c.wind_direction_10m)} · gusts ${n(c.wind_gusts_10m, 1, " m/s")}`],
        ["Radiation", `${n(c.shortwave_radiation, 0, " W/m²")} (direct ${n(c.direct_radiation, 0)}, diffuse ${n(c.diffuse_radiation, 0)})`],
        ["UV index", `${n(c.uv_index, 1)} (clear sky ${n(h.uv_index_clear_sky, 1)})`],
        ["Clouds", `${n(c.cloud_cover, 0, "%")} (low ${n(h.cloud_cover_low, 0, "%")})`],
        ["ET0 / ET", `${n(c.et0_fao_evapotranspiration, 2, " mm")} / ${n(c.evapotranspiration, 2, " mm")} per 15 min`],
        ["Rain", `${n(c.precipitation, 1, " mm")} · chance ${n(h.precipitation_probability, 0, "%")}`],
        ["Leaf wetness", n(h.leaf_wetness_probability, 0, "%")],
        ["Pressure", n(c.surface_pressure, 0, " hPa")],
      ]);
      $("wxToday").innerHTML = `<tr><th colspan="2">Today</th></tr>` + rows([
        ["ET0 (FAO-56)", `<b>${n(d.et0_fao_evapotranspiration, 2, " mm")}</b>`],
        ["Temperature", `${n(d.temperature_2m_min, 1)} – ${n(d.temperature_2m_max, 1, " °C")}`],
        ["Rain", `${n(d.precipitation_sum, 1, " mm")} in ${n(d.precipitation_hours, 0, " h")} · max chance ${n(d.precipitation_probability_max, 0, "%")}`],
        ["Radiation", n(d.shortwave_radiation_sum, 1, " MJ/m²")],
        ["UV max", `${n(d.uv_index_max, 1)} (clear sky ${n(d.uv_index_clear_sky_max, 1)})`],
        ["Sunshine", `${hrs(d.sunshine_duration)} of ${hrs(d.daylight_duration)}`],
        ["Sun", `${time(d.sunrise)} – ${time(d.sunset)}`],
        ["Wind max", `${n(d.wind_speed_10m_max, 1, " m/s")} ${compass(d.wind_direction_10m_dominant)} · gusts ${n(d.wind_gusts_10m_max, 1, " m/s")}`],
        ["Next 24 h", `ET0 ${n(x.et0_mm, 1, " mm")} · rain ${n(x.precip_mm, 1, " mm")} (max ${n(x.precip_probability_max, 0, "%")})`],
      ]);
      document.querySelectorAll("[data-wx]").forEach((td) => {
        const [grp, k] = td.dataset.wx.split(".");
        const v = (grp === "sm" ? w.soil_moisture_m3m3 : w.soil_temperature_c)[k];
        td.textContent = n(v, grp === "sm" ? 3 : 1);
      });
      const b = w.water_balance;
      const line = (label, t) => `<tr><td class="text-start">${label}</td><td>${n(t.etc_mm, 1)}</td><td>${n(t.precip_mm, 1)}</td>` +
        `<td class="${t.deficit_mm > 0 ? "text-danger" : "text-success"}"><b>${n(t.deficit_mm, 1)}</b></td></tr>`;
      $("wxBalance").innerHTML = line(`Last ${b.past.days} days`, b.past) + line(`Next ${b.forecast.days} days`, b.forecast);
    }

    const load = async () => {
      const r = await api("/drop/api/field");
      if (r.ok) render(r);
      onEto();   // the worker may have synced ETo from the weather
    };
    $("wxRefresh").addEventListener("click", async () => {
      report(await api("/drop/api/field/refresh", {}), "Refreshing location and weather…");
      setTimeout(load, 3000);
    });
    $("wxUseEto").addEventListener("click", async () => {
      const r = await api("/drop/api/field/use_eto", {});
      report(r, (x) => `ETo set to ${fmt(x.eto, 2)} mm/day`);
      if (r.ok) onEto();
    });
    $("wxSave").addEventListener("click", async () => {
      const lat = $("wxLat").value, lon = $("wxLon").value;
      if (lat === "" || lon === "") return toast("Enter latitude and longitude", "danger");
      const r = await api("/drop/api/field/location", {
        latitude: +lat, longitude: +lon, elevation: $("wxElev").value === "" ? null : +$("wxElev").value,
        prefer: $("wxPrefer").checked,
      });
      report(r, "Location saved — fetching weather…");
      if (r.ok) { render(r); setTimeout(load, 3000); }
    });
    $("wxClear").addEventListener("click", async () => {
      const r = await api("/drop/api/field/location", { clear: true });
      report(r, "Manual location cleared");
      ["wxLat", "wxLon", "wxElev"].forEach((id) => { $(id).value = ""; });
      $("wxPrefer").checked = false;
      if (r.ok) render(r);
    });
    poll(load, 10000);
  }

  // ------------------------------------------------------------------ calibration page
  function calibrationPage(recommended) {
    initViews();
    cameraPanel();
    const tun = tuningPanel($("tuningPanel"));
    const form = $("newForm");
    let sess = null, capturing = false, lastCapture = 0, history = [], liveCorners = null;

    function kindToggle(kind, scope) {
      scope.querySelectorAll(".stereo-only").forEach((el) => el.classList.toggle("d-none", kind !== "stereo"));
      scope.querySelectorAll(".ecc-only").forEach((el) => el.classList.toggle("d-none", kind !== "ecc"));
    }
    $("kindSelect").addEventListener("change", (e) => kindToggle(e.target.value, $("stepNew")));

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(form).entries());
      for (const k of ["cols", "rows", "square_mm", "distance_m", "target_pairs"]) d[k] = +d[k];
      d.name = slug(d.name);
      const r = await api("/drop/api/calib/new", d);
      report(r, "Calibration session started");
      if (r.ok) { sess = r.session; render(); }
    });

    function levelClass(level) {
      return { good: "bg-success", ok: "bg-warning text-dark", poor: "bg-danger" }[level] || "bg-secondary";
    }

    function render() {
      $("stepNew").classList.toggle("d-none", !!sess);
      $("stepSession").classList.toggle("d-none", !sess);
      $("pairsCard").classList.toggle("d-none", !sess);
      if (!sess) return;
      const p = sess.params;
      kindToggle(p.kind, $("stepSession"));
      $("sessName").textContent = p.name;
      $("sessInfo").textContent = (p.kind === "stereo" ?
        `Checkerboard ${p.board.cols}×${p.board.rows} inner corners, ${fmt(p.board.square_m * 1000, 1)} mm · ` : "ECC · ") +
        `crop at ${p.distance_m} m${isNum(p.pitch_deg) ? ` @ ${p.pitch_deg}°` : ""} · ${p.source} · images in ${p.folder}`;
      const errors = {};
      if (sess.result && sess.result.pair_errors) sess.result.pair_errors.forEach((e) => { errors[e.idx] = e; });
      const target = p.kind === "stereo" ? p.target_pairs : 1;
      $("pairCount").textContent = sess.pairs.length;
      $("pairTarget").textContent = target;
      $("pairProgress").style.width = `${Math.min(100, 100 * sess.pairs.length / target)}%`;
      $("pairList").innerHTML = sess.pairs.map((x) => {
        const e = errors[x.idx];
        const badge = e ? `<span class="badge ${e.error > 1 ? "bg-danger" : e.error > 0.5 ? "bg-warning text-dark" : "bg-success"}">${fmt(e.error, 2)} px</span> <span class="small">${fmt(e.distance_m, 2)} m</span>` : "";
        return `<div class="drop-pair card p-1"><img src="/drop/api/calib/thumb/${x.idx}.jpg?s=${encodeURIComponent(p.created)}" alt="pair ${x.idx}">` +
          `<div class="d-flex align-items-center gap-1 mt-1"><b class="small me-auto">#${x.idx}</b>${badge}` +
          `<button class="btn btn-sm btn-outline-danger py-0" data-del="${x.idx}" title="Delete pair"><i class="bi bi-trash"></i></button></div></div>`;
      }).join("") || '<span class="small text-body-secondary">No pairs yet.</span>';
      $("pairList").querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
        const r = await api("/drop/api/calib/delete_pair", { idx: +b.dataset.del });
        report(r);
        if (r.ok) { sess = r.session; render(); }
      }));

      const res = sess.result;
      $("resultBox").classList.toggle("d-none", !res);
      $("saveBox").classList.toggle("d-none", !res);
      if (res) {
        $("resLevel").className = `badge fs-6 ${levelClass(sess.level)}`;
        $("resLevel").textContent = (sess.level || "").toUpperCase();
        $("resMain").textContent = p.kind === "stereo" ?
          `RMS ${fmt(res.rms_stereo, 3)} px (IR ${fmt(res.rms_ir, 2)}, RED ${fmt(res.rms_red, 2)}) · coverage ${pct(Math.min(res.coverage_ir, res.coverage_red))}` :
          `residual ${fmt(res.residual_px, 2)} px · spread ${fmt(res.spread_px, 2)} px · ECC ${fmt(res.correlation, 2)}`;
        $("resMessages").innerHTML = sess.messages.map((m) => `<li>${m}</li>`).join("");
        if (!$("saveName").value) $("saveName").value = p.name;
        tun.load();
      }
      $("saveMsg").textContent = sess.saved_as ? `Saved as “${sess.saved_as}”.` : "";
    }

    async function capture() {
      if (capturing || !sess) return;
      capturing = true;
      const btn = $("btnCapture");
      busy(btn, true);
      $("captureMsg").textContent = sess.params.source === "still" ? "Capturing full-resolution stills — hold the board still…" : "Capturing…";
      const r = await api("/drop/api/calib/capture", {});
      busy(btn, false);
      capturing = false;
      lastCapture = Date.now();
      $("captureMsg").textContent = r.message || "";
      $("captureMsg").className = `small mt-1 ${r.ok ? "text-success" : "text-danger"}`;
      if (r.session) { sess = r.session; render(); }
    }
    $("btnCapture").addEventListener("click", capture);

    $("btnRun").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      busy(btn, true);
      const r = await api("/drop/api/calib/run", {});
      busy(btn, false);
      report(r, (x) => `Calibration ${x.session.level}`);
      if (r.ok) { sess = r.session; render(); setProcView("edges"); }
    });

    $("btnSave").addEventListener("click", async () => {
      const r = await api("/drop/api/calib/save", {
        name: slug($("saveName").value), overwrite: $("saveOverwrite").checked, use_in_ndvi: $("saveUse").checked,
      });
      report(r, "Profile saved");
      if (r.ok) { sess = r.session; render(); loadProfiles(); }
    });

    $("btnDiscard").addEventListener("click", async () => {
      if (!confirm("Discard this calibration session? The captured images stay in data/calibration_sessions/.")) return;
      const r = await api("/drop/api/calib/discard", {});
      report(r);
      sess = null;
      $("autoCapture").checked = false;
      render();
    });

    // ---- profiles table
    async function loadProfiles() {
      const r = await api("/drop/api/profiles");
      if (!r.ok) return report(r);
      $("profileRows").innerHTML = r.profiles.map((p) => {
        const plane = p.plane ? `${p.plane.distance_m} m${isNum(p.plane.pitch_deg) ? " @ " + p.plane.pitch_deg + "°" : ""}` : "–";
        const q = p.quality && p.quality.level ? `<span class="badge ${levelClass(p.quality.level)}">${p.quality.level}</span>` : "";
        const active = p.name === r.active ? ' <span class="badge bg-primary">NDVI</span>' : "";
        return `<tr><td>${p.name}${active}</td><td>${p.type}</td><td>${q}</td><td>${fmt(p.rms, 2)}</td><td>${p.n_pairs ?? "–"}</td>` +
          `<td>${plane}</td><td>${p.updated || ""}</td><td class="text-end text-nowrap">` +
          `<button class="btn btn-sm btn-outline-primary py-0" data-use="${p.name}">Use in NDVI</button> ` +
          `<button class="btn btn-sm btn-outline-danger py-0" data-delprof="${p.name}"><i class="bi bi-trash"></i></button></td></tr>`;
      }).join("") || '<tr><td colspan="8" class="text-body-secondary">No profiles yet.</td></tr>';
      $("profileRows").querySelectorAll("[data-use]").forEach((b) => b.addEventListener("click", async () => {
        report(await api("/drop/api/ndvi/profile", { name: b.dataset.use }), `“${b.dataset.use}” is now used for NDVI`);
        loadProfiles();
      }));
      $("profileRows").querySelectorAll("[data-delprof]").forEach((b) => b.addEventListener("click", async () => {
        if (!confirm(`Delete calibration profile “${b.dataset.delprof}”?`)) return;
        report(await api("/drop/api/profiles/delete", { name: b.dataset.delprof }), "Profile deleted");
        loadProfiles();
      }));
    }
    loadProfiles();

    // ---- live detection, overlays, auto capture
    function quad(pts, cols) {
      const n = pts.length;
      return [pts[0], pts[cols - 1], pts[n - 1], pts[n - cols]];
    }
    function centerSize(q) {
      const cx = q.reduce((s, p) => s + p[0], 0) / 4, cy = q.reduce((s, p) => s + p[1], 0) / 4;
      let area = 0;
      for (let i = 0; i < 4; i++) { const a = q[i], b = q[(i + 1) % 4]; area += a[0] * b[1] - b[0] * a[1]; }
      return { cx, cy, area: Math.abs(area) / 2 };
    }
    function boardCols() {
      return sess ? sess.params.board.cols : +form.cols.value;
    }

    function drawAll() {
      for (const role of ["ir", "red"]) {
        const shapes = [];
        if (sess) for (const p of sess.pairs) {
          if (p.outline && p.outline[role]) shapes.push({ type: "poly", pts: p.outline[role], color: "rgba(13,110,253,.7)", fill: "rgba(13,110,253,.12)" });
        }
        const c = liveCorners && liveCorners[role];
        if (c) {
          shapes.push({ type: "points", pts: c, color: "#20c997", radius: 2 });
          shapes.push({ type: "poly", pts: quad(c, boardCols()), color: "#20c997", width: 2 });
        }
        drawOverlay(role, shapes);
      }
    }
    window.addEventListener("resize", drawAll);
    document.addEventListener("fullscreenchange", () => setTimeout(drawAll, 100));

    function badge(id, label, found) {
      const el = $(id);
      el.className = `badge ${found ? "bg-success" : "bg-secondary"}`;
      el.textContent = `${label} ${found ? "✓ board" : "✗ no board"}`;
    }

    function showBoardBadges(show) {
      ["detIr", "detRed"].forEach((id) => $(id).classList.toggle("d-none", !show));
      document.querySelectorAll("[data-badge]").forEach((el) => el.classList.toggle("d-none", !show));
    }

    poll(async () => {
      const ecc = sess ? sess.params.kind === "ecc" : $("kindSelect").value === "ecc";
      showBoardBadges(!ecc);
      if (ecc) {
        $("detHint").textContent = "Point both cameras at a flat, textured scene at the crop distance (ideally the crop itself) and capture one pair.";
        liveCorners = null; drawAll(); return;
      }
      const qs = sess ? "" : `?cols=${form.cols.value}&rows=${form.rows.value}`;
      const r = await api("/drop/api/calib/live" + qs);
      if (!r.ok) return;
      $("statusLine").textContent = r.status || "";
      liveCorners = r.corners;
      const fi = !!(liveCorners && liveCorners.ir), fr = !!(liveCorners && liveCorners.red);
      badge("detIr", "IR", fi);
      badge("detRed", "RED", fr);
      document.querySelectorAll("[data-badge]").forEach((el) => {
        const f = el.dataset.badge === "ir" ? fi : fr;
        el.className = `drop-badge badge ${f ? "bg-success" : "bg-secondary"}`;
        el.textContent = f ? "board ✓" : "no board";
      });
      $("detHint").textContent = fi && fr ? "Board visible in both — ready." : "Show the whole board to both cameras.";
      drawAll();

      // auto capture: board in both views, held still, and in a new position/size
      if (!(sess && $("autoCapture").checked && fi && fr) || capturing) { history = []; return; }
      const cs = centerSize(quad(liveCorners.ir, boardCols()));
      history.push(cs);
      if (history.length > 3) history.shift();
      const still = history.length === 3 && history.every((h) =>
        Math.hypot(h.cx - cs.cx, h.cy - cs.cy) < 0.01 && Math.abs(h.area - cs.area) < 0.03 * cs.area);
      const novel = sess.pairs.every((p) => {
        if (!p.outline || !p.outline.ir) return true;
        const o = centerSize(p.outline.ir);
        return Math.hypot(o.cx - cs.cx, o.cy - cs.cy) > 0.08 || Math.abs(o.area - cs.area) > 0.25 * Math.max(o.area, cs.area);
      });
      if (still && novel && Date.now() - lastCapture > 2500 && sess.pairs.length < 60) {
        history = [];
        capture();
      }
    }, 500);

    // residual of the unsaved result (or the NDVI profile) for the tuning box
    poll(async () => {
      const r = await api("/drop/api/ndvi/stats?model=calib");
      if (r.ok && r.stats && r.stats.quality) tun.setResidual(r.stats.quality.alignment);
    }, 2000);

    api("/drop/api/calib/status").then((r) => {
      if (r.ok && r.session) sess = r.session;
      render();
      tun.load();
    });
  }

  return { api, ndviPage, calibrationPage, setProcView, drawOverlay };
})();
