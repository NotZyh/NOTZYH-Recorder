# -*- coding: utf-8 -*-
"""NOTZYH Low-Lag Recorder
Screen recorder for low-end Windows 10 laptops.
Video: FFmpeg (DXGI ddagrab / GDI fallback, Intel Quick Sync or x264 ultrafast)
Audio: WASAPI (microphone + loopback system audio) via PyAudioWPatch, muxed to AAC on stop.
"""
import sys
import os
import time
import wave
import shutil
import logging
import threading
import subprocess
import datetime
import collections
from array import array
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

IS_WIN = os.name == "nt"
NOWIN = 0x08000000 if IS_WIN else 0          # CREATE_NO_WINDOW
ABOVE_NORMAL = 0x00008000 if IS_WIN else 0   # ABOVE_NORMAL_PRIORITY_CLASS
APP_NAME = "NOTZYH Low-Lag Recorder"
ACCENT = "#3b9cff"
RED = "#ff3b4e"


# ----------------------------------------------------------------- paths / log
def res_path(name):
    base = getattr(sys, "_MEIPASS", None) or str(Path(__file__).resolve().parent)
    return os.path.join(base, name)


def find_ffmpeg():
    for c in (res_path("ffmpeg.exe"), res_path("ffmpeg/ffmpeg.exe")):
        if os.path.isfile(c):
            return c
    return shutil.which("ffmpeg")


DATA_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "NOTZYH_LowLag_Recorder"
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    DATA_DIR = Path.home()
LOG_FILE = DATA_DIR / "recorder.log"


class LogBus(QtCore.QObject):
    line = QtCore.Signal(str)


BUS = LogBus()


class BusHandler(logging.Handler):
    def emit(self, record):
        try:
            BUS.line.emit(self.format(record))
        except Exception:
            pass


logger = logging.getLogger("notzyh")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
try:
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=512_000, backupCount=2, encoding="utf-8")
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)
except Exception:
    pass
_bh = BusHandler()
_bh.setFormatter(_fmt)
logger.addHandler(_bh)

# ----------------------------------------------------------------- presets
PROFILES = {
    "Low-End Laptop Mode": dict(res="1280x720", fps=30, quality="Fast (stability)", bitrate=2500, encoder="auto"),
    "Balanced Mode": dict(res="1280x720", fps=30, quality="Balanced", bitrate=4500, encoder="auto"),
    "High Quality Mode": dict(res="1920x1080", fps=30, quality="Quality", bitrate=9000, encoder="auto"),
}
RESOLUTIONS = [("1280x720  (720p)", "1280x720"), ("1920x1080  (1080p)", "1920x1080")]
FPS_OPTIONS = [30, 60]
# quality -> (x264 preset, qsv preset)
QUALITY = {
    "Fast (stability)": ("ultrafast", "veryfast"),
    "Balanced": ("superfast", "faster"),
    "Quality": ("veryfast", "medium"),
}
ENCODERS = [("Auto (detect best)", "auto"), ("Intel Quick Sync (H.264)", "qsv"), ("CPU x264 ultrafast", "x264")]


# ----------------------------------------------------------------- capability detection
def _quiet(args, timeout=20):
    try:
        r = subprocess.run(args, capture_output=True, timeout=timeout, creationflags=NOWIN)
        return r.returncode == 0
    except Exception:
        return False


def detect_caps(ff):
    caps = {"qsv": False, "ddagrab": False, "version": "unknown"}
    try:
        v = subprocess.run([ff, "-version"], capture_output=True, timeout=10, creationflags=NOWIN)
        caps["version"] = v.stdout.decode(errors="ignore").splitlines()[0]
        enc = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, timeout=10,
                             creationflags=NOWIN).stdout.decode(errors="ignore")
        if "h264_qsv" in enc:
            caps["qsv"] = _quiet([ff, "-hide_banner", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                                  "-frames:v", "10", "-vf", "format=nv12", "-c:v", "h264_qsv", "-f", "null", "-"])
        caps["ddagrab"] = _quiet([ff, "-hide_banner", "-f", "lavfi", "-i", "ddagrab=framerate=30",
                                  "-frames:v", "5", "-vf", "hwdownload,format=bgra", "-f", "null", "-"])
    except Exception as e:
        logger.info("Capability detection error: %s", e)
    return caps


# ----------------------------------------------------------------- audio
class AudioCapture:
    """Writes one WASAPI device to a WAV file. Silence-fills gaps (loopback sends nothing while silent)."""

    def __init__(self, pa, pyaudio, info, path, name, vol, on_level):
        self.pa, self.pyaudio, self.info = pa, pyaudio, info
        self.path, self.name, self.vol, self.on_level = path, name, vol, on_level
        self.rate = int(info["defaultSampleRate"])
        self.ch = max(1, min(int(info["maxInputChannels"]), 2))
        self.written = 0
        self.lock = threading.Lock()
        self.last_emit = 0.0
        self.stream = None
        self.t0 = 0.0
        self.wf = None

    def _write_silence(self, frames):
        chunk = self.rate  # 1 s per write
        zero = b"\x00" * (chunk * self.ch * 2)
        while frames > 0:
            n = min(frames, chunk)
            self.wf.writeframes(zero[: n * self.ch * 2])
            self.written += n
            frames -= n

    def _pad(self, now, frames_in=0, threshold=0.15):
        expected = int((now - self.t0) * self.rate)
        missing = expected - self.written - frames_in
        if missing > int(self.rate * threshold):
            self._write_silence(missing)

    def _cb(self, data, frame_count, ti, status):
        now = time.monotonic()
        with self.lock:
            if self.wf is None:
                return (None, self.pyaudio.paComplete)
            self._pad(now, frame_count)
            self.wf.writeframes(data)
            self.written += frame_count
        if now - self.last_emit > 0.066:
            self.last_emit = now
            try:
                a = array("h")
                a.frombytes(data)
                peak = max((abs(x) for x in a[::8]), default=0) / 32768.0
                self.on_level(self.name, min(1.0, peak * self.vol))
            except Exception:
                pass
        return (None, self.pyaudio.paContinue)

    def start(self):
        self.wf = wave.open(self.path, "wb")
        self.wf.setnchannels(self.ch)
        self.wf.setsampwidth(2)
        self.wf.setframerate(self.rate)
        self.t0 = time.monotonic()
        try:
            self.stream = self.pa.open(format=self.pyaudio.paInt16, channels=self.ch, rate=self.rate, input=True,
                                       input_device_index=int(self.info["index"]), frames_per_buffer=2048,
                                       stream_callback=self._cb)
        except Exception:
            self.wf.close()
            self.wf = None
            raise

    def stop(self, end_t):
        try:
            if self.stream:
                self.stream.stop_stream()
                self.stream.close()
        except Exception as e:
            logger.info("Audio stream close (%s): %s", self.name, e)
        with self.lock:
            if self.wf:
                try:
                    self._pad(end_t, 0, 0.0)
                    self.wf.close()
                except Exception as e:
                    logger.info("WAV close (%s): %s", self.name, e)
                self.wf = None


def list_audio(pa, pyaudio):
    """Returns (mics, loopbacks, default_mic_index, default_loop_index) using WASAPI only."""
    mics, loops, dmic, dloop = [], [], None, None
    try:
        wasapi = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
    except Exception:
        return mics, loops, dmic, dloop
    for i in range(pa.get_device_count()):
        try:
            d = pa.get_device_info_by_index(i)
        except Exception:
            continue
        if d.get("hostApi") != wasapi["index"] or d.get("maxInputChannels", 0) < 1:
            continue
        (loops if d.get("isLoopbackDevice") else mics).append(d)
    dmic = wasapi.get("defaultInputDevice")
    try:
        out = pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for d in loops:
            if d["name"].startswith(out["name"]):
                dloop = d["index"]
                break
    except Exception:
        pass
    return mics, loops, dmic, dloop


# ----------------------------------------------------------------- recorder
class Recorder(QtCore.QObject):
    sig_level = QtCore.Signal(str, float)
    sig_stats = QtCore.Signal(int, int)
    sig_done = QtCore.Signal(bool, str)
    sig_failed = QtCore.Signal(str)
    sig_fallback = QtCore.Signal()

    def __init__(self, ff):
        super().__init__()
        self.ff = ff
        self.busy = False
        self.stopping = False
        self.proc = None
        self.audio = []
        self.video_t0 = None
        self.tail = collections.deque(maxlen=40)
        self.cfg = None

    # ---- video command
    def _build_cmd(self, method):
        c = self.cfg
        w, h, fps, br = c["w"], c["h"], c["fps"], c["bitrate"]
        pix = "nv12" if c["encoder"] == "qsv" else "yuv420p"
        scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=fast_bilinear,"
                 f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format={pix}")
        cmd = [self.ff, "-hide_banner", "-y", "-loglevel", "warning", "-nostats",
               "-progress", "pipe:1", "-stats_period", "0.2"]
        if method == "ddagrab":
            cmd += ["-f", "lavfi", "-i", f"ddagrab=framerate={fps}:draw_mouse=1"]
            vf = "hwdownload,format=bgra," + scale
        else:
            cmd += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1", "-i", "desktop"]
            vf = scale
        cmd += ["-vf", vf, "-an", "-r", str(fps), "-fps_mode", "cfr"]
        gop = str(fps * 2)
        xq, qq = QUALITY[c["quality"]]
        if c["encoder"] == "qsv":
            cmd += ["-c:v", "h264_qsv", "-preset", qq, "-b:v", f"{br}k", "-maxrate", f"{int(br * 1.25)}k",
                    "-g", gop, "-look_ahead", "0"]
        else:
            cmd += ["-c:v", "libx264", "-preset", xq, "-tune", "zerolatency", "-b:v", f"{br}k",
                    "-maxrate", f"{int(br * 1.25)}k", "-bufsize", f"{br * 2}k", "-g", gop, "-threads", "2"]
        cmd += ["-f", "matroska", self.tmp_video]
        return cmd

    def _launch(self, method):
        self.method = method
        self.video_t0 = None
        self.got_frame = False
        self.tail.clear()
        cmd = self._build_cmd(method)
        logger.info("Video capture: %s | encoder: %s", method, self.cfg["encoder"])
        logger.info("FFmpeg: %s", " ".join(cmd))
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                creationflags=NOWIN | ABOVE_NORMAL)
        self.proc = proc
        threading.Thread(target=self._read_out, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(proc,), daemon=True).start()

    def _read_err(self, proc):
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode(errors="ignore").strip()
                if line:
                    self.tail.append(line)
                    logger.info("ffmpeg: %s", line)
        except Exception:
            pass

    def _read_out(self, proc):
        frames, drops = 0, 0
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode(errors="ignore").strip()
                if line.startswith("frame="):
                    try:
                        frames = int(line[6:])
                    except ValueError:
                        pass
                    if frames >= 1 and not self.got_frame:
                        self.got_frame = True
                        self.video_t0 = time.monotonic()
                elif line.startswith("drop_frames="):
                    try:
                        drops = int(line[12:])
                    except ValueError:
                        pass
                elif line.startswith("progress="):
                    self.sig_stats.emit(frames, drops)
        except Exception:
            pass
        try:
            proc.wait()
        except Exception:
            pass
        if proc is not self.proc or self.stopping:
            return
        # ffmpeg ended on its own
        if not self.got_frame and self.method == "ddagrab":
            logger.info("DXGI capture failed, falling back to GDI capture.")
            self.sig_fallback.emit()
        elif not self.got_frame:
            self._abort("FFmpeg stopped before recording any video.\n\n" + "\n".join(list(self.tail)[-8:]))
        else:
            logger.info("FFmpeg ended unexpectedly; salvaging recording.")
            self.stopping = True
            self._stop_audio()
            threading.Thread(target=self._finalize, args=(proc, True), daemon=True).start()

    def retry_gdigrab(self):
        try:
            self._launch("gdigrab")
        except Exception as e:
            self._abort(f"Could not start FFmpeg: {e}")

    def _abort(self, msg):
        self.stopping = True
        self._stop_audio()
        self.busy = False
        self.sig_failed.emit(msg)

    # ---- public API
    def start(self, cfg, pa, pyaudio):
        if self.busy:
            raise RuntimeError("A recording is already running.")
        self.cfg = cfg
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = Path(cfg["outdir"])
        out.mkdir(parents=True, exist_ok=True)
        self.final = str(out / f"NOTZYH_{stamp}.mp4")
        self.tmp_video = str(out / f".NOTZYH_{stamp}.video.mkv")
        self.audio = []
        self.stopping = False
        # audio first, so any device problem aborts cleanly before ffmpeg starts
        try:
            for key, label in (("mic", "mic"), ("sys", "sys")):
                a = cfg.get(key)
                if a:
                    path = str(out / f".NOTZYH_{stamp}.{label}.wav")
                    cap = AudioCapture(pa, pyaudio, a["info"], path, label, a["vol"], self.sig_level.emit)
                    cap.start()
                    self.audio.append(cap)
                    logger.info("Audio %s: %s @ %d Hz, %d ch", label, a["info"]["name"], cap.rate, cap.ch)
            self.busy = True
            self._launch("ddagrab" if cfg["caps"]["ddagrab"] else "gdigrab")
        except Exception:
            self.busy = False
            self._stop_audio()
            raise
        logger.info("Recording started -> %s", self.final)

    def _stop_audio(self):
        end = time.monotonic()
        for a in self.audio:
            a.stop(end)

    def stop(self):
        if not self.busy or self.stopping:
            return
        self.stopping = True
        logger.info("Stopping recording...")
        self._stop_audio()
        threading.Thread(target=self._finalize, args=(self.proc, False), daemon=True).start()

    def _finalize(self, proc, unexpected):
        try:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=25)
            except subprocess.TimeoutExpired:
                logger.info("FFmpeg did not exit in time; terminating.")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            ok, msg = self._mux()
            if ok and unexpected:
                msg += "\n(FFmpeg ended early; the saved video may be shorter than expected.)"
            self.busy = False
            self.sig_done.emit(ok, msg)
        except Exception as e:
            logger.info("Finalize error: %s", e)
            self.busy = False
            self.sig_done.emit(False, f"Finalizing failed: {e}\nRaw files kept in {self.cfg['outdir']}")

    def _mux(self):
        tmp = self.tmp_video
        if not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
            return False, "No video data was recorded. Audio files (if any) were kept in the output folder."
        cmd = [self.ff, "-hide_banner", "-y", "-loglevel", "error", "-i", tmp]
        filters, labels = [], []
        for k, a in enumerate(self.audio):
            off = (self.video_t0 - a.t0) if self.video_t0 else 0.0
            ms = 0
            if off < 0:
                ms, off = int(-off * 1000), 0.0
            cmd += ["-ss", f"{off:.3f}", "-i", a.path]
            f = f"[{k + 1}:a]aresample=48000,aformat=channel_layouts=stereo,volume={a.vol:.2f}"
            if ms > 10:
                f += f",adelay={ms}|{ms}"
            filters.append(f + f"[a{k}]")
            labels.append(f"[a{k}]")
        if self.audio:
            if len(self.audio) == 2:
                filters.append("[a0][a1]amix=inputs=2:duration=longest:normalize=0[aout]")
                lab = "[aout]"
            else:
                lab = labels[0]
            cmd += ["-filter_complex", ";".join(filters), "-map", "0:v:0", "-map", lab,
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        else:
            cmd += ["-map", "0:v:0", "-c:v", "copy"]
        cmd += ["-movflags", "+faststart", self.final]
        logger.info("Finalizing MP4...")
        r = subprocess.run(cmd, capture_output=True, creationflags=NOWIN)
        if r.returncode != 0 or not os.path.isfile(self.final):
            err = r.stderr.decode(errors="ignore").strip()
            logger.info("Mux failed: %s", err)
            return False, ("Could not finalize the MP4. Your raw recording was KEPT in the output folder "
                           f"(hidden files starting with .NOTZYH_).\n\n{err[-600:]}")
        for p in [tmp] + [a.path for a in self.audio]:
            try:
                os.remove(p)
            except OSError:
                pass
        logger.info("Saved: %s", self.final)
        return True, self.final


# ----------------------------------------------------------------- custom widgets
class Switch(QtWidgets.QAbstractButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setFixedSize(46, 26)
        self._p = 0.0
        self._anim = QtCore.QPropertyAnimation(self, b"progress", self)
        self._anim.setDuration(140)
        self.toggled.connect(self._go)

    def _go(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._p)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def getp(self):
        return self._p

    def setp(self, v):
        self._p = v
        self.update()

    progress = QtCore.Property(float, getp, setp)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        off, on = QtGui.QColor("#3a3f4a"), QtGui.QColor(ACCENT)
        t = self._p
        col = QtGui.QColor(int(off.red() + (on.red() - off.red()) * t), int(off.green() + (on.green() - off.green()) * t),
                           int(off.blue() + (on.blue() - off.blue()) * t))
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 12, 12)
        p.setBrush(QtGui.QColor("#ffffff"))
        x = 5 + t * 20
        p.drawEllipse(QtCore.QRectF(x, 5, 16, 16))


class LevelMeter(QtWidgets.QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.v = 0.0
        self.setFixedHeight(8)
        self.setMinimumWidth(80)

    def push(self, v):
        self.v = max(v, self.v * 0.7)
        self.update()

    def decay(self):
        if self.v > 0.001:
            self.v *= 0.82
            self.update()

    def reset(self):
        self.v = 0.0
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(self.rect())
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 18))
        p.drawRoundedRect(r, 4, 4)
        if self.v > 0.002:
            w = max(8.0, r.width() * min(1.0, self.v ** 0.6))
            g = QtGui.QLinearGradient(0, 0, r.width(), 0)
            g.setColorAt(0, QtGui.QColor("#2fd6a0"))
            g.setColorAt(0.7, QtGui.QColor(ACCENT))
            g.setColorAt(1, QtGui.QColor(RED))
            p.setBrush(g)
            p.drawRoundedRect(QtCore.QRectF(0, 0, w, r.height()), 4, 4)


class StatusChip(QtWidgets.QWidget):
    COLORS = {"IDLE": "#7d8696", "READY": "#2fd6a0", "RECORDING": RED, "FINALIZING": "#ffb020",
              "ERROR": "#ff6b6b", "DETECTING": ACCENT}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "DETECTING"
        self.phase = 0
        self.setFixedHeight(34)
        self.setMinimumWidth(150)

    def set_state(self, s):
        self.state = s
        self.update()

    def tick(self):
        self.phase = (self.phase + 1) % 10
        self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        col = QtGui.QColor(self.COLORS.get(self.state, "#7d8696"))
        bg = QtGui.QColor(col)
        bg.setAlpha(28)
        p.setPen(QtGui.QPen(QtGui.QColor(col.red(), col.green(), col.blue(), 90), 1))
        p.setBrush(bg)
        p.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1), 17, 17)
        pulse = self.state in ("RECORDING", "FINALIZING")
        a = 255 if not pulse else int(120 + 135 * abs(5 - self.phase) / 5)
        c2 = QtGui.QColor(col)
        c2.setAlpha(a)
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(c2)
        p.drawEllipse(QtCore.QRectF(14, 11, 12, 12))
        p.setPen(col)
        f = QtGui.QFont("Segoe UI Semibold", 9)
        f.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.2)
        p.setFont(f)
        p.drawText(QtCore.QRectF(34, 0, self.width() - 40, self.height()),
                   QtCore.Qt.AlignVCenter | QtCore.Qt.AlignLeft, self.state)


class Background(QtWidgets.QWidget):
    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        g = QtGui.QLinearGradient(0, 0, self.width(), self.height())
        g.setColorAt(0, QtGui.QColor("#1b1e25"))
        g.setColorAt(1, QtGui.QColor("#0f1115"))
        p.fillRect(self.rect(), g)
        rg = QtGui.QRadialGradient(self.width() * 0.15, -40, self.width() * 0.6)
        rg.setColorAt(0, QtGui.QColor(59, 156, 255, 46))
        rg.setColorAt(1, QtGui.QColor(59, 156, 255, 0))
        p.fillRect(self.rect(), rg)
        rg2 = QtGui.QRadialGradient(self.width() * 0.95, self.height() * 1.05, self.width() * 0.5)
        rg2.setColorAt(0, QtGui.QColor(255, 59, 78, 26))
        rg2.setColorAt(1, QtGui.QColor(255, 59, 78, 0))
        p.fillRect(self.rect(), rg2)


QSS = f"""
* {{ font-family: 'Segoe UI'; color: #e8ecf3; }}
QLabel {{ background: transparent; }}
QFrame#card {{ background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.08); border-radius: 14px; }}
QLabel#cardTitle {{ color: #8fb8ff; font-size: 11px; font-weight: 600; letter-spacing: 2px; }}
QLabel#muted {{ color: #8b93a3; font-size: 11px; }}
QLabel#tileLabel {{ color: #8b93a3; font-size: 10px; letter-spacing: 1px; }}
QLabel#tileValue {{ font-size: 15px; font-weight: 600; }}
QLabel#timer {{ font-family: 'Segoe UI Light'; font-size: 66px; color: #ffffff; }}
QLabel#brand {{ font-size: 22px; font-weight: 700; letter-spacing: 3px; color: {ACCENT}; }}
QComboBox, QSpinBox {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.10);
    border-radius: 8px; padding: 6px 10px; min-height: 22px; }}
QComboBox:hover, QSpinBox:hover {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{ background: #20242c; border: 1px solid #343a46; selection-background-color: {ACCENT};
    outline: 0; }}
QLineEdit {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.10); border-radius: 8px; padding: 7px 10px; }}
QPushButton {{ background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.10); border-radius: 9px;
    padding: 7px 14px; font-weight: 600; }}
QPushButton:hover {{ background: rgba(59,156,255,0.25); border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #5b6270; background: rgba(255,255,255,0.03); border-color: rgba(255,255,255,0.05); }}
QPushButton#rec {{ font-size: 20px; font-weight: 700; letter-spacing: 3px; color: white; border: none; border-radius: 16px;
    background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ff5a6a, stop:1 #e0162b); }}
QPushButton#rec:hover {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #ff7482, stop:1 #f12036); }}
QPushButton#rec:pressed {{ background: #c41225; }}
QPushButton#rec:disabled {{ background: rgba(255,59,78,0.22); color: rgba(255,255,255,0.45); }}
QPushButton#stop {{ font-size: 16px; font-weight: 700; letter-spacing: 2px; border-radius: 16px;
    background: rgba(255,255,255,0.10); border: 1px solid rgba(255,255,255,0.22); }}
QPushButton#stop:hover {{ background: rgba(255,255,255,0.20); }}
QPlainTextEdit {{ background: rgba(0,0,0,0.30); border: 1px solid rgba(255,255,255,0.06); border-radius: 10px;
    font-family: Consolas; font-size: 11px; color: #a9b4c7; padding: 6px; }}
QSlider::groove:horizontal {{ height: 6px; background: rgba(255,255,255,0.12); border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 3px; }}
QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -5px 0; border-radius: 8px; background: white; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QToolTip {{ background: #20242c; color: white; border: 1px solid #343a46; }}
"""


def make_card(title):
    f = QtWidgets.QFrame()
    f.setObjectName("card")
    lay = QtWidgets.QVBoxLayout(f)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(10)
    if title:
        t = QtWidgets.QLabel(title.upper())
        t.setObjectName("cardTitle")
        lay.addWidget(t)
    return f, lay


class Tile(QtWidgets.QFrame):
    def __init__(self, label):
        super().__init__()
        self.setObjectName("card")
        l = QtWidgets.QVBoxLayout(self)
        l.setContentsMargins(14, 10, 14, 10)
        l.setSpacing(2)
        a = QtWidgets.QLabel(label.upper())
        a.setObjectName("tileLabel")
        self.v = QtWidgets.QLabel("-")
        self.v.setObjectName("tileValue")
        self.v.setWordWrap(True)
        l.addWidget(a)
        l.addWidget(self.v)

    def set(self, t):
        self.v.setText(t)


# ----------------------------------------------------------------- main window
class MainWindow(QtWidgets.QMainWindow):
    caps_ready = QtCore.Signal(dict)

    def __init__(self, ff):
        super().__init__()
        self.ff = ff
        self.caps = None
        self.pa = None
        self.pyaudio = None
        self.t_start = 0.0
        self.settings = QtCore.QSettings("NOTZYH", "LowLagRecorder")
        self.rec = Recorder(ff) if ff else None
        self.setWindowTitle(APP_NAME)
        scr = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        self.resize(min(1180, scr.width() - 40), min(790, scr.height() - 40))
        self.setMinimumSize(900, 600)
        self._build()
        self._init_audio()
        self._load_settings()
        self._wire()
        self.refresh_summary()
        if not ff:
            self._fatal("FFmpeg was not found. Rebuild the app with ffmpeg.exe bundled, or place ffmpeg.exe next to it.")
        else:
            self.caps_ready.connect(self._on_caps)
            threading.Thread(target=lambda: self.caps_ready.emit(detect_caps(ff)), daemon=True).start()
            logger.info("%s started. Detecting encoders...", APP_NAME)

    # ---- UI construction
    def _combo(self, items=None):
        c = QtWidgets.QComboBox()
        c.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToMinimumContentsLengthWithIcon)
        c.setMinimumContentsLength(14)
        for t, d in (items or []):
            c.addItem(t, d)
        return c

    def _row(self, label, widget):
        r = QtWidgets.QHBoxLayout()
        l = QtWidgets.QLabel(label)
        l.setObjectName("muted")
        l.setFixedWidth(82)
        r.addWidget(l)
        r.addWidget(widget, 1)
        return r

    def _build(self):
        bg = Background()
        self.setCentralWidget(bg)
        root = QtWidgets.QHBoxLayout(bg)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(18)

        # ---- left dashboard
        left = QtWidgets.QVBoxLayout()
        left.setSpacing(12)
        head = QtWidgets.QHBoxLayout()
        brand = QtWidgets.QVBoxLayout()
        brand.setSpacing(0)
        b = QtWidgets.QLabel("NOTZYH")
        b.setObjectName("brand")
        s = QtWidgets.QLabel("Low-Lag Recorder  -  built for modest laptops")
        s.setObjectName("muted")
        brand.addWidget(b)
        brand.addWidget(s)
        head.addLayout(brand)
        head.addStretch(1)
        self.chip = StatusChip()
        head.addWidget(self.chip)
        left.addLayout(head)

        hero, hl = make_card(None)
        self.timer_lbl = QtWidgets.QLabel("00:00:00")
        self.timer_lbl.setObjectName("timer")
        self.timer_lbl.setAlignment(QtCore.Qt.AlignCenter)
        hl.addWidget(self.timer_lbl)
        btns = QtWidgets.QHBoxLayout()
        btns.setSpacing(12)
        self.btn_rec = QtWidgets.QPushButton("\u25CF  RECORD")
        self.btn_rec.setObjectName("rec")
        self.btn_rec.setMinimumHeight(76)
        self.btn_rec.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_stop = QtWidgets.QPushButton("\u25A0  STOP")
        self.btn_stop.setObjectName("stop")
        self.btn_stop.setMinimumHeight(76)
        self.btn_stop.setMinimumWidth(150)
        self.btn_stop.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_stop.setEnabled(False)
        btns.addWidget(self.btn_rec, 3)
        btns.addWidget(self.btn_stop, 1)
        hl.addLayout(btns)
        self.msg = QtWidgets.QLabel("Detecting hardware encoders...")
        self.msg.setObjectName("muted")
        self.msg.setAlignment(QtCore.Qt.AlignCenter)
        self.msg.setWordWrap(True)
        hl.addWidget(self.msg)
        left.addWidget(hero)

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        self.t_res, self.t_fps = Tile("Resolution"), Tile("Frame rate")
        self.t_enc, self.t_drop = Tile("Encoder"), Tile("Dropped frames")
        grid.addWidget(self.t_res, 0, 0)
        grid.addWidget(self.t_fps, 0, 1)
        grid.addWidget(self.t_enc, 1, 0)
        grid.addWidget(self.t_drop, 1, 1)
        left.addLayout(grid)

        dest, dl = make_card("Recording destination")
        row = QtWidgets.QHBoxLayout()
        self.dest_lbl = QtWidgets.QLabel("")
        self.dest_lbl.setWordWrap(True)
        self.btn_open = QtWidgets.QPushButton("Open")
        row.addWidget(self.dest_lbl, 1)
        row.addWidget(self.btn_open)
        dl.addLayout(row)
        left.addWidget(dest)

        logc, ll = make_card("Recording log")
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(400)
        ll.addWidget(self.log, 1)
        lrow = QtWidgets.QHBoxLayout()
        self.btn_logfile = QtWidgets.QPushButton("Open log file")
        lrow.addStretch(1)
        lrow.addWidget(self.btn_logfile)
        ll.addLayout(lrow)
        left.addWidget(logc, 1)

        lw = QtWidgets.QWidget()
        lw.setLayout(left)
        lw.setMinimumWidth(420)
        root.addWidget(lw, 5)

        # ---- right settings
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        inner = QtWidgets.QWidget()
        inner.setAttribute(QtCore.Qt.WA_TranslucentBackground)
        right = QtWidgets.QVBoxLayout(inner)
        right.setContentsMargins(0, 0, 6, 0)
        right.setSpacing(12)

        vc, vl = make_card("Video")
        self.cb_profile = self._combo([(k, k) for k in PROFILES] + [("Custom", "custom")])
        self.cb_res = self._combo(RESOLUTIONS)
        self.cb_fps = self._combo([(f"{f} FPS", f) for f in FPS_OPTIONS])
        self.cb_quality = self._combo([(k, k) for k in QUALITY])
        self.cb_enc = self._combo(ENCODERS)
        self.sp_bitrate = QtWidgets.QSpinBox()
        self.sp_bitrate.setRange(500, 40000)
        self.sp_bitrate.setSingleStep(250)
        self.sp_bitrate.setSuffix(" kbps")
        for lab, w in (("Profile", self.cb_profile), ("Resolution", self.cb_res), ("FPS", self.cb_fps),
                       ("Quality", self.cb_quality), ("Encoder", self.cb_enc), ("Bitrate", self.sp_bitrate)):
            vl.addLayout(self._row(lab, w))
        right.addWidget(vc)

        ac, al = make_card("Audio")
        self.sw_mic, self.sw_sys = Switch(), Switch()
        self.cb_mic, self.cb_sys = self._combo(), self._combo()
        self.sl_mic, self.sl_sys = QtWidgets.QSlider(QtCore.Qt.Horizontal), QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.lv_mic, self.lv_sys = LevelMeter(), LevelMeter()
        self.lb_mic, self.lb_sys = QtWidgets.QLabel("100%"), QtWidgets.QLabel("100%")
        for sw, cb, sl, lv, lb, title in ((self.sw_mic, self.cb_mic, self.sl_mic, self.lv_mic, self.lb_mic, "Microphone"),
                                          (self.sw_sys, self.cb_sys, self.sl_sys, self.lv_sys, self.lb_sys, "System audio (WASAPI loopback)")):
            h = QtWidgets.QHBoxLayout()
            h.addWidget(QtWidgets.QLabel(title), 1)
            h.addWidget(sw)
            al.addLayout(h)
            al.addWidget(cb)
            sl.setRange(0, 200)
            sl.setValue(100)
            h2 = QtWidgets.QHBoxLayout()
            vl_ = QtWidgets.QLabel("Volume")
            vl_.setObjectName("muted")
            lb.setObjectName("muted")
            lb.setFixedWidth(40)
            h2.addWidget(vl_)
            h2.addWidget(sl, 1)
            h2.addWidget(lb)
            al.addLayout(h2)
            al.addWidget(lv)
            sep = QtWidgets.QFrame()
            sep.setFixedHeight(4)
            al.addWidget(sep)
        self.btn_refresh = QtWidgets.QPushButton("Refresh audio devices")
        al.addWidget(self.btn_refresh)
        right.addWidget(ac)

        oc, ol = make_card("Output folder")
        self.ed_dir = QtWidgets.QLineEdit()
        self.ed_dir.setReadOnly(True)
        self.btn_browse = QtWidgets.QPushButton("Browse...")
        h = QtWidgets.QHBoxLayout()
        h.addWidget(self.ed_dir, 1)
        h.addWidget(self.btn_browse)
        ol.addLayout(h)
        right.addWidget(oc)
        right.addStretch(1)
        scroll.setWidget(inner)
        scroll.setMinimumWidth(400)
        root.addWidget(scroll, 4)

        self.ui_timer = QtCore.QTimer(self)
        self.ui_timer.setInterval(100)
        self.ui_timer.timeout.connect(self._tick)

    # ---- audio devices
    def _init_audio(self):
        try:
            import pyaudiowpatch as pyaudio
            self.pyaudio = pyaudio
            self.pa = pyaudio.PyAudio()
        except Exception as e:
            logger.info("Audio backend unavailable: %s", e)
            self.pa = None
        self._fill_devices()

    def _fill_devices(self):
        self.cb_mic.clear()
        self.cb_sys.clear()
        if not self.pa:
            for cb, sw in ((self.cb_mic, self.sw_mic), (self.cb_sys, self.sw_sys)):
                cb.addItem("Audio backend unavailable", None)
                cb.setEnabled(False)
                sw.setEnabled(False)
            return
        mics, loops, dmic, dloop = list_audio(self.pa, self.pyaudio)
        for d in mics:
            self.cb_mic.addItem(d["name"], d)
            if d["index"] == dmic:
                self.cb_mic.setCurrentIndex(self.cb_mic.count() - 1)
        for d in loops:
            self.cb_sys.addItem(d["name"], d)
            if d["index"] == dloop:
                self.cb_sys.setCurrentIndex(self.cb_sys.count() - 1)
        if not mics:
            self.cb_mic.addItem("No microphone found", None)
        if not loops:
            self.cb_sys.addItem("No output device found", None)
        logger.info("Audio devices: %d microphone(s), %d loopback output(s)", len(mics), len(loops))

    def _refresh_devices(self):
        if self.rec and self.rec.busy:
            return
        try:
            self.pa.terminate()
        except Exception:
            pass
        self._init_audio()

    # ---- settings
    def _load_settings(self):
        s = self.settings
        prof = s.value("profile", "Low-End Laptop Mode")
        i = self.cb_profile.findData(prof) if prof != "Low-End Laptop Mode" else 0
        self.cb_profile.setCurrentIndex(max(0, i))
        if prof in PROFILES:
            self._apply_profile(prof)
        for cb, key in ((self.cb_res, "res"), (self.cb_fps, "fps"), (self.cb_quality, "quality"), (self.cb_enc, "encoder")):
            v = s.value(key)
            if v is not None:
                idx = cb.findData(int(v) if key == "fps" else v)
                if idx >= 0:
                    cb.setCurrentIndex(idx)
        self.sp_bitrate.setValue(int(s.value("bitrate", self.sp_bitrate.value() or 2500)))
        self.sw_mic.setChecked(s.value("mic_on", "false") == "true")
        self.sw_sys.setChecked(s.value("sys_on", "false") == "true")
        self.sl_mic.setValue(int(s.value("mic_vol", 100)))
        self.sl_sys.setValue(int(s.value("sys_vol", 100)))
        for cb, key in ((self.cb_mic, "mic_name"), (self.cb_sys, "sys_name")):
            n = s.value(key)
            if n:
                for k in range(cb.count()):
                    if cb.itemText(k) == n:
                        cb.setCurrentIndex(k)
        default_dir = str(Path.home() / "Videos" / "NOTZYH Recordings")
        self.ed_dir.setText(s.value("outdir", default_dir))

    def _save_settings(self):
        s = self.settings
        s.setValue("profile", self.cb_profile.currentData())
        s.setValue("res", self.cb_res.currentData())
        s.setValue("fps", self.cb_fps.currentData())
        s.setValue("quality", self.cb_quality.currentData())
        s.setValue("encoder", self.cb_enc.currentData())
        s.setValue("bitrate", self.sp_bitrate.value())
        s.setValue("mic_on", "true" if self.sw_mic.isChecked() else "false")
        s.setValue("sys_on", "true" if self.sw_sys.isChecked() else "false")
        s.setValue("mic_vol", self.sl_mic.value())
        s.setValue("sys_vol", self.sl_sys.value())
        s.setValue("mic_name", self.cb_mic.currentText())
        s.setValue("sys_name", self.cb_sys.currentText())
        s.setValue("outdir", self.ed_dir.text())

    def _apply_profile(self, name):
        p = PROFILES.get(name)
        if not p:
            return
        for w in (self.cb_res, self.cb_fps, self.cb_quality, self.cb_enc, self.sp_bitrate):
            w.blockSignals(True)
        self.cb_res.setCurrentIndex(max(0, self.cb_res.findData(p["res"])))
        self.cb_fps.setCurrentIndex(max(0, self.cb_fps.findData(p["fps"])))
        self.cb_quality.setCurrentIndex(max(0, self.cb_quality.findData(p["quality"])))
        self.cb_enc.setCurrentIndex(max(0, self.cb_enc.findData(p["encoder"])))
        self.sp_bitrate.setValue(p["bitrate"])
        for w in (self.cb_res, self.cb_fps, self.cb_quality, self.cb_enc, self.sp_bitrate):
            w.blockSignals(False)

    # ---- wiring
    def _wire(self):
        BUS.line.connect(self._append_log)
        self.btn_rec.clicked.connect(self.start_recording)
        self.btn_stop.clicked.connect(self.stop_recording)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_open.clicked.connect(lambda: self._open(self.ed_dir.text()))
        self.btn_logfile.clicked.connect(lambda: self._open(str(LOG_FILE)))
        self.btn_refresh.clicked.connect(self._refresh_devices)
        self.cb_profile.currentIndexChanged.connect(self._profile_changed)
        for w in (self.cb_res, self.cb_fps, self.cb_quality, self.cb_enc):
            w.currentIndexChanged.connect(self._custom_changed)
        self.sp_bitrate.valueChanged.connect(self._custom_changed)
        self.sl_mic.valueChanged.connect(lambda v: self.lb_mic.setText(f"{v}%"))
        self.sl_sys.valueChanged.connect(lambda v: self.lb_sys.setText(f"{v}%"))
        self.ed_dir.textChanged.connect(self.refresh_summary)
        if self.rec:
            self.rec.sig_level.connect(self._on_level)
            self.rec.sig_stats.connect(self._on_stats)
            self.rec.sig_done.connect(self._on_done)
            self.rec.sig_failed.connect(self._on_failed)
            self.rec.sig_fallback.connect(self.rec.retry_gdigrab)

    def _profile_changed(self):
        d = self.cb_profile.currentData()
        if d in PROFILES:
            self._apply_profile(d)
        self.refresh_summary()

    def _custom_changed(self):
        if self.cb_profile.currentData() != "custom":
            self.cb_profile.blockSignals(True)
            self.cb_profile.setCurrentIndex(self.cb_profile.findData("custom"))
            self.cb_profile.blockSignals(False)
        self.refresh_summary()

    def _append_log(self, line):
        self.log.appendPlainText(line)

    def _open(self, path):
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True) if path.endswith(".log") else Path(path).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose recording folder", self.ed_dir.text())
        if d:
            self.ed_dir.setText(d)

    # ---- state helpers
    def resolved_encoder(self):
        sel = self.cb_enc.currentData()
        if sel == "auto":
            return "qsv" if (self.caps and self.caps["qsv"]) else "x264"
        return sel

    def refresh_summary(self):
        self.t_res.set(self.cb_res.currentData().replace("x", " x "))
        self.t_fps.set(f"{self.cb_fps.currentData()} FPS")
        enc = self.resolved_encoder()
        name = "Intel Quick Sync (h264_qsv)" if enc == "qsv" else "CPU x264 ultrafast"
        if self.caps is None:
            name = "Detecting..."
        elif self.cb_enc.currentData() == "qsv" and not self.caps["qsv"]:
            name = "Quick Sync not available - will use x264"
        self.t_enc.set(f"{name}  -  {self.sp_bitrate.value()} kbps")
        self.dest_lbl.setText(self.ed_dir.text())

    def _set_idle_ui(self, ready=True):
        self.btn_rec.setEnabled(ready)
        self.btn_stop.setEnabled(False)
        self.btn_rec.setText("\u25CF  RECORD")
        for w in (self.cb_profile, self.cb_res, self.cb_fps, self.cb_quality, self.cb_enc, self.sp_bitrate,
                  self.sw_mic, self.sw_sys, self.cb_mic, self.cb_sys, self.btn_browse, self.btn_refresh):
            w.setEnabled(True)
        self.chip.set_state("READY" if ready else "ERROR")

    def _fatal(self, text):
        self.msg.setText(text)
        self.btn_rec.setEnabled(False)
        self.chip.set_state("ERROR")
        logger.info(text)

    def _on_caps(self, caps):
        self.caps = caps
        logger.info("%s", caps["version"])
        logger.info("Quick Sync: %s | DXGI capture: %s", "available" if caps["qsv"] else "not available",
                    "available" if caps["ddagrab"] else "not available (GDI fallback)")
        self.refresh_summary()
        self._set_idle_ui(True)
        self.msg.setText("Ready. Quick Sync hardware encoding detected." if caps["qsv"]
                         else "Ready. Using CPU x264 ultrafast (Quick Sync not available).")

    # ---- recording flow
    def _device(self, cb, sw, vol):
        if not sw.isChecked():
            return None
        info = cb.currentData()
        if not info:
            raise RuntimeError("The selected audio device is not available. Pick another device or turn it off.")
        return {"info": info, "vol": vol.value() / 100.0}

    def start_recording(self):
        if not self.rec or self.rec.busy or self.caps is None:
            return
        try:
            outdir = Path(self.ed_dir.text())
            outdir.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(outdir).free < 1_000_000_000:
                QtWidgets.QMessageBox.warning(self, APP_NAME, "Less than 1 GB free in the output folder. Free some space first.")
                return
            w, h = (int(x) for x in self.cb_res.currentData().split("x"))
            cfg = dict(w=w, h=h, fps=int(self.cb_fps.currentData()), bitrate=self.sp_bitrate.value(),
                       quality=self.cb_quality.currentData(), encoder=self.resolved_encoder(), outdir=str(outdir),
                       caps=self.caps, mic=self._device(self.cb_mic, self.sw_mic, self.sl_mic),
                       sys=self._device(self.cb_sys, self.sw_sys, self.sl_sys))
            self._save_settings()
            self.rec.start(cfg, self.pa, self.pyaudio)
        except Exception as e:
            logger.info("Start failed: %s", e)
            QtWidgets.QMessageBox.critical(self, APP_NAME, f"Could not start recording:\n\n{e}")
            self._set_idle_ui(self.caps is not None)
            return
        self.t_start = time.monotonic()
        self.timer_lbl.setText("00:00:00")
        self.t_drop.set("0")
        self.btn_rec.setEnabled(False)
        self.btn_rec.setText("RECORDING")
        self.btn_stop.setEnabled(True)
        for w in (self.cb_profile, self.cb_res, self.cb_fps, self.cb_quality, self.cb_enc, self.sp_bitrate,
                  self.sw_mic, self.sw_sys, self.cb_mic, self.cb_sys, self.btn_browse, self.btn_refresh):
            w.setEnabled(False)
        self.chip.set_state("RECORDING")
        self.msg.setText("Recording in progress...")
        self.ui_timer.start()

    def stop_recording(self):
        if not self.rec or not self.rec.busy:
            return
        self.btn_stop.setEnabled(False)
        self.chip.set_state("FINALIZING")
        self.msg.setText("Finalizing MP4 - please wait, do not close the app...")
        self.ui_timer.stop()
        self._pulse = QtCore.QTimer(self)
        self._pulse.timeout.connect(self.chip.tick)
        self._pulse.start(100)
        self.rec.stop()

    def _tick(self):
        e = int(time.monotonic() - self.t_start)
        self.timer_lbl.setText(f"{e // 3600:02d}:{e % 3600 // 60:02d}:{e % 60:02d}")
        self.chip.tick()
        self.lv_mic.decay()
        self.lv_sys.decay()

    def _on_level(self, which, v):
        (self.lv_mic if which == "mic" else self.lv_sys).push(v)

    def _on_stats(self, frames, drops):
        self.t_drop.set(f"{drops}   ({frames} frames)")

    def _stop_pulse(self):
        self.ui_timer.stop()
        p = getattr(self, "_pulse", None)
        if p:
            p.stop()
        self.lv_mic.reset()
        self.lv_sys.reset()

    def _on_done(self, ok, text):
        self._stop_pulse()
        self._set_idle_ui(True)
        if ok:
            path = text.splitlines()[0]
            self.msg.setText("Saved: " + path)
            box = QtWidgets.QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setIcon(QtWidgets.QMessageBox.Information)
            box.setText("Recording saved.\n\n" + text)
            open_b = box.addButton("Show in folder", QtWidgets.QMessageBox.AcceptRole)
            box.addButton(QtWidgets.QMessageBox.Close)
            box.exec()
            if box.clickedButton() is open_b:
                if IS_WIN:
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    self._open(str(Path(path).parent))
        else:
            self.chip.set_state("ERROR")
            self.msg.setText("Recording problem - see log.")
            QtWidgets.QMessageBox.critical(self, APP_NAME, text)

    def _on_failed(self, text):
        self._stop_pulse()
        self._set_idle_ui(True)
        self.chip.set_state("ERROR")
        self.msg.setText("FFmpeg failed to start recording - see log.")
        QtWidgets.QMessageBox.critical(self, APP_NAME, text)

    # ---- window events
    def showEvent(self, e):
        super().showEvent(e)
        if IS_WIN:
            try:
                import ctypes
                hwnd = int(self.winId())
                v = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(v), 4)
            except Exception:
                pass

    def closeEvent(self, e):
        if self.rec and self.rec.busy:
            r = QtWidgets.QMessageBox.question(
                self, APP_NAME, "A recording is in progress.\n\nStop, save the recording and exit?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            if r != QtWidgets.QMessageBox.Yes:
                e.ignore()
                return
            self.rec.stop()
            t0 = time.monotonic()
            while self.rec.busy and time.monotonic() - t0 < 90:
                QtWidgets.QApplication.processEvents()
                time.sleep(0.05)
        self._save_settings()
        try:
            if self.pa:
                self.pa.terminate()
        except Exception:
            pass
        e.accept()


def apply_dark_palette(app):
    app.setStyle("Fusion")
    pal = QtGui.QPalette()
    pal.setColor(QtGui.QPalette.Window, QtGui.QColor("#14161a"))
    pal.setColor(QtGui.QPalette.WindowText, QtGui.QColor("#e8ecf3"))
    pal.setColor(QtGui.QPalette.Base, QtGui.QColor("#1b1e25"))
    pal.setColor(QtGui.QPalette.Text, QtGui.QColor("#e8ecf3"))
    pal.setColor(QtGui.QPalette.Button, QtGui.QColor("#242830"))
    pal.setColor(QtGui.QPalette.ButtonText, QtGui.QColor("#e8ecf3"))
    pal.setColor(QtGui.QPalette.Highlight, QtGui.QColor(ACCENT))
    pal.setColor(QtGui.QPalette.HighlightedText, QtGui.QColor("white"))
    app.setPalette(pal)


def main():
    ff = find_ffmpeg()
    if "--selftest" in sys.argv:
        ok = bool(ff) and _quiet([ff, "-version"])
        try:
            import pyaudiowpatch  # noqa: F401
        except Exception:
            ok = False
        sys.exit(0 if ok else 1)
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True) if hasattr(QtCore.Qt, "AA_EnableHighDpiScaling") else None
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setFont(QtGui.QFont("Segoe UI", 10))
    apply_dark_palette(app)
    app.setStyleSheet(QSS)
    win = MainWindow(ff)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
