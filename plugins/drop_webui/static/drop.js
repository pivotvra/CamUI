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

    poll(async () => {
      const r = await api("/drop/api/ndvi/stats");
      if (!r.ok) return;
      $("statusLine").textContent = r.status || "";
      const s = r.stats;
      if (!s) return;
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
