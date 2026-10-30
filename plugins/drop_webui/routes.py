"""HTTP routes of the drop-rpi CamUI plugin (Blueprint mounted at /drop)."""
import time

from flask import Blueprint, Response, abort, jsonify, render_template, request

import alignment
import calib_core

from . import render
from .calib_session import CalibrationSession


def create_blueprint(svc):
    bp = Blueprint("drop", __name__, url_prefix="/drop",
                   template_folder="templates", static_folder="static", static_url_path="/static")

    def ok(**kw):
        return jsonify(dict(ok=True, **kw))

    def fail(message, code=400):
        return jsonify(ok=False, message=str(message)), code

    def body():
        return request.get_json(silent=True) or {}

    def model_arg():
        return "calib" if request.args.get("model", body().get("model")) == "calib" else "ndvi"

    @bp.errorhandler(Exception)
    def on_error(exc):
        if hasattr(exc, "code") and hasattr(exc, "description"):
            return fail(exc.description, exc.code)
        if isinstance(exc, ValueError):
            return fail(exc, 400)
        import traceback
        traceback.print_exc()
        return fail(exc, 500)

    # ------------------------------------------------------------------ pages

    def page_context():
        return {"ir_num": svc.settings["ir_cam"], "red_num": svc.settings["red_cam"],
                "views": render.VIEWS}

    @bp.route("/ndvi")
    def ndvi_page():
        svc.touch()
        return render_template("drop/ndvi.html", active_page="drop_ndvi", **page_context())

    @bp.route("/calibration")
    def calibration_page():
        if svc.input_mode != "live":
            svc.recorded.select("live")
        svc.touch()
        return render_template("drop/calibration.html", active_page="drop_calibration",
                               min_pairs=calib_core.MIN_PAIRS, recommended_pairs=calib_core.RECOMMENDED_PAIRS,
                               **page_context())

    # ------------------------------------------------------------------ streams

    @bp.route("/stream/<view>")
    def stream(view):
        if view not in render.VIEWS:
            abort(404)
        key = model_arg()

        def gen():
            fid = 0
            while True:
                frame = svc.wait_frame(fid, timeout=2.0)
                if frame is not None and frame["id"] == fid:
                    continue
                fid = frame["id"] if frame else 0
                data = svc.render(view, key)
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
                if frame is None:
                    time.sleep(1)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    @bp.route("/frame/<view>.jpg")
    def frame(view):
        if view not in render.VIEWS:
            abort(404)
        svc.fresh()
        return Response(svc.render(view, model_arg()), mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ cameras

    @bp.route("/api/cameras")
    def cameras():
        return ok(ir=svc.camera_info("ir"), red=svc.camera_info("red"),
                  available=sorted(int(k) for k in svc.cameras.keys()))

    @bp.route("/api/camera/metadata")
    def camera_metadata():
        f = svc.fresh()
        frame = None
        if f:
            frame = {"id": f["id"], "age_s": time.time() - f["t"], "captured": f["t"],
                     "ir_size": list(f["ir_s"].shape[1::-1]), "red_size": list(f["red_s"].shape[1::-1]),
                     "ir_crop": list(f["ir"].crop), "red_crop": list(f["red"].crop)}
        return ok(ir=svc.metadata("ir"), red=svc.metadata("red"), frame=frame,
                  sync_ms=f["sync_ms"] if f else None, status=svc.status, input_mode=svc.input_mode)

    @bp.route("/api/camera/control", methods=["POST"])
    def camera_control():
        b = body()
        role = b.get("role", "ir")
        if role not in ("ir", "red", "both"):
            return fail("role must be ir, red or both")
        return ok(applied=svc.set_control(role, b.get("id"), b.get("value")))

    @bp.route("/api/camera/lock_exposure", methods=["POST"])
    def lock_exposure():
        svc.require_live()
        return ok(locked=svc.lock_exposure(body().get("role", "both")))

    @bp.route("/api/camera/copy_exposure", methods=["POST"])
    def copy_exposure():
        svc.require_live()
        return ok(values=svc.copy_exposure())

    @bp.route("/api/camera/save_profile", methods=["POST"])
    def save_camera_profile():
        svc.require_live()
        b = body()
        return ok(saved=svc.save_camera_profile(b.get("role", "both"), b.get("name", "")))

    # ------------------------------------------------------------------ NDVI

    @bp.route("/api/ndvi/input", methods=["GET", "POST"])
    def ndvi_input():
        if request.method == "POST":
            b = body()
            return ok(input=svc.recorded.select(b.get("mode"), b.get("source"), b.get("index", 0)))
        return ok(input=svc.recorded.state())

    @bp.route("/api/ndvi/datasets")
    def ndvi_datasets():
        return ok(sources=svc.recorded.sources())

    @bp.route("/api/ndvi/evaluate", methods=["POST"])
    def ndvi_evaluate():
        return ok(input=svc.recorded.start_job())

    @bp.route("/api/ndvi/export", methods=["POST"])
    def ndvi_export():
        return ok(input=svc.recorded.start_job(export=True))

    @bp.route("/api/ndvi/settings", methods=["GET", "POST"])
    def ndvi_settings():
        if request.method == "POST":
            if svc.input_mode == "recorded" and "proc_width" in body():
                svc.recorded._idle()
            changed = svc.settings.update(body())
            if svc.input_mode == "recorded" and "proc_width" in changed:
                svc.recorded.select("recorded", svc.recorded.source, svc.recorded.index)
            if changed.get("eto_from_weather"):
                svc.env.start()
                try:
                    svc.apply_weather_eto()
                except RuntimeError:
                    pass   # applied by the worker once the weather arrives
        data = {k: v for k, v in svc.settings.data.items() if k != "camera_extras"}
        return ok(settings=data)

    @bp.route("/api/ndvi/stats")
    def ndvi_stats():
        svc.fresh()
        return ok(stats=svc.stats(model_arg()), status=svc.status)

    @bp.route("/api/profiles")
    def profiles():
        return ok(profiles=alignment.list_profiles(), active=svc.settings["profile"],
                  model=svc.model_state("ndvi"))

    @bp.route("/api/profiles/delete", methods=["POST"])
    def delete_profile():
        name = body().get("name", "")
        if not alignment.valid_name(name):
            return fail("invalid name")
        alignment.delete_profile(name)
        if svc.settings["profile"] == name:
            svc.set_active_profile(None)
        return ok()

    @bp.route("/api/ndvi/profile", methods=["POST"])
    def set_profile():
        name = body().get("name") or None
        if name and not alignment.valid_name(name):
            return fail("invalid name")
        svc.set_active_profile(name)
        return ok(model=svc.model_state("ndvi"))

    @bp.route("/api/model")
    def model_state():
        return ok(model=svc.model_state(model_arg()))

    @bp.route("/api/tuning", methods=["POST"])
    def tuning():
        b = body()
        key = model_arg()
        offset = b.get("offset_px")
        svc.tune(key, b.get("distance_m"), b.get("pitch_deg"),
                 [float(offset[0]), float(offset[1])] if offset is not None else None)
        return ok(model=svc.model_state(key))

    @bp.route("/api/tuning/auto_offset", methods=["POST"])
    def auto_offset():
        key = model_arg()
        res = svc.auto_offset(key)
        return ok(result=res, model=svc.model_state(key))

    @bp.route("/api/tuning/save", methods=["POST"])
    def save_tuning():
        m = svc.model("ndvi")
        if not m.name:
            return fail("no calibration profile is active")
        alignment.update_tuning(m.name, m.plane, m.offset_px)
        return ok(model=svc.model_state("ndvi"))

    @bp.route("/api/tuning/revert", methods=["POST"])
    def revert_tuning():
        svc.set_active_profile(svc.settings["profile"])
        return ok(model=svc.model_state("ndvi"))

    @bp.route("/api/ndvi/red_gain_from_panel", methods=["POST"])
    def red_gain_from_panel():
        return ok(red_gain=svc.red_gain_from_panel())

    @bp.route("/api/ndvi/snapshot", methods=["POST"])
    def snapshot():
        return ok(path=svc.snapshot())

    # ------------------------------------------------------------------ field location + weather

    @bp.route("/api/field")
    def field_state():
        return ok(**svc.env.state())

    @bp.route("/api/field/refresh", methods=["POST"])
    def field_refresh():
        svc.env.refresh()
        return ok()

    @bp.route("/api/field/location", methods=["POST"])
    def field_location():
        b = body()
        if b.get("clear"):
            svc.env.set_manual(None, None)
        else:
            svc.env.set_manual(b.get("latitude"), b.get("longitude"), b.get("elevation"), b.get("prefer"))
        return ok(**svc.env.state())

    @bp.route("/api/field/use_eto", methods=["POST"])
    def field_use_eto():
        try:
            return ok(eto=svc.apply_weather_eto())
        except RuntimeError as exc:
            return fail(exc, 409)

    # ------------------------------------------------------------------ calibration

    def session():
        if svc.calib is None:
            abort(409, "no calibration session — start a new one")
        return svc.calib

    @bp.route("/api/calib/status")
    def calib_status():
        return ok(session=svc.calib.status() if svc.calib else None)

    @bp.route("/api/calib/new", methods=["POST"])
    def calib_new():
        svc.require_live()
        if svc.calib is not None:
            svc.calib.discard()
        svc.calib = CalibrationSession(svc, body())
        return ok(session=svc.calib.status())

    @bp.route("/api/calib/capture", methods=["POST"])
    def calib_capture():
        svc.require_live()
        s = session()
        res = s.capture()
        return jsonify(dict(res, session=s.status())), (200 if res["ok"] else 422)

    @bp.route("/api/calib/delete_pair", methods=["POST"])
    def calib_delete_pair():
        s = session()
        s.delete(int(body().get("idx", -1)))
        return ok(session=s.status())

    @bp.route("/api/calib/run", methods=["POST"])
    def calib_run():
        s = session()
        try:
            return ok(session=s.run())
        except (RuntimeError, ValueError) as exc:
            return fail(exc, 422)

    @bp.route("/api/calib/save", methods=["POST"])
    def calib_save():
        s = session()
        b = body()
        try:
            folder = s.save(b.get("name"), bool(b.get("overwrite")))
        except FileExistsError:
            return fail("a profile with this name exists — tick 'overwrite' or choose another name", 409)
        if b.get("use_in_ndvi"):
            svc.set_active_profile(s.saved_as)
        return ok(folder=folder, session=s.status())

    @bp.route("/api/calib/discard", methods=["POST"])
    def calib_discard():
        if svc.calib is not None:
            svc.calib.discard(bool(body().get("delete_images")))
            svc.calib = None
        return ok()

    @bp.route("/api/calib/live")
    def calib_live():
        svc.touch()
        b = svc.calib.board if svc.calib else calib_core.Board(int(request.args.get("cols", 8)),
                                                               int(request.args.get("rows", 6)))
        return ok(corners=svc.live_corners(b), status=svc.status)

    @bp.route("/api/calib/thumb/<int:idx>.jpg")
    def calib_thumb(idx):
        data = session().thumb(idx)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg")

    return bp
