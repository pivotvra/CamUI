"""
Frame grabbing from CamUI CameraObjects without disturbing their MJPEG streams.

Frames are returned as BGR uint8 arrays in the sensor's native orientation
(CamUI hflip/vflip undone), together with the metadata of the same request,
so the ScalerCrop is always the one the frame was taken with.
"""
import concurrent.futures
import threading
import time
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class Frame:
    bgr: np.ndarray
    metadata: dict
    crop: tuple            # ScalerCrop (x, y, w, h) in full-sensor pixels
    hflip: bool
    vflip: bool
    t: float = field(default_factory=time.time)

    @property
    def size(self):
        return self.bgr.shape[1], self.bgr.shape[0]


def to_bgr(array, fmt):
    """picamera2 array (memory order depends on `fmt`) → BGR uint8."""
    fmt = (fmt or "").upper()
    if array.ndim == 2:
        if fmt.startswith("YUV420"):
            return cv2.cvtColor(array, cv2.COLOR_YUV2BGR_I420)
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if fmt in ("RGB888", "XRGB8888"):          # memory order B, G, R(, X)
        return np.ascontiguousarray(array[..., :3])
    if fmt in ("BGR888", "XBGR8888"):          # memory order R, G, B(, X)
        return np.ascontiguousarray(array[..., 2::-1])
    return np.ascontiguousarray(array[..., :3])


def flip_code(hflip, vflip):
    if hflip and vflip:
        return -1
    if hflip:
        return 1
    if vflip:
        return 0
    return None


def unflip(image, hflip, vflip):
    code = flip_code(hflip, vflip)
    return image if code is None else cv2.flip(image, code)


def camera_flips(cam, config=None):
    """(hflip, vflip) of the transform the image was actually produced with."""
    tr = (config or {}).get("transform") if isinstance(config, dict) else None
    if tr is not None:
        return bool(getattr(tr, "hflip", 0)), bool(getattr(tr, "vflip", 0))
    prof = cam.camera_profile or {}
    return bool(int(prof.get("hflip", 0) or 0)), bool(int(prof.get("vflip", 0) or 0))


def sensor_size(cam):
    try:
        return tuple(int(v) for v in cam.picam2.sensor_resolution)
    except Exception:
        return 3280, 2464


def _crop_from(metadata, cam):
    crop = metadata.get("ScalerCrop") if metadata else None
    if crop is None:
        w, h = sensor_size(cam)
        return 0, 0, w, h
    return tuple(int(v) for v in crop)


def _release_later(job):
    def done(future):
        try:
            future.result().release()
        except Exception:
            pass
    job._future.add_done_callback(done)


def grab(cam, timeout=2.0):
    """One frame from the running video stream, or None (camera busy)."""
    if cam is None or cam.use_placeholder:
        return None
    picam2 = cam.picam2
    try:
        config = picam2.camera_configuration()
        fmt = config["main"]["format"]
        job = picam2.capture_request(wait=False)
    except Exception:
        return None
    try:
        req = picam2.wait(job, timeout=timeout)
    except (TimeoutError, concurrent.futures.TimeoutError):
        _release_later(job)
        return None
    except Exception:
        return None
    try:
        array = req.make_array("main")
        metadata = dict(req.get_metadata())
    finally:
        req.release()
    hflip, vflip = camera_flips(cam, config)
    return Frame(unflip(to_bgr(array, fmt), hflip, vflip), metadata,
                 _crop_from(metadata, cam), hflip, vflip)


def _parallel(fn, cams):
    out = [None] * len(cams)
    errors = [None] * len(cams)

    def run(i):
        try:
            out[i] = fn(cams[i])
        except Exception as exc:
            errors[i] = exc
    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(len(cams))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out, errors


def grab_pair(cam_ir, cam_red, timeout=2.0):
    (ir, red), _ = _parallel(lambda c: grab(c, timeout), [cam_ir, cam_red])
    return ir, red


def capture_still(cam):
    """
    Full-quality still (CamUI still configuration), like CameraObject.take_still
    but kept in memory. The live stream shows the placeholder meanwhile.
    """
    picam2 = cam.picam2
    cam.use_placeholder = True
    time.sleep(0.3)
    try:
        cam.stop_streaming()
        still_config = cam.still_config
        buffers, metadata = picam2.switch_mode_and_capture_buffers(still_config, ["main"])
        array = picam2.helpers.make_array(buffers[0], still_config["main"])
        fmt = still_config["main"]["format"]
    finally:
        try:
            cam.start_streaming()
        finally:
            cam.use_placeholder = False
    hflip, vflip = camera_flips(cam, still_config)
    metadata = dict(metadata)
    return Frame(unflip(to_bgr(array, fmt), hflip, vflip), metadata,
                 _crop_from(metadata, cam), hflip, vflip)


def capture_still_pair(cam_ir, cam_red):
    (ir, red), errors = _parallel(capture_still, [cam_ir, cam_red])
    for e in errors:
        if e is not None:
            raise RuntimeError(f"still capture failed: {e}")
    return ir, red


def sync_ms(frame_a, frame_b):
    ta = (frame_a.metadata or {}).get("SensorTimestamp")
    tb = (frame_b.metadata or {}).get("SensorTimestamp")
    if ta is None or tb is None:
        return None
    return (int(ta) - int(tb)) / 1e6
