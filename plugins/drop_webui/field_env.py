"""
GPS position + irrigation weather for the NDVI tab.

A background thread reads a fix from the GPS receiver (repo gps.py) and
fetches Open-Meteo data (repo weather.py) for it, so HTTP requests never block
on the serial port or the network. A manual location (data/field_location.json)
is used when set, and as a fallback while there is no GPS fix.
"""
import importlib.util
import json
import math
import os
import threading
import time

GPS_INTERVAL_S = 300          # re-read the position this often
GPS_RETRY_S = 60              # after a failed read
GPS_TIMEOUT_S = 20
WEATHER_INTERVAL_S = 15 * 60
WEATHER_RETRY_S = 120
MOVE_REFRESH_KM = 1.0         # refetch weather when the position moves this far


def _load(repo, name):
    spec = importlib.util.spec_from_file_location(f"drop_{name}", os.path.join(repo, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


class FieldEnvironment:
    def __init__(self, repo, data_dir, on_weather=None):
        self.path = os.path.join(data_dir, "field_location.json")
        self.on_weather = on_weather
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread = None
        self.gps = {"fix": None, "t": None, "error": None}
        self.weather = {"summary": None, "t": None, "error": None, "location": None}
        self.manual = self._read_manual()
        self.weather_mod, self.gps_mod = None, None
        try:
            self.weather_mod = _load(repo, "weather")
        except Exception as exc:
            self.weather["error"] = f"weather.py not available: {exc}"
        try:
            self.gps_mod = _load(repo, "gps")
        except Exception as exc:
            self.gps["error"] = f"GPS unavailable: {exc}"

    # ------------------------------------------------------------------ manual location

    def _read_manual(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return {"latitude": float(d["latitude"]), "longitude": float(d["longitude"]),
                    "elevation": d.get("elevation"), "prefer": bool(d.get("prefer", False))}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def set_manual(self, lat, lon, elevation=None, prefer=False):
        if lat is None or lon is None:
            self.manual = None
            try:
                os.remove(self.path)
            except OSError:
                pass
        else:
            lat, lon = float(lat), float(lon)
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("latitude must be in [-90, 90] and longitude in [-180, 180]")
            self.manual = {"latitude": lat, "longitude": lon,
                           "elevation": float(elevation) if elevation not in (None, "") else None,
                           "prefer": bool(prefer)}
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.manual, f, indent=2)
        self.refresh()

    def location(self):
        """(lat, lon, elevation, source) currently used for the weather, or None."""
        fix = self.gps["fix"]
        m = self.manual
        if m and (m["prefer"] or fix is None):
            return m["latitude"], m["longitude"], m["elevation"], "manual"
        if fix is not None:
            return fix["latitude"], fix["longitude"], fix.get("altitude"), "gps"
        return None

    # ------------------------------------------------------------------ worker

    def start(self):
        with self.lock:
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="drop-field-env", daemon=True)
                self.thread.start()

    def refresh(self):
        """Force a new GPS read and weather fetch."""
        self.gps["next"] = 0
        self.weather["next"] = 0
        self.start()
        self.wake.set()

    def _read_gps(self):
        if self.gps_mod is None:
            return
        try:
            fix = self.gps_mod.get_location(timeout_s=GPS_TIMEOUT_S)
            self.gps.update(fix=fix, t=time.time(), error=None, next=time.time() + GPS_INTERVAL_S)
        except Exception as exc:
            self.gps.update(error=f"{type(exc).__name__}: {exc}", next=time.time() + GPS_RETRY_S)

    def _fetch_weather(self, loc):
        lat, lon, elev, source = loc
        try:
            data = self.weather_mod.fetch_weather(lat, lon, elev)
            summary = self.weather_mod.summarize(data)
            self.weather.update(summary=summary, t=time.time(), error=None, location=[lat, lon],
                                source=source, next=time.time() + WEATHER_INTERVAL_S)
            if self.on_weather:
                self.on_weather(summary)
        except Exception as exc:
            self.weather.update(error=f"{type(exc).__name__}: {exc}", next=time.time() + WEATHER_RETRY_S)

    def _run(self):
        while True:
            now = time.time()
            if self.gps_mod is not None and now >= self.gps.get("next", 0) and not (self.manual and self.manual["prefer"]):
                self._read_gps()
            loc = self.location()
            if loc and self.weather_mod is not None:
                prev = self.weather.get("location")
                moved = prev is None or _km(prev, loc[:2]) > MOVE_REFRESH_KM
                if moved or time.time() >= self.weather.get("next", 0):
                    self._fetch_weather(loc)
            self.wake.wait(5)
            self.wake.clear()

    # ------------------------------------------------------------------ API

    def state(self):
        self.start()
        now = time.time()
        loc = self.location()
        return {
            "location": None if loc is None else {"latitude": loc[0], "longitude": loc[1],
                                                   "elevation": loc[2], "source": loc[3]},
            "gps": {"fix": self.gps["fix"], "age_s": now - self.gps["t"] if self.gps["t"] else None,
                    "error": self.gps["error"], "available": self.gps_mod is not None},
            "manual": self.manual,
            "weather": self.weather["summary"],
            "weather_age_s": now - self.weather["t"] if self.weather["t"] else None,
            "weather_error": self.weather["error"],
        }
