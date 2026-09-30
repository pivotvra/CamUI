"""
CamUI plugin: drop-rpi NDVI and calibration tabs.

The code lives in plugins/drop_webui/. It needs the drop-rpi repository
(found as the folder containing CamUI/, or via the DROP_RPI_DIR environment
variable) and python3-opencv + numpy.
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def init_plugin(app, context=None):
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import drop_webui
        drop_webui.init(app, context or {})
    except Exception:
        print("[drop-rpi] plugin disabled:")
        traceback.print_exc()
