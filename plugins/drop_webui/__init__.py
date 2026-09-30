"""
drop-rpi web UI for CamUI: NDVI and Calibration tabs.

init(app, context) is called by plugins/drop_rpi.py with CamUI's Flask app
and {'cameras': {num: CameraObject}, 'plugin_hooks': ...}.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def find_repo():
    candidates = [os.environ.get("DROP_RPI_DIR"),
                  os.path.abspath(os.path.join(HERE, "..", "..", ".."))]
    for c in candidates:
        if c and os.path.isfile(os.path.join(c, "Claude_xaris", "alignment.py")):
            return c
    raise RuntimeError("drop-rpi repository not found: set DROP_RPI_DIR to its folder")


def init(app, context):
    repo = find_repo()
    for sub in ("Claude_xaris", "calibration"):
        path = os.path.join(repo, sub)
        if path not in sys.path:
            sys.path.insert(0, path)

    from .routes import create_blueprint
    from .service import DropService

    svc = DropService(context.get("cameras", {}), os.path.join(repo, "data"))
    app.register_blueprint(create_blueprint(svc))
    app.extensions["drop_rpi"] = svc

    nav = app.config.setdefault("PLUGIN_NAV", [])
    nav.extend([{"id": "drop_ndvi", "label": "NDVI", "url": "/drop/ndvi"},
                {"id": "drop_calibration", "label": "Calibration", "url": "/drop/calibration"}])
    if not app.config.get("_PLUGIN_NAV_PROCESSOR"):
        app.config["_PLUGIN_NAV_PROCESSOR"] = True

        @app.context_processor
        def plugin_nav():
            return {"plugin_nav": app.config.get("PLUGIN_NAV", [])}

    print(f"[drop-rpi] NDVI + Calibration tabs enabled (repo {repo})")
    return svc
