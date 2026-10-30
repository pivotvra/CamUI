import csv
import io
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

import cv2
import numpy as np
from flask import Flask
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader

CAMUI = Path(__file__).resolve().parents[1]
REPO = CAMUI.parent
sys.path[:0] = [str(CAMUI / "plugins"), str(REPO / "Claude_xaris"), str(REPO / "calibration")]

import frame_quality
from drop_webui.routes import create_blueprint
from drop_webui.service import DropService


class RecordedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.field = self.repo / "FieldData"
        self.dataset = self.field / "session"
        self.dataset.mkdir(parents=True)
        rng = np.random.default_rng(42)
        gray = rng.integers(30, 200, (240, 320), dtype=np.uint8)
        self.image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        self.originals = {}
        for i in range(5):
            self.pair(f"good_{i}", self.image)
        self.pair("bad_dark", np.zeros_like(self.image))
        (self.dataset / "missing_ir.png").write_bytes(self.png(self.image))
        (self.dataset / "corrupt_ir.png").write_bytes(b"not an image")
        (self.dataset / "corrupt_red.png").write_bytes(b"not an image")
        self.svc = DropService({}, str(self.repo / "data"), str(self.repo))
        self.svc.touch = lambda: None
        self.recorded = self.svc.recorded
        self.app = Flask(__name__)
        self.app.register_blueprint(create_blueprint(self.svc))
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(CAMUI / "plugins/drop_webui/templates")),
        ])
        self.client = self.app.test_client()

    @staticmethod
    def png(image):
        ok, data = cv2.imencode(".png", image)
        assert ok
        return data.tobytes()

    def pair(self, stem, image, folder=None):
        folder = folder or self.dataset
        folder.mkdir(parents=True, exist_ok=True)
        for role in ("ir", "red"):
            path = folder / f"{stem}_{role}.png"
            data = self.png(image)
            path.write_bytes(data)
            self.originals[path.relative_to(self.dataset).as_posix()] = data

    def post(self, endpoint, data=None, status=200):
        response = self.client.post(f"/drop/api/ndvi/{endpoint}", json=data or {})
        self.assertEqual(response.status_code, status, response.get_json())
        return response.get_json()

    def load(self, source="session", index=2):
        return self.post("input", {"mode": "recorded", "source": source, "index": index})["input"]

    def finish_job(self):
        deadline = time.monotonic() + 15
        while self.recorded.state()["job"]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        state = self.recorded.state()
        self.assertFalse(state["job"]["running"])
        self.assertIsNone(state["job"]["error"])
        return state

    def test_quality_export_originals_and_report(self):
        self.load()
        self.post("evaluate")
        state = self.finish_job()
        self.assertEqual((state["good"], state["bad"]), (5, 3))
        self.post("export")
        state = self.finish_job()
        folder = Path(state["job"]["export"])
        self.assertTrue((folder / "COMPLETE").is_file())
        self.assertFalse((folder / "INCOMPLETE").exists())
        files = list((folder / "good").rglob("*.png"))
        self.assertEqual(len(files), 10)
        for path in files:
            relative = path.relative_to(folder / "good").as_posix()
            self.assertEqual(path.read_bytes(), self.originals[relative])
        rows = list(csv.DictReader(io.StringIO((folder / "report.csv").read_text())))
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(r["verdict"] == "good" for r in rows), 5)
        reasons = {r["stem"]: r["reasons"] for r in rows}
        self.assertEqual(reasons["missing"], "missing_red")
        self.assertIn("Unreadable image", reasons["corrupt"])
        self.assertIn("underexposed", reasons["bad_dark"])
        self.assertTrue((folder / "evaluation.json").is_file())
        sources = self.recorded.sources()
        self.assertFalse(any("exports" in s["path"] for s in sources))
        self.assertEqual((self.dataset / "good_0_ir.png").read_bytes(), self.originals["good_0_ir.png"])

    def test_zip_nested_sessions_and_camera_index_names(self):
        archive = self.field / "captures.ZIP"
        data = self.png(self.image)
        with zipfile.ZipFile(archive, "w") as z:
            for session in ("a/same", "b/same"):
                z.writestr(f"{session}/capture_0_raw.jpeg", data)
                z.writestr(f"{session}/capture_1_raw.jpeg", data)
            z.writestr("../outside_ir.png", data)
        state = self.load("captures.ZIP", 0)
        self.assertEqual(state["total"], 2)
        self.post("evaluate")
        self.assertEqual(self.finish_job()["good"], 2)
        self.post("export")
        folder = Path(self.finish_job()["job"]["export"]) / "good"
        for session in ("a/same", "b/same"):
            self.assertEqual((folder / session / "capture_0_raw.jpeg").read_bytes(), data)
            self.assertEqual((folder / session / "capture_1_raw.jpeg").read_bytes(), data)
        self.assertEqual(len(list(folder.rglob("*jpeg"))), 4)

    def test_browsing_stats_rendering_and_invalidation(self):
        self.load()
        first_id = self.svc.frame_id
        for view in ("ir", "red", "ndvi", "overlay"):
            response = self.client.get(f"/drop/frame/{view}.jpg")
            self.assertEqual(response.status_code, 200)
            self.assertIsNotNone(cv2.imdecode(np.frombuffer(response.data, np.uint8), cv2.IMREAD_COLOR))
        response = self.client.get("/drop/api/ndvi/stats")
        self.assertTrue(response.get_json()["ok"])
        self.assertIsNone(response.get_json()["stats"]["quality"]["sync_ms"])
        metadata = self.client.get("/drop/api/camera/metadata").get_json()
        self.assertEqual(metadata["input_mode"], "recorded")
        self.assertEqual(metadata["ir"], {})
        state = self.load(index=3)
        self.assertEqual(state["pair"]["stem"], "good_1")
        self.assertGreater(self.svc.frame_id, first_id)
        self.post("evaluate")
        self.assertTrue(self.finish_job()["evaluated"])
        self.svc.settings.update({"ir_band": "g"})
        self.assertFalse(self.recorded.state()["evaluated"])
        self.post("export", status=400)
        old_id = self.svc.frame_id
        self.svc.fresh()
        self.assertGreater(self.svc.frame_id, old_id)
        self.post("evaluate")
        self.assertTrue(self.finish_job()["evaluated"])
        self.svc.tune("ndvi", offset_px=[1, 0])
        self.assertFalse(self.recorded.state()["evaluated"])

    def test_bad_pair_does_not_prevent_browsing(self):
        self.load(index=1)
        self.assertIsNone(self.svc.frame)
        self.assertIn("Unreadable image", self.svc.status)
        self.load(index=2)
        self.assertIsNotNone(self.svc.frame)
        self.load(index=7)
        self.assertIsNone(self.svc.frame)
        self.assertIn("missing_red", self.svc.status)

    def test_invalid_inputs_and_live_restoration(self):
        self.load()
        for source in ("../", "/tmp", "exports", "", None):
            self.post("input", {"mode": "recorded", "source": source}, status=400)
        for index in (-1, 8, True, 0.5):
            self.post("input", {"mode": "recorded", "source": "session", "index": index}, status=400)
        self.post("snapshot", status=400)
        self.post("input", {"mode": "live"})
        self.assertEqual(self.svc.input_mode, "live")
        self.assertIsNone(self.svc.frame)
        with patch("drop_webui.camera_io.grab_pair", return_value=(None, None)):
            self.assertEqual(self.post("snapshot", status=500)["message"], "cameras busy")

    def test_job_blocks_dataset_changes_and_errors_are_visible(self):
        self.load()
        self.recorded.job["running"] = True
        self.post("input", {"mode": "live"}, status=400)
        self.post("evaluate", status=400)
        self.recorded.job["running"] = False
        self.post("evaluate")
        self.finish_job()
        with patch.object(self.recorded, "read_bytes", side_effect=OSError("disk failure")):
            self.post("export")
            deadline = time.monotonic() + 10
            while self.recorded.state()["job"]["running"] and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn("disk failure", self.recorded.state()["job"]["error"])
        self.assertFalse(list((self.field / "exports").rglob("COMPLETE")))
        self.assertTrue(list((self.field / "exports").rglob("INCOMPLETE")))

    def test_ndvi_page_contains_input_controls(self):
        response = self.client.get("/drop/ndvi")
        self.assertEqual(response.status_code, 200)
        for label in (b"FieldData folder / ZIP", b"Evaluate dataset", b"Export good captures"):
            self.assertIn(label, response.data)

    def test_shared_band_evaluator_preserves_default_metrics(self):
        import ndvi

        ir = ndvi.normalise(ndvi.extract_band(self.image, "IR"))
        red = ndvi.normalise(ndvi.extract_band(self.image, "RED"))
        expected = frame_quality.evaluate_bands(ir, red)
        actual = frame_quality.evaluate_pair(self.image, self.image)
        self.assertEqual(expected.keys(), actual.keys())
        for key in expected:
            np.testing.assert_equal(actual[key], expected[key], err_msg=key)

    def test_session_relative_blur_is_rejected(self):
        blurred = cv2.GaussianBlur(self.image, (0, 0), 4)
        self.pair("soft", blurred)
        self.load()
        self.post("evaluate")
        self.finish_job()
        soft = next(r for r in self.recorded.rows if r["stem"] == "soft")
        self.assertIn("ir_blurry", soft["reasons"])
        self.assertIn("red_blurry", soft["reasons"])
        self.assertLess(soft["ir_relative_sharpness"], frame_quality.Thresholds().min_relative_sharpness)

    def test_changed_files_cannot_be_exported_as_evaluated(self):
        self.load()
        self.post("evaluate")
        self.finish_job()
        (self.dataset / "good_0_ir.png").write_bytes(self.png(np.zeros_like(self.image)))
        self.post("export")
        deadline = time.monotonic() + 10
        while self.recorded.state()["job"]["running"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("Capture changed since evaluation", self.recorded.state()["job"]["error"])
        self.assertFalse(list((self.field / "exports").rglob("COMPLETE")))

    def test_live_worker_still_publishes_pairs(self):
        self.svc.last_client = time.time()
        ir = self.recorded.read_frame(next(iter(self.recorded._discover("session")[2][2].values())))
        red = self.recorded.read_frame(next(iter(self.recorded._discover("session")[2][2].values())))
        def finish(_):
            self.svc.last_client = 0
        with patch.object(self.svc, "cam", return_value=object()), \
                patch("drop_webui.camera_io.grab_pair", return_value=(ir, red)) as grab, \
                patch("drop_webui.service.time.sleep", side_effect=finish):
            self.svc._run()
        grab.assert_called_once()
        self.assertIsNotNone(self.svc.frame)
        self.assertEqual(self.svc.input_mode, "live")

    def test_recorded_processing_width_can_be_changed(self):
        larger = cv2.resize(self.image, (960, 720))
        self.pair("wide", larger)
        pairs = self.recorded._discover("session")
        index = next(i for i, p in enumerate(pairs) if p[1] == "wide")
        self.load(index=index)
        self.assertEqual(self.svc.frame["ir_s"].shape[1], 820)
        self.post("settings", {"proc_width": 640})
        self.assertEqual(self.svc.frame["ir_s"].shape[1], 640)

    def test_folder_combines_split_zip_archives(self):
        folder = self.field / "split"
        folder.mkdir()
        data = self.png(self.image)
        for role in ("ir", "red"):
            with zipfile.ZipFile(folder / f"{role}.zip", "w") as archive:
                archive.writestr(f"session/capture_{role}.png", data)
        state = self.load("split", 0)
        self.assertEqual(state["total"], 1)
        self.post("evaluate")
        self.assertEqual(self.finish_job()["good"], 1)
        self.post("export")
        output = Path(self.finish_job()["job"]["export"])
        for role in ("ir", "red"):
            self.assertEqual((output / "good/session" / f"capture_{role}.png").read_bytes(), data)
        self.assertEqual(self.load(".", 0)["total"], 9)


if __name__ == "__main__":
    unittest.main()
