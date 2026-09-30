"""
Guided calibration session (Calibration tab).

stereo : checkerboard pairs → calib_core.calibrate_stereo → stereo profile
         (works at any distance; RED is mapped onto IR through a plane at
         the crop distance you enter).
ecc    : one pair of a flat, textured scene at the crop distance → ECC
         homography profile (quick field fix, valid for that distance only).

Images are saved in <drop-rpi>/data/calibration_sessions/<name>/cam_0|cam_1/
(the layout calibration/calibrate_cameras.py reads), in sensor orientation.
"""
import json
import os
import shutil
import threading
import time

import cv2
import numpy as np

import alignment
import calib_core
import frame_quality

from . import camera_io, service as service_mod


class CalibrationSession:
    def __init__(self, svc, params):
        name = str(params.get("name") or "").strip()
        if not alignment.valid_name(name):
            raise ValueError("name: 1–64 letters, digits, '_', '.' or '-'")
        kind = params.get("kind", "stereo")
        if kind not in ("stereo", "ecc"):
            raise ValueError("kind must be 'stereo' or 'ecc'")
        self.svc = svc
        self.name = name
        self.kind = kind
        self.board = calib_core.Board(int(params.get("cols", 8)), int(params.get("rows", 6)),
                                      float(params.get("square_mm", 28)) / 1000.0)
        if self.board.cols < 3 or self.board.rows < 3 or self.board.square_m <= 0:
            raise ValueError("board: at least 3x3 inner corners and a positive square size")
        self.distance_m = float(params.get("distance_m", 5.0))
        if not 0.1 <= self.distance_m <= 500:
            raise ValueError("distance must be 0.1–500 m")
        pitch = params.get("pitch_deg")
        self.pitch_deg = None if pitch in (None, "") else float(pitch)
        self.target_pairs = int(params.get("target_pairs", 15)) if self.kind == "stereo" else 1
        self.source = params.get("source", "still")
        if self.source not in ("still", "stream"):
            raise ValueError("source must be 'still' or 'stream'")
        self.notes = str(params.get("notes", ""))[:2000]
        self.folder = os.path.join(svc.data_dir, "calibration_sessions",
                                   f"{name}_{time.strftime('%Y%m%d-%H%M%S')}")
        os.makedirs(os.path.join(self.folder, "cam_0"))
        os.makedirs(os.path.join(self.folder, "cam_1"))
        self.pairs = []
        self.next_idx = 1
        self.result = None
        self.level = None
        self.messages = []
        self.profile = None
        self.saved_as = None
        self.busy = threading.Lock()
        self.created = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(self.folder, "session.json"), "w") as f:
            json.dump(self.params(), f, indent=2)

    def params(self):
        return {"name": self.name, "kind": self.kind, "board": self.board.to_dict(),
                "distance_m": self.distance_m, "pitch_deg": self.pitch_deg,
                "target_pairs": self.target_pairs, "source": self.source, "notes": self.notes,
                "folder": self.folder, "created": self.created}

    # ------------------------------------------------------------------ capture

    def capture(self):
        if not self.busy.acquire(blocking=False):
            raise RuntimeError("busy")
        try:
            cam_ir, cam_red = self.svc.cam("ir"), self.svc.cam("red")
            if cam_ir is None or cam_red is None:
                raise RuntimeError("IR/RED camera not available")
            self.svc.pause()
            try:
                if self.source == "still":
                    ir, red = camera_io.capture_still_pair(cam_ir, cam_red)
                else:
                    ir, red = camera_io.grab_pair(cam_ir, cam_red, timeout=3.0)
                    if ir is None or red is None:
                        raise RuntimeError("cameras busy, try again")
            finally:
                self.svc.resume()

            if self.pairs:
                ref = self.pairs[0]
                if list(ir.size) != ref["size_ir"] or list(red.size) != ref["size_red"]:
                    raise RuntimeError(f"image size changed ({ir.size}/{red.size} vs "
                                       f"{ref['size_ir']}/{ref['size_red']}): keep the sensor mode "
                                       "fixed during a session or start a new one")
            pair = {"idx": self.next_idx, "size_ir": list(ir.size), "size_red": list(red.size),
                    "crop_ir": list(ir.crop), "crop_red": list(red.crop),
                    "flips": {"ir": [ir.hflip, ir.vflip], "red": [red.hflip, red.vflip]},
                    "sync_ms": camera_io.sync_ms(ir, red),
                    "exposure": {"ir": ir.metadata.get("ExposureTime"), "red": red.metadata.get("ExposureTime")}}

            if self.kind == "stereo":
                found = [None, None]

                def detect(i, img):
                    found[i] = calib_core.find_corners(img, self.board)
                threads = [threading.Thread(target=detect, args=(0, ir.bgr)),
                           threading.Thread(target=detect, args=(1, red.bgr))]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                missing = [n for n, c in zip(("IR", "RED"), found) if c is None]
                if missing:
                    return {"ok": False, "message": f"Checkerboard ({self.board.cols}x{self.board.rows} inner corners) "
                            f"not found in {' and '.join(missing)} — pair not kept. Make sure the whole "
                            "board is visible, sharp and evenly lit in both views."}
                pair["corners_ir"], pair["corners_red"] = found
                pair["outline"] = {
                    "ir": self._outline(found[0], ir), "red": self._outline(found[1], red)}
            else:
                pair["corners_ir"] = pair["corners_red"] = None
                pair["outline"] = None

            idx = self.next_idx
            cv2.imwrite(self.image_path("cam_0", idx), ir.bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            cv2.imwrite(self.image_path("cam_1", idx), red.bgr, [cv2.IMWRITE_JPEG_QUALITY, 95])
            pair["thumb"] = self._thumb(ir.bgr, red.bgr, pair)
            self.pairs.append(pair)
            self.next_idx += 1
            self.result = None
            return {"ok": True, "message": f"Pair #{idx} captured ({len(self.pairs)}/{self.target_pairs}).",
                    "idx": idx}
        finally:
            self.busy.release()

    def _outline(self, corners, frame):
        c = corners.reshape(self.board.rows, self.board.cols, 2)
        quad = np.array([c[0, 0], c[0, -1], c[-1, -1], c[-1, 0]], np.float64)
        quad /= np.array(frame.size, np.float64)
        return service_mod.display_points(quad, frame.hflip, frame.vflip).round(4).tolist()

    def _thumb(self, ir_bgr, red_bgr, pair, width=240):
        tiles = []
        for img, key in ((ir_bgr, "corners_ir"), (red_bgr, "corners_red")):
            s = width / img.shape[1]
            small = cv2.resize(img, (width, round(img.shape[0] * s)), interpolation=cv2.INTER_AREA)
            if pair.get(key) is not None:
                small = calib_core.draw_corners(small, pair[key] * s, self.board)
            tiles.append(small)
        if tiles[0].shape != tiles[1].shape:
            tiles[1] = cv2.resize(tiles[1], (tiles[0].shape[1], tiles[0].shape[0]))
        ok, buf = cv2.imencode(".jpg", np.hstack(tiles), [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes()

    def image_path(self, cam, idx):
        return os.path.join(self.folder, cam, f"{idx:02d}.jpg")

    def delete(self, idx):
        for p in list(self.pairs):
            if p["idx"] == idx:
                self.pairs.remove(p)
                for cam in ("cam_0", "cam_1"):
                    path = self.image_path(cam, idx)
                    if os.path.exists(path):
                        os.remove(path)
                self.result = None
                return True
        return False

    def thumb(self, idx):
        for p in self.pairs:
            if p["idx"] == idx:
                return p["thumb"]
        return None

    # ------------------------------------------------------------------ run / save

    def run(self):
        if not self.pairs:
            raise RuntimeError("capture at least one pair first")
        ref = self.pairs[0]
        extra = {"flips": ref["flips"], "session_dir": self.folder,
                 "capture": {"source": self.source, "image_size_ir": ref["size_ir"],
                             "image_size_red": ref["size_red"]}}
        if self.kind == "stereo":
            if ref["size_ir"] != ref["size_red"]:
                raise RuntimeError("stereo calibration needs both cameras at the same resolution — "
                                   "use the same sensor mode on both")
            if len(self.pairs) < calib_core.MIN_PAIRS:
                raise RuntimeError(f"need at least {calib_core.MIN_PAIRS} pairs "
                                   f"({calib_core.RECOMMENDED_PAIRS}+ recommended)")
            res = calib_core.calibrate_stereo([p["corners_ir"] for p in self.pairs],
                                              [p["corners_red"] for p in self.pairs],
                                              tuple(ref["size_ir"]), self.board)
            level, msgs = calib_core.assess(res)
            crops = {tuple(p["crop_ir"]) for p in self.pairs} | {tuple(p["crop_red"]) for p in self.pairs}
            if len({tuple(p["crop_ir"]) for p in self.pairs}) > 1 or len({tuple(p["crop_red"]) for p in self.pairs}) > 1:
                msgs.append(f"ScalerCrop changed between pairs ({len(crops)} different) — results may be off.")
            profile = alignment.make_stereo_profile(
                self.name, res, self.board.to_dict(), ref["crop_ir"], ref["crop_red"],
                self.distance_m, self.pitch_deg, notes=self.notes,
                quality={"level": level, "messages": msgs}, extra=extra)
            summary = {"rms_stereo": res["rms_stereo"], "rms_ir": res["rms_ir"], "rms_red": res["rms_red"],
                       "pair_errors": [{"idx": p["idx"], "error": e, "distance_m": d}
                                       for p, e, d in zip(self.pairs, res["pair_errors"], res["board_distance_m"])],
                       "coverage_ir": res["coverage_ir"], "coverage_red": res["coverage_red"],
                       "baseline_cm": float(np.linalg.norm(res["T"])) * 100,
                       "rotation_deg": calib_core.rotation_angle_deg(res["R"])}
        else:
            p = self.pairs[-1]
            ir = cv2.imread(self.image_path("cam_0", p["idx"]))
            red = cv2.imread(self.image_path("cam_1", p["idx"]))
            if ir.shape != red.shape:
                red = cv2.resize(red, (ir.shape[1], ir.shape[0]), interpolation=cv2.INTER_AREA)
            s = self.svc.settings
            ir8 = (service_mod.band_from(ir, s["ir_band"]) * 255).astype(np.uint8)
            red8 = (service_mod.band_from(red, s["red_band"]) * 255).astype(np.uint8)
            H, corr = alignment.estimate_homography(ir8, red8)
            # judge by the shift that remains after warping (image px of the stored pair)
            size = (ir8.shape[1], ir8.shape[0])
            red_al = cv2.warpPerspective(red8, H, size, flags=cv2.WARP_INVERSE_MAP | cv2.INTER_LINEAR)
            work = (820, max(1, round(820 * size[1] / size[0])))
            m = frame_quality.alignment_metrics(cv2.resize(ir8, work, interpolation=cv2.INTER_AREA),
                                                cv2.resize(red_al, work, interpolation=cv2.INTER_AREA), None)
            k = size[0] / work[0]
            shift, spread = m["shift_px"] * k, m["tile_spread_px"] * k
            if m["valid_tiles"] < 4 or not np.isfinite(shift):
                level, verdict = "poor", "not enough texture to verify: use a sharp, textured flat target"
            elif shift <= 1.5 and spread <= 3:
                level, verdict = "good", "good"
            elif shift <= 4 and spread <= 8:
                level, verdict = "ok", "acceptable"
            else:
                level, verdict = "poor", "poor: use a sharper, more textured flat target that fills both views"
            msgs = [f"Residual after warp on pair #{p['idx']}: {shift:.2f} px, spread across the image "
                    f"{spread:.2f} px ({verdict}). ECC correlation {corr:.2f}.",
                    f"Valid for targets at ~{self.distance_m:g} m only (no distortion / parallax model)."]
            profile = alignment.make_homography_profile(
                self.name, H, (ir.shape[1], ir.shape[0]), p["crop_ir"], p["crop_red"], notes=self.notes,
                quality={"level": level, "messages": msgs}, extra=dict(extra, distance_m=self.distance_m))
            summary = {"correlation": corr, "pair": p["idx"], "residual_px": shift, "spread_px": spread}
        self.result = summary
        self.level, self.messages = level, msgs
        self.profile = profile
        self.svc.set_model("calib", alignment.AlignmentModel(profile))
        return self.status()

    def save(self, name=None, overwrite=False):
        if self.profile is None:
            raise RuntimeError("run the calibration first")
        name = (name or self.name).strip()
        if not alignment.valid_name(name):
            raise ValueError("invalid profile name")
        profile = json.loads(json.dumps(alignment._jsonable(self.profile)))
        tuned = self.svc.model("calib").profile if self.svc.models.get("calib") else None
        if tuned:
            profile["plane"], profile["offset_px"] = tuned.get("plane"), tuned.get("offset_px")
        profile["name"] = name
        folder = alignment.save_profile(profile, overwrite=overwrite)
        self.saved_as = name
        return folder

    def discard(self, delete_images=False):
        self.svc.set_model("calib", None)
        if delete_images and os.path.isdir(self.folder):
            shutil.rmtree(self.folder)

    # ------------------------------------------------------------------ status

    def status(self):
        return {"params": self.params(),
                "pairs": [{"idx": p["idx"], "outline": p["outline"], "sync_ms": p["sync_ms"],
                           "exposure": p["exposure"], "size_ir": p["size_ir"], "size_red": p["size_red"],
                           "crop_ir": p["crop_ir"], "crop_red": p["crop_red"]} for p in self.pairs],
                "result": service_mod._num_tree(self.result) if self.result else None,
                "level": self.level, "messages": self.messages, "saved_as": self.saved_as,
                "busy": self.busy.locked()}
