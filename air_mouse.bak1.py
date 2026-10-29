"""
AirGesture Pro - hand-tracking mouse for Windows.

Gestures (shown in the preview window, press H to hide the help):
    Move cursor   : point / relax hand, cursor follows the knuckles
    Left click    : pinch index + thumb, release
    Drag          : pinch index + thumb and hold, move, release to drop
    Double click  : two quick pinches
    Right click   : pinch middle + thumb
    Scroll        : raise index + middle (ring + pinky down), move up/down
    Pause cursor  : open the whole hand (5 fingers spread)

Keys: F8 (global) or SPACE = on/off, H = help, Q / ESC = quit.
"""
from __future__ import annotations

import os

os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

import argparse
import csv
import ctypes
import json
import logging
import math
import sys
import threading
import time
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import cv2
import numpy as np

IS_WINDOWS = sys.platform == "win32"
HERE = Path(__file__).resolve().parent

# Console + file log. File helps when launched from the desktop shortcut (no console).
_handlers = [logging.StreamHandler()]
try:
    _handlers.append(logging.FileHandler(HERE / "air_mouse.log", mode="w", encoding="utf-8"))
except OSError:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=_handlers)
log = logging.getLogger("airgesture")
VK_F8 = 0x77

# BGR colours
GREY, GREEN, ORANGE = (180, 180, 180), (0, 255, 0), (0, 165, 255)
RED, YELLOW, CYAN, BLUE = (0, 0, 255), (0, 215, 255), (255, 255, 0), (255, 0, 0)


# ==========================================================
# 1. Configuration
# ==========================================================
CONFIG_VERSION = 2


@dataclass
class Config:
    config_version: int = CONFIG_VERSION
    calibrated: bool = False          # False = guided pinch calibration on next start
    # camera
    cam_w: int = 640
    cam_h: int = 480
    cam_fps: int = 60
    camera_index: int = -1            # -1 = auto search
    # hand travel area (share of camera width; height follows screen shape)
    active_width: float = 0.72
    active_center_x: float = 0.5
    active_center_y: float = 0.5
    # cursor smoothing (One Euro): lower cutoff = steadier, higher beta = less lag when fast
    smooth_min_cutoff: float = 1.2
    smooth_beta: float = 0.012
    deadzone_stabilizer_px: float = 2.2
    # scroll
    deadzone_scroll_px: float = 8.0
    scroll_gain: float = 6.0          # wheel units per pixel of finger travel
    scroll_invert: bool = False
    # click / drag
    pinch_threshold_open: float = 0.44
    pinch_threshold_closed: float = 0.33
    pinch_smooth: float = 0.55        # 1.0 = no smoothing
    pinch_min_reach: float = 0.4      # right click only: middle tip must be this far (x palm) from its knuckle
    pinch_confirm_frames: int = 2     # frames a pinch must hold / release to count
    drag_hold_seconds: float = 0.5
    double_click_window: float = 0.45
    right_click_threshold: float = 0.32
    right_click_cooldown_seconds: float = 0.5
    freeze_threshold_left: float = 0.46
    freeze_threshold_right: float = 0.42
    aim_lookback_seconds: float = 0.08
    # poses
    palm_rest_pinches: float = 0.45
    min_palm_size: float = 35.0
    mode_confirm_frames: int = 3
    rest_hold_seconds: float = 0.4    # open hand must stay this long to pause
    max_jump_palms: float = 1.8       # tracker jump bigger than this per frame = glitch, skipped
    acquire_frames: int = 3
    lost_grace_seconds: float = 0.3
    # tracker
    detect_confidence: float = 0.6
    track_confidence: float = 0.6
    # app
    auto_start: bool = True           # False = start paused, press F8 / SPACE
    show_help: bool = True
    draw_skeleton: bool = True

    @classmethod
    def load(cls, path: Path) -> "Config":
        cfg = cls()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Bad config file (%s). Using defaults.", exc)
                raw = {}
            known = {f.name for f in fields(cls)}
            try:
                old_ver = int(raw.get("config_version", 1))
            except (TypeError, ValueError):
                old_ver = 1
            if raw and old_ver < CONFIG_VERSION:
                log.info("Old config version. Using new defaults. Old file kept as .old.json")
                try:
                    path.replace(path.with_name(path.stem + ".old.json"))
                except OSError:
                    pass
                raw = {}
            for key, val in raw.items():
                if key not in known:
                    log.info("Config key '%s' is retired. Ignored.", key)
                    continue
                default = getattr(cfg, key)
                try:
                    if isinstance(default, bool):
                        val = bool(val)
                    elif isinstance(default, int):
                        val = int(val)
                    elif isinstance(default, float):
                        val = float(val)
                    setattr(cfg, key, val)
                except (TypeError, ValueError):
                    log.warning("Bad value for '%s'. Using default.", key)
        cfg.validate()
        return cfg

    def validate(self):
        self.active_width = min(max(self.active_width, 0.3), 0.95)
        self.pinch_threshold_open = max(self.pinch_threshold_open, self.pinch_threshold_closed + 0.03)
        self.pinch_smooth = min(max(self.pinch_smooth, 0.1), 1.0)
        self.mode_confirm_frames = max(1, self.mode_confirm_frames)
        self.pinch_confirm_frames = max(1, self.pinch_confirm_frames)
        self.acquire_frames = max(1, self.acquire_frames)

    def save(self, path: Path):
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


# ==========================================================
# 2. OS input (SendInput on Windows, dry-run elsewhere)
# ==========================================================
class WinInput:
    LDOWN, LUP, RDOWN, RUP, WHEEL = 0x0002, 0x0004, 0x0008, 0x0010, 0x0800

    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

        ulong_ptr = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                        ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                        ("time", ctypes.c_ulong), ("dwExtraInfo", ulong_ptr)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", ctypes.c_ulong), ("u", _U)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        self._MI, self._INPUT, self._POINT = MOUSEINPUT, INPUT, POINT
        self.u = ctypes.windll.user32
        self.u.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
        self.screen_w = self.u.GetSystemMetrics(0)
        self.screen_h = self.u.GetSystemMetrics(1)

    def _send(self, flags, data=0):
        inp = self._INPUT(type=0)
        inp.u.mi = self._MI(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        self.u.SendInput(1, ctypes.byref(inp), ctypes.sizeof(self._INPUT))

    def move(self, x, y):
        self.u.SetCursorPos(int(x), int(y))

    def get_pos(self):
        p = self._POINT()
        self.u.GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def left_down(self): self._send(self.LDOWN)
    def left_up(self): self._send(self.LUP)

    def click(self):
        self._send(self.LDOWN)
        self._send(self.LUP)

    def right_click(self):
        self._send(self.RDOWN)
        self._send(self.RUP)

    def scroll(self, amount):
        self._send(self.WHEEL, int(amount))

    def key_pressed(self, vk):
        return bool(self.u.GetAsyncKeyState(vk) & 0x8000)


class NullInput:
    """Dry run. Moves nothing. Used on non-Windows and for tests."""

    def __init__(self, w=1920, h=1080):
        self.screen_w, self.screen_h = w, h
        self.pos = (w // 2, h // 2)
        self.events = deque(maxlen=500)

    def move(self, x, y): self.pos = (int(x), int(y))
    def get_pos(self): return self.pos
    def left_down(self): self.events.append("down")
    def left_up(self): self.events.append("up")
    def click(self): self.events.append("click")
    def right_click(self): self.events.append("right")
    def scroll(self, amount): self.events.append(f"scroll:{int(amount)}")
    def key_pressed(self, vk): return False


class TraceWriter:
    """Per-frame CSV for live testing. Read it with analyze_trace.py."""
    COLS = ["wall", "t", "mode", "pose", "L", "R", "raw_L", "raw_R", "fingers", "palm",
            "ax", "ay", "cx", "cy", "pinch", "drag", "fps", "event"]

    def __init__(self, path: Path, cfg: "Config", screen):
        meta = asdict(cfg)
        meta["screen_w"], meta["screen_h"] = screen
        path.with_suffix(".meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        self.f = open(path, "w", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        self.w.writerow(self.COLS)
        self.t0, self.events, self.n = time.perf_counter(), [], 0

    def event(self, name):
        self.events.append(name)

    def row(self, now, st, info, cursor, fps, pinch, drag):
        def f(v, nd=3):
            return "" if v is None else round(v, nd)
        a = info.get("anchor") or (None, None)
        self.w.writerow([round(time.time(), 3), round(now - self.t0, 3), st.mode, info.get("pose", ""),
                         f(info.get("L")), f(info.get("R")), f(info.get("raw_L")), f(info.get("raw_R")),
                         info.get("fingers", ""), f(info.get("palm"), 1), f(a[0], 1), f(a[1], 1),
                         cursor[0], cursor[1], int(pinch), int(drag), round(fps, 1), "|".join(self.events)])
        self.events.clear()
        self.n += 1
        if self.n % 30 == 0:
            self.f.flush()

    def close(self):
        try:
            self.f.flush()
            self.f.close()
        except Exception:
            pass


class TracingInput:
    """Wraps a real or dry input. Logs every click / drag / scroll into the trace."""

    def __init__(self, inner, trace: TraceWriter):
        self.inner, self.trace = inner, trace

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def click(self): self.trace.event("click"); self.inner.click()
    def right_click(self): self.trace.event("right"); self.inner.right_click()
    def left_down(self): self.trace.event("down"); self.inner.left_down()
    def left_up(self): self.trace.event("up"); self.inner.left_up()
    def scroll(self, amount): self.trace.event(f"scroll:{int(amount)}"); self.inner.scroll(amount)


# ==========================================================
# 3. Smoothing
# ==========================================================
class OneEuroFilter:
    def __init__(self, min_cutoff=1.0, beta=0.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.x_prev = None
        self.dx_prev = 0.0
        self.t_prev = None

    @staticmethod
    def _alpha(t_e, cutoff):
        r = 2 * math.pi * cutoff * t_e
        return r / (r + 1)

    def filter(self, x, t):
        if self.t_prev is None:
            self.t_prev, self.x_prev = t, x
            return x
        t_e = t - self.t_prev
        if t_e <= 0.0:
            return self.x_prev
        a_d = self._alpha(t_e, self.d_cutoff)
        dx_hat = a_d * ((x - self.x_prev) / t_e) + (1 - a_d) * self.dx_prev
        a = self._alpha(t_e, self.min_cutoff + self.beta * abs(dx_hat))
        x_hat = a * x + (1 - a) * self.x_prev
        self.x_prev, self.dx_prev, self.t_prev = x_hat, dx_hat, t
        return x_hat


class CursorStabilizer:
    def __init__(self, min_cutoff, beta, deadzone_px):
        self.fx = OneEuroFilter(min_cutoff, beta)
        self.fy = OneEuroFilter(min_cutoff, beta)
        self.deadzone_px = deadzone_px
        self.last = None

    def reset(self):
        self.fx.reset()
        self.fy.reset()
        self.last = None

    def process(self, x, y, t):
        sx, sy = self.fx.filter(x, t), self.fy.filter(y, t)
        if self.last is not None and math.hypot(sx - self.last[0], sy - self.last[1]) < self.deadzone_px:
            return int(round(self.last[0])), int(round(self.last[1]))
        self.last = (sx, sy)
        return int(round(sx)), int(round(sy))


# ==========================================================
# 4. Camera (threaded, latest-frame, no stale re-processing)
# ==========================================================
class CameraStream:
    def __init__(self, cfg: Config):
        self.cap = None
        indices = [cfg.camera_index] if cfg.camera_index >= 0 else [0, 1, 2]
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF, cv2.CAP_ANY] if IS_WINDOWS else [cv2.CAP_ANY]
        for idx in indices:
            for backend in backends:
                cap = cv2.VideoCapture(idx, backend)
                if not cap.isOpened():
                    cap.release()
                    continue
                cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.cam_w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.cam_h)
                cap.set(cv2.CAP_PROP_FPS, cfg.cam_fps)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                ok, frame = cap.read()
                if ok and frame is not None:
                    self.cap = cap
                    log.info("Camera ready (index %d, %dx%d).", idx, frame.shape[1], frame.shape[0])
                    break
                cap.release()
            if self.cap is not None:
                break
        if self.cap is None:
            raise RuntimeError("No working webcam found. Close other apps that use the camera.")

        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._frame, self._id = None, 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self.cap.read()
            if not ok or frame is None:
                time.sleep(0.005)
                continue
            with self._cond:
                self._frame, self._id = frame, self._id + 1
                self._cond.notify_all()

    def read(self, last_id, timeout=0.1):
        """Return (frame_id, frame). frame is None if nothing new."""
        with self._cond:
            self._cond.wait_for(lambda: self._id != last_id or self._stop.is_set(), timeout)
            if self._id == last_id:
                return last_id, None
            return self._id, self._frame

    def stop(self):
        self._stop.set()
        time.sleep(0.05)
        if self.cap is not None:
            self.cap.release()


# ==========================================================
# 5. Hand tracker (Tasks API first, legacy fallback)
# ==========================================================
class HandTracker:
    CONNECTIONS = [(0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
                   (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15),
                   (15, 16), (13, 17), (17, 18), (18, 19), (19, 20), (0, 17)]
    MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
                 "hand_landmarker/float16/1/hand_landmarker.task")

    def __init__(self, cfg: Config):
        import mediapipe as mp
        self.mp = mp
        self.mode = None
        self.last_ts = 0
        try:
            self._init_tasks(cfg)
        except Exception as exc:
            log.warning("Tasks API failed (%s). Trying legacy API.", exc)
            self._init_legacy(cfg)
        log.info("Hand tracker: %s API.", self.mode)

    def _init_tasks(self, cfg):
        from mediapipe.tasks import python
        from mediapipe.tasks.python import vision

        model = HERE / "hand_landmarker.task"
        if not model.exists():
            log.info("Downloading hand model...")
            urllib.request.urlretrieve(self.MODEL_URL, model)
        options = vision.HandLandmarkerOptions(
            base_options=python.BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=1,
            min_hand_detection_confidence=cfg.detect_confidence,
            min_hand_presence_confidence=cfg.detect_confidence,
            min_tracking_confidence=cfg.track_confidence,
        )
        self.detector = vision.HandLandmarker.create_from_options(options)
        self.mode = "tasks"

    def _init_legacy(self, cfg):
        try:
            from mediapipe.python.solutions import hands as mp_hands
        except Exception:
            mp_hands = self.mp.solutions.hands
        self.hands = mp_hands.Hands(
            static_image_mode=False, max_num_hands=1, model_complexity=1,
            min_detection_confidence=cfg.detect_confidence,
            min_tracking_confidence=cfg.track_confidence,
        )
        self.mode = "legacy"

    def find(self, rgb, w, h):
        """Return list of 21 (x, y) pixel points, or None."""
        if self.mode == "tasks":
            img = self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
            ts = max(int(time.perf_counter() * 1000), self.last_ts + 1)
            self.last_ts = ts
            res = self.detector.detect_for_video(img, ts)
            if res.hand_landmarks:
                return [(p.x * w, p.y * h) for p in res.hand_landmarks[0]]
            return None
        res = self.hands.process(rgb)
        if res.multi_hand_landmarks:
            return [(p.x * w, p.y * h) for p in res.multi_hand_landmarks[0].landmark]
        return None

    def close(self):
        try:
            if self.mode == "tasks":
                self.detector.close()
            else:
                self.hands.close()
        except Exception:
            pass

    def draw(self, frame, lm):
        pts = [(int(x), int(y)) for x, y in lm]
        for a, b in self.CONNECTIONS:
            cv2.line(frame, pts[a], pts[b], (80, 220, 100), 2, cv2.LINE_AA)
        for p in pts:
            cv2.circle(frame, p, 4, (255, 255, 255), cv2.FILLED)


# ==========================================================
# 6. Gesture controller
# ==========================================================
def dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


@dataclass
class Status:
    mode: str = "IDLE"
    color: tuple = GREY
    anchor: tuple | None = None
    left_pinch: tuple | None = None    # (index_tip, thumb_tip)
    right_pinch: tuple | None = None   # (middle_tip, thumb_tip)
    scroll_pts: tuple | None = None
    banner: list | None = None         # big centre text (calibration)
    debug: list | None = None


class AirGestureController:
    REST, SCROLL, MOVE = "REST", "SCROLL", "MOVE"
    CALIB_STEPS = ["Relax hand like when you move the cursor",
                   "PINCH index + thumb and HOLD",
                   "PINCH middle + thumb and HOLD"]
    CALIB_LEAD, CALIB_SAMPLE = 2.0, 2.0

    def __init__(self, cfg: Config, inp, tracker: HandTracker | None = None, cfg_path: Path | None = None):
        self.cfg, self.inp, self.tracker, self.cfg_path = cfg, inp, tracker, cfg_path
        self.screen_w, self.screen_h = inp.screen_w, inp.screen_h
        self.stab = CursorStabilizer(cfg.smooth_min_cutoff, cfg.smooth_beta, cfg.deadzone_stabilizer_px)
        self.enabled = cfg.auto_start
        self.show_debug = False
        self.calib = None
        self.trace = None
        self.info = {}
        self.is_dragging = False
        self.last_seen = None
        self.last_click_time, self.last_click_pos = 0.0, None
        self.last_right_time = 0.0
        self.flash_text, self.flash_color, self.flash_until = "", GREY, 0.0
        self._rect_key, self._rect = None, None
        self._reset_state()

    # ---------- state ----------
    def _reset_state(self):
        self.stab.reset()
        self.left_r = self.right_r = None
        self.fstate = [False] * 4
        self.pose, self._cand, self._cand_n, self._cand_since = self.MOVE, self.MOVE, 0, 0.0
        self.is_pinching, self.pinch_start = False, 0.0
        self._on_n = self._off_n = 0
        self.right_armed = False
        self.aim_locked = False
        self.last_anchor, self.skipped = None, 0
        self.offset = [0.0, 0.0]           # hand-to-cursor shift, set when cursor is re-anchored
        self._rebase_pending, self._last_mapped = False, None
        self.history = deque(maxlen=90)
        self.hand_frames = 0
        self._scroll_reset()

    def _scroll_reset(self):
        self._sc_s = self._sc_ref = None
        self._sc_engaged, self._sc_acc, self._sc_idle = False, 0.0, 0.0

    def release_all(self):
        if self.is_dragging:
            self.inp.left_up()
            self.is_dragging = False
        self.is_pinching = False
        self._on_n = self._off_n = 0
        self._scroll_reset()

    def toggle(self):
        self.enabled = not self.enabled
        if not self.enabled:
            self.release_all()
        log.info("Control %s.", "ON" if self.enabled else "OFF")

    def _flash(self, text, color, now, secs=0.35):
        self.flash_text, self.flash_color, self.flash_until = text, color, now + secs

    def _reseed(self, now):
        """Start smoothing from where the real cursor is now. No jump."""
        x, y = self.inp.get_pos()
        for f, v in ((self.stab.fx, x), (self.stab.fy, y)):
            f.x_prev, f.dx_prev, f.t_prev = v, 0.0, now
        self.stab.last = (x, y)

    def _save_cfg(self):
        if self.cfg_path:
            try:
                self.cfg.save(self.cfg_path)
            except OSError as exc:
                log.warning("Could not save config: %s", exc)

    # ---------- geometry ----------
    def active_rect(self, cw, ch):
        if self._rect_key != (cw, ch):
            w = cw * self.cfg.active_width
            h = w * self.screen_h / self.screen_w
            if h > ch * 0.9:
                h = ch * 0.9
                w = h * self.screen_w / self.screen_h
            x0 = min(max(cw * self.cfg.active_center_x - w / 2, 0), cw - w)
            y0 = min(max(ch * self.cfg.active_center_y - h / 2, 0), ch - h)
            self._rect, self._rect_key = (x0, y0, x0 + w, y0 + h), (cw, ch)
        return self._rect

    def _to_screen(self, ax, ay, cw, ch):
        x0, y0, x1, y1 = self.active_rect(cw, ch)
        nx = min(max((ax - x0) / (x1 - x0), 0.0), 1.0)
        ny = min(max((ay - y0) / (y1 - y0), 0.0), 1.0)
        return nx * (self.screen_w - 1), ny * (self.screen_h - 1)

    # ---------- hand features ----------
    def _update_fingers(self, lm):
        """Finger up/down with hysteresis. In-between keeps old state. No flicker."""
        wrist = lm[0]
        for i, (t, p) in enumerate(((8, 6), (12, 10), (16, 14), (20, 18))):
            r = dist(lm[t], wrist) / max(dist(lm[p], wrist), 1e-6)
            if r > 1.2:
                self.fstate[i] = True
            elif r < 1.0:
                self.fstate[i] = False
        return self.fstate

    @staticmethod
    def _thumb_out(lm):
        return dist(lm[4], lm[17]) > dist(lm[3], lm[17])

    @staticmethod
    def _palm_size(lm):
        return max(dist(lm[0], lm[9]), 1.15 * dist(lm[5], lm[17]))

    @staticmethod
    def _anchor(lm):
        """Centre of the 4 knuckles. Barely moves when fingers pinch."""
        return (sum(lm[i][0] for i in (5, 9, 13, 17)) / 4.0,
                sum(lm[i][1] for i in (5, 9, 13, 17)) / 4.0)

    def _raw_ratios(self, lm, palm):
        rl = dist(lm[8], lm[4]) / palm
        rr = dist(lm[12], lm[4]) / palm
        if dist(lm[12], lm[9]) / palm < self.cfg.pinch_min_reach:
            rr = max(rr, 1.0)          # curled middle finger resting on thumb is not a pinch
        return rl, rr

    def _smooth(self, prev, raw):
        a = self.cfg.pinch_smooth
        return raw if prev is None else a * raw + (1 - a) * prev

    def _lookback_pos(self, now):
        target, pos = now - self.cfg.aim_lookback_seconds, None
        for t, x, y in self.history:
            if t > target:
                break
            pos = (x, y)
        return pos

    # ---------- calibration ----------
    def start_calibration(self):
        self.release_all()
        self.calib = {"step": 0, "t0": None, "samples": [[], [], []]}
        log.info("Calibration started.")

    def cancel_calibration(self, now):
        self.calib = None
        self.cfg.calibrated = True       # skipped on purpose: do not ask again
        self._save_cfg()
        self._flash("CALIBRATION SKIPPED", ORANGE, now, 1.5)

    def _calibrate(self, raw_l, raw_r, now, st):
        c = self.calib
        if c["t0"] is None:
            c["t0"] = now
        el = now - c["t0"]
        st.mode, st.color = "CALIBRATING", CYAN
        title = f"Calibration {c['step'] + 1}/3"
        if el < self.CALIB_LEAD:
            st.banner = [title, self.CALIB_STEPS[c["step"]], f"Get ready... {math.ceil(self.CALIB_LEAD - el)}"]
        else:
            c["samples"][c["step"]].append((raw_l, raw_r))
            st.banner = [title, self.CALIB_STEPS[c["step"]], "Hold still..."]
            if el >= self.CALIB_LEAD + self.CALIB_SAMPLE:
                c["step"] += 1
                c["t0"] = None
                if c["step"] >= 3:
                    self._finish_calibration(now)
        return st

    def _finish_calibration(self, now):
        cfg, smp = self.cfg, self.calib["samples"]
        self.calib = None
        if min(len(x) for x in smp) < 10:
            log.warning("Calibration failed: too few hand frames.")
            self._flash("CALIBRATION FAILED: hand lost", ORANGE, now, 3.0)
            return
        relax, left, right = (np.array(x) for x in smp)
        o_l, o_r = np.percentile(relax[:, 0], 20), np.percentile(relax[:, 1], 20)
        p_l, p_r = float(np.median(left[:, 0])), float(np.median(right[:, 1]))
        g_l, g_r = o_l - p_l, o_r - p_r
        log.info("Calibration: relaxed L=%.2f R=%.2f | pinch L=%.2f R=%.2f", o_l, o_r, p_l, p_r)
        if g_l < 0.2 or g_r < 0.2:
            log.warning("Calibration failed: pinch too close to relaxed hand.")
            self._flash("FAILED: pinch tighter, open wider. Press C", ORANGE, now, 4.0)
            return
        cfg.pinch_threshold_closed = round(p_l + 0.30 * g_l, 3)
        cfg.pinch_threshold_open = round(p_l + 0.55 * g_l, 3)
        cfg.freeze_threshold_left = round(p_l + 0.65 * g_l, 3)
        cfg.palm_rest_pinches = cfg.freeze_threshold_left
        cfg.right_click_threshold = round(p_r + 0.30 * g_r, 3)
        cfg.freeze_threshold_right = round(p_r + 0.60 * g_r, 3)
        cfg.calibrated = True
        cfg.validate()
        self._save_cfg()
        self._flash("CALIBRATED", GREEN, now, 2.0)
        log.info("Thresholds saved: closed=%.2f open=%.2f freezeL=%.2f rightclick=%.2f freezeR=%.2f",
                 cfg.pinch_threshold_closed, cfg.pinch_threshold_open, cfg.freeze_threshold_left,
                 cfg.right_click_threshold, cfg.freeze_threshold_right)

    # ---------- main update ----------
    def update(self, lm, now, cw, ch) -> Status:
        cfg = self.cfg
        self.info = {}

        if not self.enabled and self.calib is None:
            self.release_all()
            return Status("PAUSED - F8 / SPACE", YELLOW)

        if lm is None:
            if self.last_seen is not None and now - self.last_seen > cfg.lost_grace_seconds:
                self.release_all()
                self._reset_state()
                self.last_seen = None
            st = Status("NO HAND", GREY)
            if self.calib is not None:
                self.calib["t0"] = None                       # restart this step
                self.calib["samples"][self.calib["step"]].clear()
                st.banner = ["Show your hand to the camera"]
            return st

        if self.last_seen is None or now - self.last_seen > cfg.lost_grace_seconds:
            self.release_all()
            self._reset_state()
        self.last_seen = now
        self.hand_frames += 1

        palm = self._palm_size(lm)
        self.info["palm"] = palm
        st = Status()
        if palm < cfg.min_palm_size:
            st.mode, st.color = "MOVE CLOSER", ORANGE
            return st

        # bad single frame from tracker: skip it (max 2 in a row)
        anchor = self._anchor(lm)
        if self.last_anchor is not None and self.skipped < 2 \
                and dist(anchor, self.last_anchor) / palm > cfg.max_jump_palms:
            self.skipped += 1
            st.mode, st.color = "TRACK GLITCH", ORANGE
            return st
        self.skipped, self.last_anchor = 0, anchor
        self.info["anchor"] = anchor

        raw_l, raw_r = self._raw_ratios(lm, palm)
        self.info["raw_L"], self.info["raw_R"] = raw_l, raw_r
        if self.calib is not None:
            return self._calibrate(raw_l, raw_r, now, st)

        self.left_r = self._smooth(self.left_r, raw_l)
        self.right_r = self._smooth(self.right_r, raw_r)
        left, right = self.left_r, self.right_r

        if self.hand_frames <= cfg.acquire_frames:
            self._update_fingers(lm)
            st.mode, st.color = "ACQUIRING", GREY
            return st

        # finger nearest thumb wins. Neighbour finger must not fire too.
        if right < left:
            left = max(left, 1.0)
        elif left < right:
            right = max(right, 1.0)
        if not self.right_armed and right > cfg.freeze_threshold_right:
            self.right_armed = True
        self.info["L"], self.info["R"] = left, right

        # ----- pose -----
        idx, mid, ring, pnk = self._update_fingers(lm)
        self.info["fingers"] = "".join("1" if f else "0" for f in self.fstate)
        pinch_near = left < cfg.freeze_threshold_left or right < cfg.freeze_threshold_right
        if pinch_near:
            raw_pose = self.MOVE                   # pinch always wins, at once
        elif idx and mid and ring and pnk and self._thumb_out(lm):
            raw_pose = self.REST
        elif idx and mid and not ring and not pnk:
            raw_pose = self.SCROLL
        else:
            raw_pose = self.MOVE
        self._update_pose(raw_pose, pinch_near, now)

        if self.pose == self.REST:
            st.mode, st.color = "REST (PAUSED)", YELLOW
        elif raw_pose == self.REST and self.pose == self.MOVE and not self.is_pinching and not self.is_dragging:
            st.mode, st.color = "HOLD TO PAUSE", YELLOW      # cursor waits, no sliding
            self._rebase_pending = True
        elif self.pose == self.SCROLL:
            st.mode, st.color = "SCROLLING", CYAN
            st.scroll_pts = (lm[8], lm[12])
            self._do_scroll((lm[8][1] + lm[12][1]) / 2.0, now)
        else:
            self._do_move(anchor, lm, left, right, now, cw, ch, st)

        if now < self.flash_until:
            st.mode, st.color = self.flash_text, self.flash_color
        if self.show_debug:
            st.debug = [f"L {left:.2f} (click<{cfg.pinch_threshold_closed:.2f} freeze<{cfg.freeze_threshold_left:.2f})",
                        f"R {right:.2f} (click<{cfg.right_click_threshold:.2f} freeze<{cfg.freeze_threshold_right:.2f})",
                        f"fingers IMRP {''.join('1' if f else '0' for f in self.fstate)}  pose {self.pose}",
                        f"palm {palm:.0f}px  pinch {int(self.is_pinching)}  drag {int(self.is_dragging)}"]
        return st

    def _update_pose(self, raw, immediate, now):
        if raw == self.pose:
            self._cand, self._cand_n = raw, 0
            return
        if raw == self._cand:
            self._cand_n += 1
        else:
            self._cand, self._cand_n, self._cand_since = raw, 1, now
        need = 1 if immediate else (2 if raw == self.MOVE else self.cfg.mode_confirm_frames)
        if self._cand_n < need:
            return
        if raw == self.REST and now - self._cand_since < self.cfg.rest_hold_seconds:
            return
        old, self.pose = self.pose, raw
        self._scroll_reset()
        if raw != self.MOVE:
            self.release_all()
        elif old != self.MOVE:
            self._rebase_pending = True        # resume from where cursor is (like lifting a mouse)

    # ---------- actions ----------
    def _do_scroll(self, y, now):
        cfg = self.cfg
        s = y if self._sc_s is None else 0.5 * y + 0.5 * self._sc_s
        self._sc_s = s
        if self._sc_ref is None:
            self._sc_ref = s
            return
        dy = self._sc_ref - s
        if not self._sc_engaged:
            if abs(dy) < cfg.deadzone_scroll_px:
                return
            self._sc_engaged, self._sc_idle = True, now
        if abs(dy) < 0.8:                      # fingers nearly still
            if now - self._sc_idle > 0.25:
                self._sc_engaged, self._sc_ref, self._sc_acc = False, s, 0.0
            return
        self._sc_idle = now
        self._sc_acc += dy * cfg.scroll_gain * (-1 if cfg.scroll_invert else 1)
        amount = int(self._sc_acc)
        if amount:
            self.inp.scroll(amount)
            self._sc_acc -= amount
        self._sc_ref = s

    def _left_click(self, now):
        if self.last_click_pos and now - self.last_click_time <= self.cfg.double_click_window:
            self.inp.move(*self.last_click_pos)     # same spot so Windows sees a double click
        self.inp.click()
        self.last_click_time, self.last_click_pos = now, self.inp.get_pos()

    def _do_move(self, anchor, lm, left, right, now, cw, ch, st: Status):
        cfg = self.cfg
        st.mode, st.color = "CURSOR MOVE", GREEN
        st.anchor = anchor
        st.left_pinch, st.right_pinch = (lm[8], lm[4]), (lm[12], lm[4])

        about_to_click = left < cfg.freeze_threshold_left or right < cfg.freeze_threshold_right
        if not about_to_click or self.is_dragging:
            if self.aim_locked:
                self.aim_locked = False
                self._rebase_pending = True        # hand drifted while pinching: keep cursor, no jump
            mx, my = self._to_screen(anchor[0], anchor[1], cw, ch)
            if self._rebase_pending:
                cx, cy = self.inp.get_pos()
                self.offset = [cx - mx, cy - my]
                self._rebase_pending = False
                self._reseed(now)
            elif self._last_mapped is not None:
                mag = math.hypot(self.offset[0], self.offset[1])
                if mag > 0.0:                      # offset fades only while the hand moves
                    moved = math.hypot(mx - self._last_mapped[0], my - self._last_mapped[1])
                    k = max(0.0, 1.0 - 0.3 * moved / mag)
                    self.offset = [self.offset[0] * k, self.offset[1] * k]
            self._last_mapped = (mx, my)
            tx = min(max(mx + self.offset[0], 0.0), self.screen_w - 1)
            ty = min(max(my + self.offset[1], 0.0), self.screen_h - 1)
            x, y = self.stab.process(tx, ty, now)
            self.inp.move(x, y)
            self.history.append((now, x, y))
        else:
            if not self.aim_locked:                # snap back to where the hand was aiming
                self.aim_locked = True
                back = self._lookback_pos(now)
                if back:
                    self.inp.move(*back)
            st.mode, st.color = "AIM LOCK", ORANGE

        # ----- left pinch: click / drag (debounced) -----
        thr = cfg.pinch_threshold_open if self.is_pinching else cfg.pinch_threshold_closed
        want = left < thr
        if want:
            self._on_n, self._off_n = self._on_n + 1, 0
        else:
            self._on_n, self._off_n = 0, self._off_n + 1

        if not self.is_pinching:
            if self._on_n >= cfg.pinch_confirm_frames:
                self.is_pinching, self.pinch_start = True, now
        else:
            if want and not self.is_dragging and now - self.pinch_start > cfg.drag_hold_seconds:
                self.inp.left_down()
                self.is_dragging = True
            if not want and self._off_n >= cfg.pinch_confirm_frames:
                if self.is_dragging:
                    self.inp.left_up()
                    self.is_dragging = False
                else:
                    self._left_click(now)
                    self._flash("LEFT CLICK!", RED, now)
                self.is_pinching = False
        if self.is_dragging:
            st.mode, st.color = "DRAGGING", RED

        # ----- right pinch: one click per pinch -----
        if (right < cfg.right_click_threshold and self.right_armed
                and not self.is_pinching and not self.is_dragging
                and now - self.last_right_time > cfg.right_click_cooldown_seconds):
            self.inp.right_click()
            self.last_right_time, self.right_armed = now, False
            self._flash("RIGHT CLICK!", BLUE, now)

    # ---------- drawing ----------
    def draw(self, frame, lm, st: Status, fps):
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = self.active_rect(w, h)
        cv2.rectangle(frame, (int(x0), int(y0)), (int(x1), int(y1)), (180, 105, 255), 2)

        if lm is not None and self.cfg.draw_skeleton:
            self.tracker.draw(frame, lm)
        if st.anchor:
            p = (int(st.anchor[0]), int(st.anchor[1]))
            cv2.circle(frame, p, 8, st.color, cv2.FILLED)
            cv2.circle(frame, p, 14, st.color, 2)
        if st.mode in ("DRAGGING", "LEFT CLICK!") and st.left_pinch:
            a, b = st.left_pinch
            cv2.line(frame, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), RED, 3)
        if st.scroll_pts:
            for q in st.scroll_pts:
                cv2.circle(frame, (int(q[0]), int(q[1])), 9, CYAN, cv2.FILLED)

        overlay = frame.copy()
        cv2.rectangle(overlay, (10, 10), (400, 85), (20, 20, 20), -1)
        if self.cfg.show_help:
            cv2.rectangle(overlay, (10, h - 112), (w - 10, h - 10), (20, 20, 20), -1)
        if st.banner:
            cv2.rectangle(overlay, (20, h // 2 - 70), (w - 20, h // 2 + 70), (20, 20, 20), -1)
        cv2.addWeighted(overlay, 0.75, frame, 0.25, 0, frame)
        cv2.putText(frame, f"MODE: {st.mode}", (22, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.7, st.color, 2, cv2.LINE_AA)
        cv2.putText(frame, f"FPS {int(fps)} | F8 on/off  C calibrate  D debug  Q quit", (22, 72),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (210, 210, 210), 1, cv2.LINE_AA)
        if st.banner:
            for i, t in enumerate(st.banner):
                cv2.putText(frame, t, (36, h // 2 - 30 + i * 36), cv2.FONT_HERSHEY_SIMPLEX,
                            0.8 if i == 1 else 0.65, CYAN if i == 1 else (240, 240, 240), 2, cv2.LINE_AA)
        elif self.cfg.show_help:
            lines = ["Pinch index+thumb: click (hold = drag, twice = double click)",
                     "Pinch middle+thumb: right click",
                     "Index+middle up (ring, pinky down): scroll",
                     "Open whole hand: pause cursor   |   Keep hand inside purple box"]
            for i, t in enumerate(lines):
                cv2.putText(frame, t, (22, h - 88 + i * 24), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (230, 230, 230), 1, cv2.LINE_AA)
        if st.debug:
            for i, t in enumerate(st.debug):
                cv2.putText(frame, t, (22, 108 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                            (0, 255, 255), 1, cv2.LINE_AA)

    # ---------- loop ----------
    def run(self):
        cam = CameraStream(self.cfg).start()
        win = "AirGesture Pro"
        cv2.namedWindow(win, cv2.WINDOW_AUTOSIZE)
        try:
            cv2.setWindowProperty(win, cv2.WND_PROP_TOPMOST, 1)
        except Exception:
            pass

        if not self.cfg.calibrated:
            self.start_calibration()       # first run: learn this user's pinch
        last_id, prev, fps, shown, f8_prev = 0, time.perf_counter(), 0.0, False, False
        log.info("Running. F8 = on/off, C = calibrate, Q = quit. Control is %s.",
                 "ON" if self.enabled else "OFF (press F8)")
        try:
            while True:
                last_id, frame = cam.read(last_id)
                f8 = self.inp.key_pressed(VK_F8)
                if f8 and not f8_prev:
                    self.toggle()
                f8_prev = f8

                if frame is not None:
                    frame = cv2.flip(frame, 1)
                    h, w = frame.shape[:2]
                    lm = self.tracker.find(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), w, h)
                    now = time.perf_counter()
                    st = self.update(lm, now, w, h)
                    fps = 0.9 * fps + 0.1 / max(now - prev, 1e-6)
                    prev = now
                    self.draw(frame, lm, st, fps)
                    if self.trace:
                        self.trace.row(now, st, dict(self.info, pose=self.pose), self.inp.get_pos(),
                                       fps, self.is_pinching, self.is_dragging)
                    cv2.imshow(win, frame)
                    shown = True

                key = cv2.waitKey(1) & 0xFF
                if key == 27 and self.calib is not None:
                    self.cancel_calibration(time.perf_counter())
                elif key in (ord("q"), 27):
                    break
                elif key in (ord(" "), ord("p")):
                    self.toggle()
                elif key == ord("h"):
                    self.cfg.show_help = not self.cfg.show_help
                elif key == ord("d"):
                    self.show_debug = not self.show_debug
                elif key == ord("c"):
                    self.start_calibration()
                if shown and cv2.getWindowProperty(win, cv2.WND_PROP_VISIBLE) < 1:
                    break
        finally:
            self.release_all()          # never leave mouse button stuck
            cam.stop()
            self.tracker.close()
            if self.trace:
                self.trace.close()
            cv2.destroyAllWindows()


# ==========================================================
# 7. Entry point
# ==========================================================
def main():
    ap = argparse.ArgumentParser(description="AirGesture Pro - hand mouse")
    ap.add_argument("--config", type=Path, default=HERE / "air_gesture_config.json")
    ap.add_argument("--dry-run", action="store_true", help="track hand but do not touch the real mouse")
    ap.add_argument("--trace", nargs="?", const=str(HERE / "air_trace.csv"), default=None,
                    help="write per-frame CSV for live testing (default air_trace.csv)")
    ap.add_argument("--write-config", action="store_true", help="write full default config and exit")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    if args.write_config or not args.config.exists():
        cfg.save(args.config)
        log.info("Config written: %s", args.config)
        if args.write_config:
            return

    if args.dry_run or not IS_WINDOWS:
        if not IS_WINDOWS:
            log.warning("Not Windows. Dry run only.")
        inp = NullInput()
    else:
        inp = WinInput()

    trace = None
    if args.trace:
        trace = TraceWriter(Path(args.trace), cfg, (inp.screen_w, inp.screen_h))
        inp = TracingInput(inp, trace)
        log.info("Tracing to %s", args.trace)

    try:
        ctrl = AirGestureController(cfg, inp, HandTracker(cfg), args.config)
        ctrl.trace = trace
        ctrl.run()
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    except Exception as exc:
        log.exception("Fatal error")
        if IS_WINDOWS:   # no console when started from shortcut, so show a popup
            ctypes.windll.user32.MessageBoxW(
                0, f"AirGesture Pro could not start:\n\n{exc}\n\nDetails: air_mouse.log",
                "AirGesture Pro", 0x10)
        sys.exit(1)


if __name__ == "__main__":
    main()
