"""
Live NDVI / alignment service shared by the NDVI and Calibration tabs.

A worker thread grabs IR/RED pairs from the CamUI cameras while a page is
open (clients poll or stream), downsizes them to `proc_width` and keeps the
latest pair. Everything else (aligned bands, NDVI, rendered views, stats,
checkerboard detection) is computed lazily per frame and cached.

Processing happens in the sensor's native orientation, so calibration
profiles do not depend on CamUI's hflip/vflip; views and NDVI zones are
flipped to the IR camera's display orientation at the end.
"""
import json
import math
import os
import threading
import time

import cv2
import numpy as np

import alignment
import calib_core
import config
import frame_quality
from irrigation import calculate_irrigation

from . import camera_io, render
from .field_env import FieldEnvironment
from .recorded import RecordedData

IDLE_TIMEOUT_S = 10.0

DEFAULT_SETTINGS = {
    "ir_cam": getattr(config, "CAM_IR", 0),
    "red_cam": getattr(config, "CAM_RED", 1),
    "profile": getattr(config, "CALIBRATION_PROFILE", None),
    "ir_band": "gray",          # gray | r | g | b
    "red_band": "r",            # r | g | b | gray
    "red_gain": 1.0,            # RED multiplier before NDVI (reference-panel correction)
    "blur_sigma": 0.0,          # smoothing of both bands before NDVI (processing px)
    "exclude_saturated": True,
    "ndvi_min": -0.2,           # colour scale
    "ndvi_max": 0.9,
    "veg_threshold": getattr(config, "NDVI_VEGETATION_THRESHOLD", 0.10),
    "mask_start": round(config.MASK_ROW_START / config.CAPTURE_HEIGHT, 4),
    "mask_end": round(config.MASK_ROW_END / config.CAPTURE_HEIGHT, 4),
    "n_zones": getattr(config, "N_ZONES", 5),
    "eto": 5.0,                 # mm/day, for the irrigation estimate
    "eto_from_weather": False,  # keep eto synced to today's Open-Meteo FAO-56 ET0
    "proc_width": 820,
    "fps": 3.0,
    "recorded_crop": "full",
    "camera_extras": {},        # per camera number: controls CamUI profiles cannot store (ColourGains)
}

LIMITS = {
    "red_gain": (0.05, 20.0), "blur_sigma": (0.0, 10.0), "ndvi_min": (-1.0, 1.0),
    "ndvi_max": (-1.0, 1.0), "veg_threshold": (-1.0, 1.0), "mask_start": (0.0, 1.0),
    "mask_end": (0.0, 1.0), "n_zones": (1, 20), "eto": (0.0, 20.0),
    "proc_width": (320, 1640), "fps": (0.5, 10.0), "ir_cam": (0, 7), "red_cam": (0, 7),
}
CHOICES = {"ir_band": ("gray", "r", "g", "b"), "red_band": ("r", "g", "b", "gray"),
           "recorded_crop": ("full", "profile")}

CONTROL_TYPES = {"AeEnable": int, "AwbEnable": int, "ExposureTime": int,
                 "AnalogueGain": float, "ExposureValue": float}
METADATA_KEYS = ("ExposureTime", "AnalogueGain", "DigitalGain", "ColourGains", "Lux",
                 "ColourTemperature", "FrameDuration", "ScalerCrop", "AeLocked", "SensorTimestamp")


def _num(v):
    if isinstance(v, (list, tuple)):
        return [_num(x) for x in v]
    if isinstance(v, (np.floating, float)):
        return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, (np.integer, int)):
        return int(v)
    return v


def band_from(bgr, which):
    if which == "gray":
        return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    idx = {"b": 0, "g": 1, "r": 2}[which]
    return bgr[:, :, idx].astype(np.float32) / 255.0


class Settings:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.rev = 0
        try:
            with open(path) as f:
                stored = json.load(f)
            self.data.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data[key]

    def update(self, values):
        clean = {}
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS or key in ("camera_extras", "profile"):
                continue
            if key in CHOICES:
                if value not in CHOICES[key]:
                    raise ValueError(f"{key} must be one of {CHOICES[key]}")
            elif isinstance(DEFAULT_SETTINGS[key], bool):
                value = bool(value)
            else:
                lo, hi = LIMITS[key]
                value = type(DEFAULT_SETTINGS[key])(float(value)) if isinstance(DEFAULT_SETTINGS[key], (int, float)) else value
                value = min(max(value, lo), hi)
            clean[key] = value
        self.data.update(clean)
        if self.data["mask_end"] <= self.data["mask_start"]:
            self.data["mask_end"] = min(1.0, self.data["mask_start"] + 0.02)
        self.rev += 1
        self.save()
        return clean

    def set(self, key, value):
        self.data[key] = value
        self.rev += 1
        self.save()

    def save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)


class DropService:
    def __init__(self, cameras, data_dir, repo=None):
        self.cameras = cameras
        self.data_dir = data_dir
        self.settings = Settings(os.path.join(data_dir, "webui_settings.json"))
        self.lock = threading.RLock()
        self.frame_cond = threading.Condition()
        self.frame = None
        self.frame_id = 0
        self.last_client = 0.0
        self.worker = None
        self.paused = 0
        self.cache = {}
        self.cache_fid = None
        self.models = {"ndvi": self._load_model(self.settings["profile"]), "calib": None}
        self.model_rev = {"ndvi": 0, "calib": 0}
        self.status = ""
        self.calib = None
        self.input_mode = "live"
        self.recorded_display_rev = None
        self.recorded = RecordedData(self, repo or os.path.dirname(data_dir))
        self._apply_camera_extras()
        self.env = FieldEnvironment(repo or os.path.dirname(data_dir), data_dir, self._on_weather)
        if self.settings["eto_from_weather"]:
            self.env.start()

    def _on_weather(self, summary):
        et0 = (summary.get("today") or {}).get("et0_fao_evapotranspiration")
        if self.settings["eto_from_weather"] and et0 is not None:
            self.settings.update({"eto": round(float(et0), 2)})

    def apply_weather_eto(self):
        summary = self.env.state()["weather"]
        et0 = ((summary or {}).get("today") or {}).get("et0_fao_evapotranspiration")
        if et0 is None:
            raise RuntimeError("no weather data yet")
        self.settings.update({"eto": round(float(et0), 2)})
        return self.settings["eto"]

    # ------------------------------------------------------------------ cameras

    def cam(self, role):
        return self.cameras.get(int(self.settings["ir_cam" if role == "ir" else "red_cam"]))

    def roles(self, role):
        return ["ir", "red"] if role == "both" else [role]

    def _apply_camera_extras(self):
        for num, extras in (self.settings["camera_extras"] or {}).items():
            cam = self.cameras.get(int(num))
            gains = extras.get("ColourGains")
            if cam and gains and not self._control_value(cam, "AwbEnable", 1):
                try:
                    cam.picam2.set_controls({"ColourGains": tuple(float(g) for g in gains)})
                except Exception as exc:
                    print(f"[drop-rpi] ColourGains on camera {num}: {exc}")

    @staticmethod
    def _control_value(cam, key, default=None):
        return (cam.camera_profile.get("controls") or {}).get(key, default)

    def camera_info(self, role):
        cam = self.cam(role)
        if cam is None:
            return None
        cc = cam.picam2.camera_controls
        ranges = {k: _num(list(cc[k])) for k in ("ExposureTime", "AnalogueGain", "ExposureValue", "ColourGains")
                  if k in cc}
        extras = (self.settings["camera_extras"] or {}).get(str(cam.camera_info["Num"]), {})
        values = {k: _num(v) for k, v in (cam.camera_profile.get("controls") or {}).items()
                  if k in CONTROL_TYPES}
        if "ColourGains" in extras:
            values["ColourGains"] = extras["ColourGains"]
        try:
            active_mode = cam.get_sensor_mode()
        except Exception:
            active_mode = cam.camera_profile.get("sensor_mode")
        modes = [{"index": i, "size": list(m["size"]), "fps": round(float(m.get("fps", 0)), 1),
                  "bit_depth": m.get("bit_depth"), "crop": _num(list(m.get("crop_limits", [])))}
                 for i, m in enumerate(cam.sensor_modes)]
        try:
            main = cam.picam2.camera_configuration()["main"]
            stream = {"size": list(main["size"]), "format": main["format"]}
        except Exception:
            stream = None
        return {"role": role, "num": cam.camera_info["Num"], "model": cam.camera_info.get("Model"),
                "ranges": ranges, "values": values, "sensor_modes": modes, "sensor_mode": active_mode,
                "hflip": bool(cam.camera_profile.get("hflip")), "vflip": bool(cam.camera_profile.get("vflip")),
                "stream": stream}

    def metadata(self, role):
        if self.input_mode == "recorded":
            return {}
        cam = self.cam(role)
        if cam is None:
            return None
        frame = self.frame
        if frame is not None and time.time() - frame["t"] < 3:
            md = frame[role].metadata
        else:
            md = cam.picam2.capture_metadata()
        return {k: _num(md[k]) for k in METADATA_KEYS if k in md}

    def set_control(self, role, key, value):
        self.require_live()
        applied = {}
        for r in self.roles(role):
            cam = self.cam(r)
            if cam is None:
                continue
            if key == "ColourGains":
                gains = [float(value[0]), float(value[1])]
                cam.update_settings("AwbEnable", 0)
                cam.picam2.set_controls({"ColourGains": tuple(gains)})
                extras = self.settings["camera_extras"] or {}
                extras.setdefault(str(cam.camera_info["Num"]), {})["ColourGains"] = gains
                self.settings.set("camera_extras", extras)
                applied[r] = gains
                continue
            if key == "sensor_mode":
                cam.update_settings("sensor_mode", int(value))
                self._apply_camera_extras()
                applied[r] = int(value)
                continue
            if key not in CONTROL_TYPES:
                raise ValueError(f"unsupported control {key}")
            typed = CONTROL_TYPES[key](float(value))
            if key == "ExposureTime" or key == "AnalogueGain":
                # a manual value only sticks with AE off; stop CamUI re-enabling AE on first stream
                cam.update_settings("AeEnable", 0)
                cam._did_runtime_ae_enable_sync = True
            if key == "AeEnable" and not typed:
                cam._did_runtime_ae_enable_sync = True
            if key == "AwbEnable" and typed:
                extras = self.settings["camera_extras"] or {}
                extras.get(str(cam.camera_info["Num"]), {}).pop("ColourGains", None)
                self.settings.set("camera_extras", extras)
            cam.update_settings(key, typed)
            applied[r] = typed
        return applied

    def lock_exposure(self, role):
        out = {}
        for r in self.roles(role):
            md = self.metadata(r) or {}
            if "ExposureTime" not in md:
                continue
            self.set_control(r, "ExposureTime", md["ExposureTime"])
            self.set_control(r, "AnalogueGain", md.get("AnalogueGain", 1.0))
            out[r] = {"ExposureTime": md["ExposureTime"], "AnalogueGain": md.get("AnalogueGain")}
        return out

    def copy_exposure(self):
        md = self.metadata("ir") or {}
        if "ExposureTime" not in md:
            raise RuntimeError("no IR metadata")
        for r in ("ir", "red"):
            self.set_control(r, "ExposureTime", md["ExposureTime"])
            self.set_control(r, "AnalogueGain", md.get("AnalogueGain", 1.0))
        return {"ExposureTime": md["ExposureTime"], "AnalogueGain": md.get("AnalogueGain")}

    def save_camera_profile(self, role, name):
        if not alignment.valid_name(name):
            raise ValueError("invalid name (letters, digits, _ . - only)")
        return {r: self.cam(r).save_profile(f"{name}_{r}") for r in self.roles(role) if self.cam(r)}

    # ------------------------------------------------------------------ worker

    def touch(self):
        self.last_client = time.time()
        with self.lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, name="drop-live", daemon=True)
                self.worker.start()

    def pause(self):
        with self.lock:
            self.paused += 1

    def resume(self):
        with self.lock:
            self.paused = max(0, self.paused - 1)

    def _run(self):
        while time.time() - self.last_client < IDLE_TIMEOUT_S:
            t0 = time.time()
            if self.paused or self.input_mode == "recorded":
                time.sleep(0.1)
                continue
            cam_ir, cam_red = self.cam("ir"), self.cam("red")
            if cam_ir is None or cam_red is None:
                with self.lock:
                    if self.input_mode == "live":
                        self.status = "IR/RED camera not found — check the camera numbers in the settings"
                time.sleep(1)
                continue
            ir, red = camera_io.grab_pair(cam_ir, cam_red)
            if ir is None or red is None:
                with self.lock:
                    if self.input_mode == "live":
                        self.status = "waiting for the cameras…"
                time.sleep(0.2)
                continue
            with self.lock:
                if self.input_mode == "live":
                    self.status = ""
                    self._publish(ir, red)
            time.sleep(max(0.0, 1.0 / float(self.settings["fps"]) - (time.time() - t0)))

    def _publish(self, ir, red):
        width = int(self.settings["proc_width"])

        def small(frame):
            h, w = frame.bgr.shape[:2]
            s = min(1.0, width / w)
            img = cv2.resize(frame.bgr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else frame.bgr
            return img

        ir_s, red_s = small(ir), small(red)
        ir.bgr, red.bgr = None, None   # keep only the metadata of the full frames
        with self.frame_cond:
            self.frame_id += 1
            self.frame = {"id": self.frame_id, "t": time.time(), "ir": ir, "red": red,
                          "ir_s": ir_s, "red_s": red_s, "sync_ms": camera_io.sync_ms(ir, red)}
            self.frame_cond.notify_all()

    def wait_frame(self, after_id, timeout=5.0):
        self.touch()
        self._refresh_recorded()
        with self.frame_cond:
            self.frame_cond.wait_for(lambda: self.frame_id > after_id, timeout=timeout)
            return self.frame

    def fresh(self, max_age=1.0, timeout=3.0):
        """Latest frame, waiting for a new one if the current is stale (e.g. worker was idle)."""
        self.touch()
        if self.input_mode == "recorded":
            self._refresh_recorded()
            return self.frame
        f = self.frame
        if f is None or time.time() - f["t"] > max_age:
            f = self.wait_frame(self.frame_id, timeout)
        return f

    def _refresh_recorded(self):
        if self.input_mode != "recorded" or self.frame is None:
            return
        with self.lock:
            revision = (self.settings.rev, self.model_rev["ndvi"], self.model_rev["calib"])
            if revision != self.recorded_display_rev:
                self.recorded_display_rev = revision
                for role in ("ir", "red"):
                    self.frame[role].crop = self.recorded.crop(role, self.model("ndvi"))
                with self.frame_cond:
                    self.frame_id += 1
                    self.frame["id"] = self.frame_id
                    self.frame_cond.notify_all()

    # ------------------------------------------------------------------ models

    @staticmethod
    def _load_model(name):
        if not name:
            return alignment.AlignmentModel.identity()
        try:
            return alignment.AlignmentModel.from_name(name)
        except Exception as exc:
            print(f"[drop-rpi] calibration profile {name!r} not loaded: {exc}")
            return alignment.AlignmentModel.identity()

    def model(self, key):
        m = self.models.get(key)
        if m is None:
            return self.models["ndvi"]
        return m

    def model_key(self, key):
        return key if self.models.get(key) is not None else "ndvi"

    def set_model(self, key, model):
        with self.lock:
            self.models[key] = model
            self.model_rev[key] += 1

    def set_active_profile(self, name):
        model = alignment.AlignmentModel.identity() if not name else alignment.AlignmentModel.from_name(name)
        self.set_model("ndvi", model)
        self.settings.set("profile", name or None)
        return model

    def tune(self, key, distance_m=None, pitch_deg=None, offset_px=None):
        with self.lock:
            model = self.model(self.model_key(key))
            model.set_tuning(distance_m, pitch_deg, offset_px)
            self.model_rev[self.model_key(key)] += 1
            return model

    def model_state(self, key):
        k = self.model_key(key)
        m = self.model(k)
        saved = None
        if m.name and k == "ndvi":
            try:
                p = alignment.load_profile(m.name)
                saved = {"plane": p.get("plane"), "offset_px": p.get("offset_px")}
            except Exception:
                saved = None
        return {"source": k, "name": m.name, "kind": m.kind, "plane": m.plane, "offset_px": m.offset_px,
                "describe": m.describe(), "saved": saved,
                "flips": (m.profile or {}).get("flips"), "crop_ir": (m.profile or {}).get("crop_ir")}

    # ------------------------------------------------------------------ processing

    def _cached(self, key, fn):
        fid = self.frame["id"] if self.frame else None
        with self.lock:
            if fid != self.cache_fid:
                self.cache = {}
                self.cache_fid = fid
            full_key = key + (self.settings.rev,)
            if full_key not in self.cache:
                self.cache[full_key] = fn()
            return self.cache[full_key]

    def processed(self, key="ndvi"):
        frame = self.frame
        if frame is None:
            return None
        k = self.model_key(key)

        def compute():
            s = self.settings
            ir = band_from(frame["ir_s"], s["ir_band"])
            red = band_from(frame["red_s"], s["red_band"])
            model = self.model(k)
            crop_ir, crop_red = frame["ir"].crop, frame["red"].crop
            red_al = model.warp_red(red, ir.shape, crop_ir, crop_red)
            valid = model.warp_red(np.ones_like(red), ir.shape, crop_ir, crop_red) > 0.99
            sat = None
            if s["exclude_saturated"]:
                red_sat = model.warp_red((frame["red_s"].max(axis=2) >= 250).astype(np.float32),
                                         ir.shape, crop_ir, crop_red) > 0.01
                sat = (frame["ir_s"].max(axis=2) >= 250) | red_sat
            return {"ir": ir, "red_raw": red, "red": red_al, "valid": valid, "sat": sat,
                    "flip": camera_io.flip_code(frame["ir"].hflip, frame["ir"].vflip),
                    "crop_ir": crop_ir, "crop_red": crop_red, "frame": frame}

        return self._cached(("proc", k, self.model_rev[k]), compute)

    @staticmethod
    def to_display(img, flip):
        return img if flip is None else cv2.flip(img, flip)

    def ndvi(self, key="ndvi"):
        p = self.processed(key)
        if p is None:
            return None
        k = self.model_key(key)

        def compute():
            s = self.settings
            ir, red = p["ir"], p["red"] * float(s["red_gain"])
            if s["blur_sigma"] > 0:
                ir = cv2.GaussianBlur(ir, (0, 0), float(s["blur_sigma"]))
                red = cv2.GaussianBlur(red, (0, 0), float(s["blur_sigma"]))
            denom = ir + red
            ok = p["valid"] & (denom > 2 / 255)
            if p["sat"] is not None:
                ok &= ~p["sat"]
            ndvi = np.full(ir.shape, np.nan, np.float32)
            ndvi[ok] = np.clip((ir[ok] - red[ok]) / denom[ok], -1, 1)
            return ndvi
        return self._cached(("ndvi", k, self.model_rev[k]), compute)

    def render(self, view, key="ndvi"):
        if view not in render.VIEWS:
            raise KeyError(view)
        p = self.processed(key)
        if p is None:
            return render.jpeg(render.message_frame(self.status or "waiting for the cameras…"))
        k = self.model_key(key)

        def compute():
            s = self.settings
            ir, red, valid = p["ir"], p["red"], p["valid"]
            if view == "overlay":
                img = render.overlay(ir, red, valid)
            elif view == "edges":
                img = render.edges(ir, red, valid)
            elif view == "checker":
                img = render.checker(ir, red, valid)
            elif view == "blink":
                img = render.gray_bgr(render.stretch(ir if p["frame"]["id"] % 2 else red, valid))
            elif view == "diff":
                img = render.diff(ir, red, valid)
            elif view == "ir":
                img = render.gray_bgr(render.stretch(ir))
            elif view == "red":
                img = render.gray_bgr(render.stretch(red, valid))
            elif view == "ndvi":
                img = render.ndvi_color(self.ndvi(key), s["ndvi_min"], s["ndvi_max"], valid)
            else:
                img = render.ndvi_masked(self.ndvi(key), ir, s["ndvi_min"], s["ndvi_max"], s["veg_threshold"], valid)
            img = self.to_display(img, p["flip"])
            if view in ("ndvi", "ndvi_masked"):
                stats = self.stats(key)
                render.draw_zones(img, s["mask_start"], s["mask_end"], int(s["n_zones"]),
                                  [z["ndvi"] for z in stats["zones"]])
                render.colorbar(img, s["ndvi_min"], s["ndvi_max"])
            if view == "blink":
                render.label(img, "IR" if p["frame"]["id"] % 2 else "RED")
            return render.jpeg(img)
        return self._cached(("view", view, k, self.model_rev[k]), compute)

    def stats(self, key="ndvi"):
        p = self.processed(key)
        if p is None:
            return None
        k = self.model_key(key)

        def compute():
            s = self.settings
            frame = p["frame"]
            ndvi = self.to_display(self.ndvi(key), p["flip"])
            h, w = ndvi.shape
            y0, y1 = int(s["mask_start"] * h), max(int(s["mask_start"] * h) + 1, int(s["mask_end"] * h))
            roi = ndvi[y0:y1]
            n = int(s["n_zones"])
            edges = np.linspace(0, w, n + 1).astype(int)
            zones = []
            for i in range(n):
                vals = roi[:, edges[i]:edges[i + 1]]
                vals = vals[np.isfinite(vals)]
                veg = vals[vals > s["veg_threshold"]]
                mean = float(veg.mean()) if veg.size else None
                zone = {"zone": i + 1, "ndvi": mean,
                        "veg_fraction": float(veg.size / vals.size) if vals.size else 0.0,
                        "valid_fraction": float(vals.size / max(1, (y1 - y0) * (edges[i + 1] - edges[i])))}
                if mean is not None:
                    zone.update(calculate_irrigation(mean, float(s["eto"])))
                zones.append(zone)
            finite = ndvi[np.isfinite(ndvi)]

            quality = {}
            for label, band in (("ir", p["ir"]), ("red", p["red_raw"])):
                e = frame_quality.exposure_metrics(band)
                quality[label] = {"mean": e["mean"], "p99": e["p99"], "saturated": e["saturated_fraction"],
                                  "dark": e["dark_fraction"], "sharpness": frame_quality.sharpness(band)}
            quality["alignment"] = self.residual(key)
            quality["sync_ms"] = frame["sync_ms"]
            h_px = config.CAPTURE_HEIGHT
            return _num_tree({
                "frame_id": frame["id"], "age_s": time.time() - frame["t"],
                "size": [w, h], "mean_ndvi": float(finite.mean()) if finite.size else None,
                "veg_fraction": float((finite > s["veg_threshold"]).mean()) if finite.size else None,
                "valid_fraction": float(finite.size / ndvi.size),
                "zones": zones, "quality": quality,
                "mask_rows_px": [round(s["mask_start"] * h_px), round(s["mask_end"] * h_px), h_px],
                "model": self.model(k).describe(),
                "meta": {r: {m: _num(frame[r].metadata.get(m)) for m in ("ExposureTime", "AnalogueGain", "ColourGains", "Lux")}
                         for r in ("ir", "red")},
            })
        return self._cached(("stats", k, self.model_rev[k]), compute)

    def residual(self, key="ndvi"):
        """Remaining RED-vs-IR shift after the alignment model, in sensor pixels."""
        p = self.processed(key)
        if p is None:
            return None
        k = self.model_key(key)

        def compute():
            m = frame_quality.alignment_metrics(p["ir"], p["red"], p["valid"])
            scale = p["crop_ir"][2] / p["ir"].shape[1]
            return {"dx": m["shift_dx"] * scale, "dy": m["shift_dy"] * scale,
                    "shift": m["shift_px"] * scale, "spread": m["tile_spread_px"] * scale,
                    "confidence": m["confidence"], "tiles": m["valid_tiles"], "edge_ncc": m["edge_ncc"]}
        return self._cached(("residual", k, self.model_rev[k]), compute)

    def auto_offset(self, key="ndvi", samples=5, timeout=20.0):
        """Measures the residual on a few frames and adds it to the model offset."""
        k = self.model_key(key)
        recorded = self.input_mode == "recorded"
        if recorded:
            samples = 1
        found, fid, t_end = [], 0, time.time() + timeout
        while len(found) < samples and time.time() < t_end:
            frame = self.wait_frame(fid, timeout=3.0)
            if frame is None:
                continue
            fid = frame["id"]
            r = self.residual(k)
            if r and r["tiles"] >= 3 and r["confidence"] >= 0.2 and math.isfinite(r["dx"]):
                found.append((r["dx"], r["dy"]))
        if len(found) < (1 if recorded else max(2, samples // 2)):
            raise RuntimeError("not enough texture to measure the shift — point the cameras at the crop "
                               "(or a textured flat target) at the working distance")
        d = np.median(np.array(found), axis=0)
        off = np.array(self.model(k).offset_px) + d
        self.tune(k, offset_px=off.tolist())
        return {"measured": d.tolist(), "offset_px": off.tolist(), "samples": len(found)}

    def red_gain_from_panel(self, fraction=0.2):
        """RED gain that makes a grey reference panel in the image centre read NDVI 0."""
        p = self.processed("ndvi")
        if p is None:
            raise RuntimeError("no frame")
        h, w = p["ir"].shape
        ys, xs = slice(int(h * (0.5 - fraction / 2)), int(h * (0.5 + fraction / 2))), \
            slice(int(w * (0.5 - fraction / 2)), int(w * (0.5 + fraction / 2)))
        ir, red, valid = p["ir"][ys, xs], p["red"][ys, xs], p["valid"][ys, xs]
        if valid.mean() < 0.9 or red[valid].mean() < 0.05:
            raise RuntimeError("the centre of the image is too dark or not covered by both cameras")
        if max(ir[valid].max(), red[valid].max()) >= 0.98:
            raise RuntimeError("the panel is saturated — lower the exposure first")
        gain = float(ir[valid].mean() / red[valid].mean())
        self.settings.update({"red_gain": gain})
        return gain

    # ------------------------------------------------------------------ snapshots

    def require_live(self):
        if self.input_mode != "live":
            raise ValueError("This action requires live cameras; switch the input to Live cameras")

    def snapshot(self):
        self.require_live()
        cam_ir, cam_red = self.cam("ir"), self.cam("red")
        ir, red = camera_io.grab_pair(cam_ir, cam_red, timeout=3.0)
        if ir is None or red is None:
            raise RuntimeError("cameras busy")
        folder = os.path.join(self.data_dir, "snapshots")
        os.makedirs(folder, exist_ok=True)
        stem = time.strftime("snap_%Y%m%d-%H%M%S")
        cv2.imwrite(os.path.join(folder, f"{stem}_ir.png"), ir.bgr)
        cv2.imwrite(os.path.join(folder, f"{stem}_red.png"), red.bgr)
        meta = {"ir": {k: _num(v) for k, v in ir.metadata.items() if k in METADATA_KEYS},
                "red": {k: _num(v) for k, v in red.metadata.items() if k in METADATA_KEYS},
                "orientation": "sensor (CamUI flips undone)", "profile": self.model("ndvi").name,
                "settings": {k: v for k, v in self.settings.data.items() if k != "camera_extras"}}
        env = self.env.state()
        meta.update(location=env["location"], gps=env["gps"]["fix"], weather=env["weather"])
        with open(os.path.join(folder, f"{stem}.json"), "w") as f:
            json.dump(meta, f, indent=2)
        return os.path.join(folder, stem)

    # ------------------------------------------------------------------ live checkerboard

    def live_corners(self, board):
        frame = self.frame
        if frame is None:
            return None

        def compute():
            out = {"frame_id": frame["id"]}
            for role in ("ir", "red"):
                img = frame[f"{role}_s"]
                corners = calib_core.find_corners(img, board, fast=True)
                f = frame[role]
                if corners is None:
                    out[role] = None
                    continue
                pts = corners.reshape(-1, 2) / np.array([img.shape[1], img.shape[0]])
                out[role] = display_points(pts, f.hflip, f.vflip).round(4).tolist()
            return out
        return self._cached(("corners", board.cols, board.rows), compute)


def display_points(pts01, hflip, vflip):
    """Normalised sensor-orientation points → normalised display points."""
    pts = np.array(pts01, np.float64).copy()
    if hflip:
        pts[:, 0] = 1 - pts[:, 0]
    if vflip:
        pts[:, 1] = 1 - pts[:, 1]
    return pts


def _num_tree(obj):
    if isinstance(obj, dict):
        return {k: _num_tree(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_num_tree(v) for v in obj]
    return _num(obj)
