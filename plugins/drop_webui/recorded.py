"""Recorded IR/RED datasets and asynchronous quality/export jobs."""
import copy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath
import tempfile
import threading
import zipfile
import warnings
from itertools import groupby

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
        self.recursive = True
        self.crop_mode = service.settings["recorded_crop"]
        self.scan_warnings = []
        self.pairs = []
        self.index = 0
        self.rows = []
        self.signature = None
        self.evaluation = None
        self.job = {"running": False, "done": 0, "total": 0, "error": None, "export": None}

    def sources(self):
        self.scan_warnings = []
        if not self.root.is_dir():
            return []
        return [{"path": ".", "kind": "folder"}] + [
            {"path": p.relative_to(self.root).as_posix(),
             "kind": "folder" if p.is_dir() else "zip"}
            for p in self._walk(self.root)
            if p.is_dir() or p.suffix.lower() == ".zip"
        ]

    def _walk(self, folder, ancestors=frozenset(), recursive=True):
        """Follow dataset links, but never recurse into exports or a directory loop."""
        resolved = folder.resolve()
        if resolved in ancestors:
            return
        ancestors = ancestors | {resolved}
        try:
            paths = sorted(folder.iterdir())
        except PermissionError as exc:
            message = f"Cannot scan {folder}: {exc}"
            self.scan_warnings.append(message)
            warnings.warn(message, RuntimeWarning)
            return
        for path in paths:
            if path.name in ("exports", "quarantine"):
                continue
            if path.is_dir():
                if path.resolve() not in ancestors:
                    yield path
                    if recursive:
                        yield from self._walk(path, ancestors)
            elif path.is_file():
                yield path

    def _idle(self):
        if self.job["running"]:
            raise ValueError("Wait for the current evaluation/export job to finish")

    def _signature(self):
        s = self.service
        return (s.settings["ir_band"], s.settings["red_band"], s.model_rev["ndvi"], self.crop_mode)

    def state(self):
        with self.lock:
            current = self.pairs[self.index] if self.pairs else None
            valid = self.signature == self._signature() and bool(self.rows)
            row = self.rows[self.index] if valid else {}
            reasons = Counter(reason for r in self.rows for reason in r["reasons"].split(";") if reason) if valid else {}
            completed = sum(r["verdict"] in ("good", "bad") for r in self.rows) if valid else 0
            return {
                "mode": self.service.input_mode, "source": self.source,
                "recursive": self.recursive,
                "crop_mode": self.crop_mode, "reason_counts": dict(reasons),
                "evaluation": self.evaluation if valid else None,
                "index": self.index, "total": len(self.pairs),
                "pair": {"session": current[0], "stem": current[1],
                         "files": {k: v.member for k, v in current[2].items()}} if current else None,
                "verdict": row.get("verdict"), "reasons": row.get("reasons"),
                "evaluated": valid and completed == len(self.pairs),
                "completed": completed,
                "good": sum(r["verdict"] == "good" for r in self.rows) if valid else 0,
                "bad": sum(r["verdict"] == "bad" for r in self.rows) if valid else 0,
                "job": dict(self.job),
            }

    def _discover(self, source, recursive=True):
        self.scan_warnings = []
        if not isinstance(source, str) or not source:
            raise ValueError("Select a FieldData folder or ZIP")
        # Constrain the requested name, not the symlink target: links placed
        # inside FieldData intentionally make external capture storage available.
        path = Path(os.path.abspath(self.root / source))
        if (not path.is_relative_to(self.root)
                or any(p in ("exports", "quarantine") for p in path.relative_to(self.root).parts)):
            raise ValueError("Select a folder or ZIP inside FieldData (not exports or quarantine)")
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
                    if (not member.endswith("/") and not p.is_absolute() and ".." not in p.parts
                            and (recursive or len(p.parts) == 1)):
                        add(member, archive_path, prefix)

        if path.is_dir():
            for p in self._walk(path, recursive=recursive):
                if p.is_file():
                    relative = p.relative_to(path)
                    if p.suffix.lower() == ".zip":
                        add_zip(p, PurePosixPath(relative.parent.as_posix()))
                    else:
                        add(p.name, p.parent.resolve(), PurePosixPath(relative.parent.as_posix()))
        elif path.suffix.lower() == ".zip" and zipfile.is_zipfile(path):
            add_zip(path)
        else:
            raise ValueError("Dataset is not a folder or valid ZIP archive")
        if not pairs:
            scope = "including all subfolders" if recursive else "selected folder only"
            raise ValueError(f"No IR/RED captures found in {path} ({scope})")
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

    def read_frame(self, ref, *, data=None):
        if data is None:
            data = self.read_bytes(ref)
        image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError(f"Unreadable image: {ref.member}")
        # Recordings without sensor metadata are assumed to cover the full sensor.
        return camera_io.Frame(image, {"sha256": hashlib.sha256(data).hexdigest()},
                               tuple(alignment.full_crop()), False, False)

    def _frames(self, refs, data=None):
        if "ir" not in refs or "red" not in refs:
            raise ValueError("missing_" + ("red" if "ir" in refs else "ir"))
        ir, red = (self.read_frame(refs[role], data=data[role] if data is not None else None)
                   for role in ("ir", "red"))
        if ir.size != red.size:
            raise ValueError(f"Size mismatch: IR {ir.size} vs RED {red.size}")
        return ir, red

    def crop(self, role, model):
        if self.crop_mode == "profile":
            crop = (model.profile or {}).get(f"crop_{role}")
            if crop is None:
                raise ValueError("The active calibration has no capture crop; choose Full sensor or load a profile")
            return tuple(crop)
        return tuple(alignment.full_crop())

    def select(self, mode, source=None, index=0, *, reload=False, recursive=None, crop_mode=None):
        if mode not in ("live", "recorded"):
            raise ValueError("Input mode must be live or recorded")
        with self.service.lock, self.lock:
            browsing = (mode == "recorded" and self.service.input_mode == "recorded"
                        and source == self.source and not reload
                        and recursive in (None, self.recursive) and crop_mode in (None, self.crop_mode))
            if not browsing:
                self._idle()
            if mode == "live":
                self.service.input_mode = "live"
                self.service.frame = None
                self.service.status = ""
            else:
                crop_mode = self.crop_mode if crop_mode is None else crop_mode
                if crop_mode not in ("full", "profile"):
                    raise ValueError("Recorded crop must be full or profile")
                if crop_mode == "profile" and any(
                        not (self.service.model("ndvi").profile or {}).get(f"crop_{role}")
                        for role in ("ir", "red")):
                    raise ValueError("Load a calibration profile with capture crops first")
                recursive = self.recursive if recursive is None else recursive
                if not isinstance(recursive, bool):
                    raise ValueError("Include subdirectories must be true or false")
                rescan = reload or source != self.source or recursive != self.recursive
                pairs = self._discover(source, recursive) if rescan else self.pairs
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
                if crop_mode != self.crop_mode:
                    self.rows, self.signature = [], None
                self.crop_mode = crop_mode
                if self.service.settings["recorded_crop"] != crop_mode:
                    self.service.settings.set("recorded_crop", crop_mode)
                self.recursive = recursive
                self.service.input_mode = "recorded"
                if error is None:
                    for role, frame in (("ir", ir), ("red", red)):
                        frame.crop = self.crop(role, self.service.model("ndvi"))
                    self.service._publish(ir, red)
                    self.service.frame["sync_ms"] = None
                else:
                    self.service.frame = None
                self.service.status = f"Recorded: {source} | {pairs[index][1]}" + (f" | {error}" if error else "")
        return self.state()

    def upload(self, files):
        if not files:
            raise ValueError("Select a ZIP archive or folder to upload")
        self.root.mkdir(parents=True, exist_ok=True)
        # Stage outside FieldData so incomplete uploads cannot appear as datasets.
        with tempfile.TemporaryDirectory(prefix="drop-upload-", dir=self.root.parent) as temp:
            staging = Path(temp)
            total = 0
            seen = set()
            for item in files:
                name = item.filename or ""
                relative = PurePosixPath(name)
                if (not name or "\\" in name or relative.is_absolute() or ".." in relative.parts
                        or any(p in ("exports", "quarantine") for p in relative.parts) or relative in seen):
                    raise ValueError(f"Invalid or duplicate upload path: {name}")
                if relative.suffix.lower() not in (*evaluate_frames.IMAGE_EXTS, ".zip", ".json", ".csv", ".txt"):
                    raise ValueError(f"Unsupported capture file: {name}")
                seen.add(relative)
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as output:
                    while chunk := item.stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > 2 * 1024 ** 3:
                            raise ValueError("Upload exceeds the 2 GiB limit; upload smaller batches")
                        output.write(chunk)
                if relative.suffix.lower() == ".zip":
                    if not zipfile.is_zipfile(target):
                        raise ValueError(f"Invalid ZIP archive: {name}")
                    with zipfile.ZipFile(target) as archive:
                        if any(PurePosixPath(m).is_absolute() or ".." in PurePosixPath(m).parts
                               or "\\" in m for m in archive.namelist()):
                            raise ValueError(f"Unsafe paths in ZIP archive: {name}")
            destination = Path(tempfile.mkdtemp(prefix="upload-", dir=self.root))
            try:
                for path in staging.iterdir():
                    shutil.move(str(path), str(destination / path.name))
            except Exception:
                shutil.rmtree(destination)
                raise
        return destination.relative_to(self.root).as_posix()

    def start_job(self, export=False):
        with self.service.lock, self.lock:
            self._idle()
            if self.service.input_mode != "recorded" or not self.pairs:
                raise ValueError("Select a recorded dataset first")
            signature = self._signature()
            if export and (signature != self.signature or not self.rows
                           or any(r["verdict"] not in ("good", "bad") for r in self.rows)):
                raise ValueError("Evaluate this dataset with the current bands/calibration before exporting")
            model = copy.deepcopy(self.service.model("ndvi"))
            bands = (self.service.settings["ir_band"], self.service.settings["red_band"])
            if not export:
                self.evaluation = {
                    "source": self.source, "bands": bands,
                    "recursive": self.recursive,
                    "crop_mode": self.crop_mode,
                    "crops": {role: self.crop(role, model) for role in ("ir", "red")},
                    "model": copy.deepcopy(self.service.model_state("ndvi")),
                    "thresholds": vars(frame_quality.Thresholds()),
                    "orientation": "as stored; explicit recorded crop; no capture timestamps inferred",
                }
                self.rows = [{"session": session, "stem": stem, "verdict": "pending", "reasons": ""}
                             for session, stem, _ in self.pairs]
                self.signature = signature
            self.job = {"running": True, "done": 0, "total": len(self.pairs),
                        "error": None, "export": None, "kind": "export" if export else "evaluate",
                        "folders_done": 0, "folders_total": len({p[0] for p in self.pairs}),
                        "folder": None}
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
        for session, items in groupby(enumerate(self.pairs), key=lambda item: item[1][0]):
            items = list(items)
            rows = []
            with self.lock:
                self.job["folder"] = session
            for i, (_, stem, refs) in items:
                rows.append(self._score(session, stem, refs, model, bands))
                with self.lock:
                    self.job["done"] += 1
            frame_quality.classify(rows)
            with self.lock:
                for (i, _), row in zip(items, rows):
                    self.rows[i] = row
                self.signature = signature
                self.job["folders_done"] += 1

    def _score(self, session, stem, refs, model, bands):
        from .service import band_from

        row = {"session": session, "stem": stem,
               **{f"{role}_file": ref.display for role, ref in refs.items()}}
        try:
            # Keep checksums even for incomplete or undecodable pairs so cleanup
            # can quarantine only the exact files that were evaluated.
            data = {role: self.read_bytes(ref) for role, ref in refs.items()}
            for role, content in data.items():
                row[f"{role}_sha256"] = hashlib.sha256(content).hexdigest()
            ir_frame, red_frame = self._frames(refs, data)
            ir, red = band_from(ir_frame.bgr, bands[0]), band_from(red_frame.bgr, bands[1])
            crop_ir, crop_red = self.crop("ir", model), self.crop("red", model)
            aligned = model.warp_red(red, ir.shape, crop_ir, crop_red)
            coverage = model.warp_red(np.ones_like(red), ir.shape, crop_ir, crop_red)
            row.update(frame_quality.evaluate_bands(ir, red, aligned, coverage))
        except (OSError, ValueError, cv2.error, zipfile.BadZipFile, KeyError) as exc:
            row["error"] = str(exc)
        return row

    def start_cleanup(self):
        with self.service.lock, self.lock:
            self._idle()
            state = self.state()
            if self.service.input_mode != "recorded" or not state["evaluated"] or not state["bad"]:
                raise ValueError("Finish evaluation with the current settings before cleaning up bad captures")
            self.job = {"running": True, "done": 0, "total": len(self.pairs),
                        "error": None, "export": None, "kind": "cleanup"}
            threading.Thread(target=self._cleanup_job, name="drop-cleanup", daemon=True).start()
        return self.state()

    def _cleanup_job(self):
        try:
            root = self.root / "quarantine"
            root.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix="bad-", dir=root))
            manifest = {"evaluation": self.evaluation, "files": [], "skipped_pairs": 0,
                        "complete": False}
            manifest_path = folder / "manifest.json"
            def save_manifest():
                temp = folder / "manifest.json.tmp"
                temp.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                temp.replace(manifest_path)
            save_manifest()
            for i, (row, (_, _, refs)) in enumerate(zip(self.rows, self.pairs)):
                if row["verdict"] == "bad" and not row.get("quarantined"):
                    if any(not Path(ref.source).is_dir() or not row.get(f"{role}_sha256")
                           for role, ref in refs.items()):
                        manifest["skipped_pairs"] += 1
                    else:
                        planned = []
                        for role, ref in refs.items():
                            source = (Path(ref.source) / ref.member).resolve()
                            if not source.is_relative_to(Path(ref.source).resolve()):
                                raise ValueError("Capture path is outside the dataset")
                            checksum = row[f"{role}_sha256"]
                            if hashlib.sha256(source.read_bytes()).hexdigest() != checksum:
                                raise ValueError(f"Capture changed since evaluation: {ref.member}; re-evaluate")
                            target = folder / row["session"] / Path(ref.member).name
                            if not target.resolve().is_relative_to(folder.resolve()):
                                raise ValueError("Invalid quarantine path")
                            if target.exists():
                                raise FileExistsError(f"Quarantine target already exists: {target}")
                            planned.append({"source": str(source), "destination": str(target),
                                            "sha256": checksum, "moved": False})
                        manifest["files"].extend(planned)
                        save_manifest()
                        for entry in planned:
                            target = Path(entry["destination"])
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(entry["source"], entry["destination"])
                            entry["moved"] = True
                            save_manifest()
                        with self.lock:
                            row["quarantined"] = True
                with self.lock:
                    self.job["done"] = i + 1
            manifest["complete"] = True
            save_manifest()
            with self.lock:
                self.job["quarantine"] = str(folder)
                self.job["moved"] = sum(f["moved"] for f in manifest["files"])
                self.job["skipped_pairs"] = manifest["skipped_pairs"]
        except Exception as exc:
            with self.lock:
                self.job["error"] = str(exc)
            import traceback
            traceback.print_exc()
        finally:
            with self.lock:
                self.job["running"] = False

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
