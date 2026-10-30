"""Recorded IR/RED datasets and asynchronous quality/export jobs."""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import tempfile
import threading
import zipfile

import cv2
import numpy as np

import alignment
import evaluate_frames
import frame_quality

from . import camera_io


class RecordedData:
    def __init__(self, service, repo):
        self.service = service
        self.root = Path(repo, "FieldData").resolve()
        self.lock = threading.RLock()
        self.source = None
        self.pairs = []
        self.index = 0
        self.rows = []
        self.signature = None
        self.evaluation = None
        self.job = {"running": False, "done": 0, "total": 0, "error": None, "export": None}

    def sources(self):
        if not self.root.is_dir():
            return []
        return [{"path": ".", "kind": "folder"}] + [
            {"path": p.relative_to(self.root).as_posix(),
             "kind": "folder" if p.is_dir() else "zip"}
            for p in sorted(self.root.rglob("*"))
            if not p.is_symlink() and "exports" not in p.relative_to(self.root).parts
            and (p.is_dir() or p.suffix.lower() == ".zip")
            and p.resolve().is_relative_to(self.root)
        ]

    def _idle(self):
        if self.job["running"]:
            raise ValueError("Wait for the current evaluation/export job to finish")

    def _signature(self):
        s = self.service
        return (s.settings["ir_band"], s.settings["red_band"], s.model_rev["ndvi"])

    def state(self):
        with self.lock:
            current = self.pairs[self.index] if self.pairs else None
            valid = self.signature == self._signature() and bool(self.rows)
            row = self.rows[self.index] if valid else {}
            return {
                "mode": self.service.input_mode, "source": self.source,
                "index": self.index, "total": len(self.pairs),
                "pair": {"session": current[0], "stem": current[1],
                         "files": {k: v.member for k, v in current[2].items()}} if current else None,
                "verdict": row.get("verdict"), "reasons": row.get("reasons"),
                "evaluated": valid,
                "good": sum(r["verdict"] == "good" for r in self.rows) if valid else 0,
                "bad": sum(r["verdict"] == "bad" for r in self.rows) if valid else 0,
                "job": dict(self.job),
            }

    def _discover(self, source):
        if not isinstance(source, str) or not source:
            raise ValueError("Select a FieldData folder or ZIP")
        path = (self.root / source).resolve()
        if (not path.is_relative_to(self.root)
                or "exports" in path.relative_to(self.root).parts):
            raise ValueError("Select a folder or ZIP inside FieldData (not exports)")
        pairs = {}

        def add(member, source_path, prefix=PurePosixPath(".")):
            p = PurePosixPath(member)
            if p.suffix.lower() not in evaluate_frames.IMAGE_EXTS:
                return
            parsed = evaluate_frames._camera_of(p.stem)
            if parsed is None:
                return
            stem, role = parsed
            session = str(prefix / p.parent)
            pairs.setdefault((session, stem), {}).setdefault(
                role, evaluate_frames.FrameRef(str(source_path), member))

        def add_zip(archive_path, prefix=PurePosixPath(".")):
            with zipfile.ZipFile(archive_path) as archive:
                for member in sorted(archive.namelist()):
                    p = PurePosixPath(member)
                    if not member.endswith("/") and not p.is_absolute() and ".." not in p.parts:
                        add(member, archive_path, prefix)

        if path.is_dir():
            for p in sorted(path.rglob("*")):
                if (p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(path)
                        and "exports" not in p.relative_to(path).parts):
                    relative = p.relative_to(path)
                    if p.suffix.lower() == ".zip":
                        add_zip(p, PurePosixPath(relative.parent.as_posix()))
                    else:
                        add(relative.as_posix(), path)
        elif path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
            add_zip(path)
        else:
            raise ValueError("Dataset is not a folder or valid ZIP archive")
        if not pairs:
            raise ValueError(f"No IR/RED captures found in {path} (including all subfolders)")
        return [(session, stem, refs) for (session, stem), refs in sorted(pairs.items())]

    @staticmethod
    def read_bytes(ref):
        if Path(ref.source).is_dir():
            path = (Path(ref.source) / ref.member).resolve()
            if not path.is_relative_to(Path(ref.source).resolve()):
                raise ValueError("Capture path is outside the dataset")
            return path.read_bytes()
        with zipfile.ZipFile(ref.source) as archive:
            return archive.read(ref.member)

    def read_frame(self, ref):
        data = self.read_bytes(ref)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Unreadable image: {ref.member}")
        # Recordings without sensor metadata are assumed to cover the full sensor.
        return camera_io.Frame(image, {"sha256": hashlib.sha256(data).hexdigest()},
                               tuple(alignment.full_crop()), False, False)

    def _frames(self, refs):
        if "ir" not in refs or "red" not in refs:
            raise ValueError("missing_" + ("red" if "ir" in refs else "ir"))
        ir, red = self.read_frame(refs["ir"]), self.read_frame(refs["red"])
        if ir.size != red.size:
            raise ValueError(f"Size mismatch: IR {ir.size} vs RED {red.size}")
        return ir, red

    def select(self, mode, source=None, index=0, *, reload=False):
        if mode not in ("live", "recorded"):
            raise ValueError("Input mode must be live or recorded")
        with self.service.lock, self.lock:
            self._idle()
            if mode == "live":
                self.service.input_mode = "live"
                self.service.frame = None
                self.service.status = ""
            else:
                rescan = reload or source != self.source
                pairs = self._discover(source) if rescan else self.pairs
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(pairs):
                    raise ValueError("Pair index is out of range")
                error = None
                try:
                    ir, red = self._frames(pairs[index][2])
                except (OSError, ValueError, cv2.error, zipfile.BadZipFile, KeyError) as exc:
                    error = str(exc)
                if rescan:
                    self.rows, self.signature = [], None
                    self.job = {"running": False, "done": 0, "total": 0, "error": None, "export": None}
                self.source, self.pairs, self.index = source, pairs, index
                self.service.input_mode = "recorded"
                if error is None:
                    self.service._publish(ir, red)
                    self.service.frame["sync_ms"] = None
                else:
                    self.service.frame = None
                self.service.status = f"Recorded: {source} | {pairs[index][1]}" + (f" | {error}" if error else "")
        return self.state()

    def start_job(self, export=False):
        with self.service.lock, self.lock:
            self._idle()
            if self.service.input_mode != "recorded" or not self.pairs:
                raise ValueError("Select a recorded dataset first")
            signature = self._signature()
            if export and (signature != self.signature or not self.rows):
                raise ValueError("Evaluate this dataset with the current bands/calibration before exporting")
            model = copy.deepcopy(self.service.model("ndvi"))
            bands = (self.service.settings["ir_band"], self.service.settings["red_band"])
            if not export:
                self.evaluation = {
                    "source": self.source, "bands": bands,
                    "model": copy.deepcopy(self.service.model_state("ndvi")),
                    "thresholds": vars(frame_quality.Thresholds()),
                    "orientation": "as stored; assumed full-sensor crop; no capture timestamps inferred",
                }
            self.job = {"running": True, "done": 0, "total": len(self.pairs),
                        "error": None, "export": None}
            threading.Thread(target=self._job, args=(export, signature, model, bands),
                             name="drop-recorded", daemon=True).start()
        return self.state()

    def _job(self, export, signature, model, bands):
        try:
            if export:
                self._export()
            else:
                self._evaluate(signature, model, bands)
        except Exception as exc:
            self.service.status = f"Recorded job failed: {exc}"
            with self.lock:
                self.job["error"] = str(exc)
            import traceback
            traceback.print_exc()
        finally:
            with self.lock:
                self.job["running"] = False

    def _evaluate(self, signature, model, bands):
        from .service import band_from

        rows = []
        for i, (session, stem, refs) in enumerate(self.pairs):
            row = {"session": session, "stem": stem,
                   **{f"{role}_file": ref.display for role, ref in refs.items()}}
            try:
                ir_frame, red_frame = self._frames(refs)
                row.update(ir_sha256=ir_frame.metadata["sha256"], red_sha256=red_frame.metadata["sha256"])
                ir, red = band_from(ir_frame.bgr, bands[0]), band_from(red_frame.bgr, bands[1])
                # Remap at analysis resolution, but report shifts in original capture pixels.
                ir_s, scale = frame_quality.to_analysis_scale(ir)
                red_s, _ = frame_quality.to_analysis_scale(red)
                aligned = model.warp_red(red_s, ir_s.shape, ir_frame.crop, red_frame.crop)
                coverage = model.warp_red(np.ones_like(red_s), ir_s.shape, ir_frame.crop, red_frame.crop)
                metrics = frame_quality.evaluate_bands(ir_s, red_s, aligned, coverage)
                for key in ("shift_dx", "shift_dy", "shift_px", "tile_spread_px"):
                    metrics[f"align_{key}"] /= scale
                metrics.update(height=ir.shape[0], width=ir.shape[1])
                row.update(metrics)
            except (OSError, ValueError, cv2.error, zipfile.BadZipFile, KeyError) as exc:
                row["error"] = str(exc)
            rows.append(row)
            with self.lock:
                self.job["done"] = i + 1
        frame_quality.classify(rows)
        with self.lock:
            self.rows, self.signature = rows, signature

    def _export(self):
        root = self.root / "exports"
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        folder = Path(tempfile.mkdtemp(prefix=f"good-{stamp}-", dir=root))
        # COMPLETE is written last; interrupted/failed exports are visibly incomplete.
        (folder / "INCOMPLETE").write_text("Export in progress\n", encoding="ascii")
        for i, (row, (_, _, refs)) in enumerate(zip(self.rows, self.pairs)):
            if row["verdict"] == "good":
                for role, ref in refs.items():
                    relative = PurePosixPath(row["session"]) / PurePosixPath(ref.member).name
                    target = folder / "good" / relative
                    if not target.resolve().is_relative_to(folder.resolve()):
                        raise ValueError("Export path is outside the output folder")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = self.read_bytes(ref)
                    if hashlib.sha256(data).hexdigest() != row[f"{role}_sha256"]:
                        raise ValueError(f"Capture changed since evaluation: {ref.member}; re-evaluate the dataset")
                    with target.open("xb") as output:
                        output.write(data)
            with self.lock:
                self.job["done"] = i + 1
        evaluate_frames.write_csv(str(folder / "report.csv"), self.rows)
        (folder / "evaluation.json").write_text(json.dumps(self.evaluation, indent=2), encoding="utf-8")
        (folder / "INCOMPLETE").unlink()
        (folder / "COMPLETE").touch()
        with self.lock:
            self.job["export"] = str(folder)
