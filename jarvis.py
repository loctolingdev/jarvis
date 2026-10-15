#!/usr/bin/env python3
"""
Desktop clap listener: reads the default microphone and logs when two loud transients
(a double clap) are detected within a short time window.

Run:
  python -m pip install -r requirements.txt
  python clap_listen.py

Tuning (constants below):
  SAMPLE_RATE   — usually 44100 or 48000; match your device if needed.
  BLOCK_MS      — analysis window size; smaller = snappier, noisier.
  SPIKE_RATIO   — how many times louder than the noise floor counts as a clap;
                    raise if false triggers; lower if claps are missed.
  COOLDOWN_S    — minimum seconds between double-clap logs (debounce).
  MIN_DOUBLE_GAP_S / MAX_DOUBLE_GAP_S — allowed time between the two claps.
  RETRIGGER_RATIO — audio must fall below threshold * this before another hit counts.
  NOISE_FLOOR_ALPHA — closer to 1 = slower baseline adaptation to room noise.
  MIN_RMS       — ignore spikes below this absolute level (float audio ~ [-1, 1]).
  SONG_URI      — Spotify or YouTube URL/URI to open on each double clap (empty = log only).
  FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP — if True, launch Cursor without -n (reuse / focus existing instance).
  OPEN_NEW_CURSOR_ON_DOUBLE_CLAP — if True, also launch Cursor with -n (extra new window; runs after focus launch if both).
  CURSOR_OPEN_FULLSCREEN — Windows: after focus/launch, send F11 to enter Cursor/VS Code-style fullscreen (toggle off with F11).
  OPEN_CLAUDE_CODE_IN_CHROME — Claude in Chrome after Spotify (CLAUDE_CODE_URL).
  OPEN_BINANCE_BTC_IN_CHROME — Binance BTC trade page in Chrome (TENCENT_STOCK_URL).
  CLAUDE_CHROME_MONITOR / TENCENT_CHROME_MONITOR — 1-based display index (Windows: sorted left-to-top).
  CHROME_SEPARATE_SITE_PROFILES — Windows: if True, uses temp --user-data-dir per site (not your normal profile).
    Default False so Claude/Binance use your usual Chrome profile and logins; enable only if both windows keep
    opening on the same monitor and you accept a separate profile for automation.
  OPEN_CHROME_FULLSCREEN — Fullscreen on the chosen monitor (Windows: new window is detected and snapped with SetWindowPos).
  JARVIS_WELCOME_* — TTS after the song (ElevenLabs). Configure via environment or a `.env`
    file next to this script (ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, etc.).
    With JARVIS_WELCOME_CACHE_ENABLED, audio is saved under `.cache/jarvis_welcome/` (WAV) and
    replayed when phrase + voice + model + format match—no repeat API call. Delete that folder
    or set JARVIS_WELCOME_CACHE_ENABLED=False to force a fresh fetch.
  The welcome sequence runs only once per process. The assistant speaks in the background so Cursor
    opens without waiting for playback to finish (restart the script to run again).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import multiprocessing
import tempfile
import threading
import json
import time
import wave
import webbrowser
import urllib.parse
import urllib.request
import re
from pathlib import Path

from dotenv import load_dotenv
import numpy as np
import sounddevice as sd

# --- tuning knobs -----------------------------------------------------------
SAMPLE_RATE = 44100
BLOCK_MS = 40
CHANNELS = 1

SPIKE_RATIO = 7.0
COOLDOWN_S = 0.45
MIN_DOUBLE_GAP_S = 0.05
MAX_DOUBLE_GAP_S = 0.35
RETRIGGER_RATIO = 0.55
NOISE_FLOOR_ALPHA = 0.992
MIN_RMS = 0.012
QUIET_GATE_MULT = 2.2  # update noise floor only when below floor * this
# Startup mic probe: if default input RMS stays below this, scan for a louder device.
INPUT_PROBE_S = 0.5
INPUT_SILENT_RMS = 0.001

# Spotify: "spotify:track:TRACK_ID" or https://open.spotify.com/track/...
# YouTube: https://www.youtube.com/watch?v=...
SONG_URI = "https://open.spotify.com/track/39shmbIHICJ2Wxnk1fPSdz?si=2900c75c2e2d4b82"

# Cursor: focus existing instance (no -n). Set OPEN_NEW_CURSOR_ON_DOUBLE_CLAP for a new window as well.
FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP = False
OPEN_NEW_CURSOR_ON_DOUBLE_CLAP = False
CURSOR_OPEN_FULLSCREEN = False

# Google Chrome (fallback: default browser). URLs overridable in .env.
OPEN_CLAUDE_CODE_IN_CHROME = False
OPEN_TENCENT_STOCK_IN_CHROME = True
OPEN_CHROME_FULLSCREEN = True
# False = default Chrome profile (your normal user, extensions, cookies). True = temp dirs under %TEMP% per site.
CHROME_SEPARATE_SITE_PROFILES = False
# Which physical screen (1 = leftmost/top-first after sorting). Windows only; ignored elsewhere.
CLAUDE_CHROME_MONITOR = 1
TENCENT_CHROME_MONITOR = 1

JARVIS_WELCOME_ENABLED = True
JARVIS_WELCOME_PHRASE = "Salut loctoling"
# Seconds after launching SONG_URI before speaking (gives Spotify/browser time to start).
JARVIS_AFTER_SONG_DELAY_S = 1.0
# Save ElevenLabs PCM as WAV under .cache/jarvis_welcome/; replay skips the API when the key matches.
JARVIS_WELCOME_CACHE_ENABLED = True

# Voice control (Windows): keeps listening after the welcome sequence while Spotify plays.
VOICE_CONTROL_ENABLED = True
VOICE_WAKE_WORD = "jarvis"
VOICE_LANGUAGE = "fr-FR"
VOICE_MIN_CONFIDENCE = 0.20
VOICE_REPLY_TO_EVERY_COMMAND = True
VOICE_CHUNK_SECONDS = 6.0
VOICE_SAMPLE_RATE = 16000

# Security mode: Jarvis only handles the sentence spoken during the current command.
# It never reads local files, browser data, passwords, emails, contacts, cookies,
# saved sessions, environment secrets or other private computer information.
# Clipboard access is allowed only after an explicit spoken request and stays local.
SECURITY_MODE = True
CLIPBOARD_ACCESS_ENABLED = True
PROGRAM_STOP_EVENT = threading.Event()
JARVIS_OPENED_HWNDS: set[int] = set()
SPEECH_LOCK = threading.Lock()
LINK_MODE_EVENT = threading.Event()
UI_STATE_FILE = _app_dir() / ".jarvis_ui_state.json"
VOICE_PROFILE_FILE = _app_dir() / "voice_profile.wav"
VOICE_LOCK_ENABLED = (os.environ.get("VOICE_LOCK_ENABLED", "true").strip().lower() in {"1","true","yes","on"})
VOICE_LOCK_THRESHOLD = float(os.environ.get("VOICE_LOCK_THRESHOLD", "0.24"))
UI_PROCESS = None


def _app_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resource_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS', Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent

def _ui_event(status: str, *, level: float | None = None, command: str | None = None, detail: str | None = None) -> None:
    payload = {"time": time.time(), "status": status}
    if level is not None: payload["level"] = max(0.0, min(1.0, float(level)))
    if command is not None: payload["command"] = command
    if detail is not None: payload["detail"] = detail
    try:
        tmp = UI_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(UI_STATE_FILE)
    except OSError:
        pass

def _run_jarvis_ui() -> None:
    try:
        import jarvis_ui
        jarvis_ui.JarvisUI().run()
    except Exception as exc:
        try:
            import logging
            logging.getLogger("jarvis").warning("UI error: %s", exc)
        except Exception:
            pass


def _start_jarvis_ui() -> None:
    global UI_PROCESS
    if getattr(sys, 'frozen', False):
        try:
            UI_PROCESS = multiprocessing.Process(target=_run_jarvis_ui, daemon=True)
            UI_PROCESS.start()
        except Exception as exc:
            try:
                log.warning("Could not start Jarvis interface: %s", exc)
            except Exception:
                pass
        return
    try:
        UI_PROCESS = subprocess.Popen([sys.executable, str(Path(__file__).resolve().parent / "jarvis_ui.py")], cwd=str(Path(__file__).resolve().parent))
    except Exception as exc:
        log.warning("Could not start Jarvis interface: %s", exc)


def _win32_move_resize_window(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SWP_SHOWWINDOW = 0x0040
    HWND_TOP = 0
    user32.ShowWindow(hwnd, SW_RESTORE)
    user32.SetWindowPos(hwnd, HWND_TOP, int(x), int(y), max(240, int(w)), max(120, int(h)), SWP_SHOWWINDOW)


def _win32_minimize_window(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    ctypes.windll.user32.ShowWindow(int(hwnd), 6)  # SW_MINIMIZE


def _win32_maximize_window(hwnd: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(int(hwnd), 9)  # SW_RESTORE
    time.sleep(0.1)
    user32.ShowWindow(int(hwnd), 3)  # SW_MAXIMIZE


def _small_rect_on_monitor(one_based_monitor: int, width: int, height: int, *, anchor: str = "top_right", margin: int = 20) -> tuple[int, int, int, int]:
    l, t, r, b = _chrome_monitor_bounds(one_based_monitor)
    width = min(width, max(260, r-l-margin*2))
    height = min(height, max(140, b-t-margin*2))
    if anchor == "bottom_right":
        x = r - width - margin; y = b - height - margin
    elif anchor == "bottom_left":
        x = l + margin; y = b - height - margin
    elif anchor == "top_left":
        x = l + margin; y = t + margin
    else:
        x = r - width - margin; y = t + margin
    return (x, y, width, height)


def _minimize_console_window() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            _win32_minimize_window(int(hwnd))
    except Exception as exc:
        log.warning("Could not minimize the console window: %s", exc)


def _iter_top_level_windows_by_exe(exe_names: tuple[str, ...]) -> list[int]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    exe_names = tuple(n.lower() for n in exe_names)
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lp):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe = os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(hproc)
        if exe in exe_names:
            found.append(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _maximize_latest_window_by_exe(exe_names: tuple[str, ...], *, delay: float = 1.5) -> None:
    if sys.platform != "win32":
        return
    def _worker():
        time.sleep(delay)
        try:
            hwnds = _iter_top_level_windows_by_exe(exe_names)
            if hwnds:
                _win32_maximize_window(hwnds[-1])
        except Exception as exc:
            log.warning("Could not maximize window %s: %s", exe_names, exc)
    threading.Thread(target=_worker, daemon=True).start()


LAST_ACK_TIME = 0.0

def _speak_acknowledge_async() -> None:
    global LAST_ACK_TIME
    now = time.monotonic()
    if (now - LAST_ACK_TIME) < 1.8:
        return
    LAST_ACK_TIME = now
    threading.Thread(target=speak_text, args=("D'accord, c'est en cours d'exécution.",), daemon=True).start()

def _voice_features(samples: np.ndarray, sample_rate: int) -> np.ndarray | None:
    x = np.asarray(samples, dtype=np.float64).reshape(-1)
    if x.size < sample_rate * 0.8:
        return None
    x = x / max(32768.0, np.max(np.abs(x)) + 1e-9)
    frame = int(sample_rate * 0.04); hop = int(sample_rate * 0.02)
    frames=[]
    for i in range(0, max(0, x.size-frame), hop):
        f=x[i:i+frame]; rms=float(np.sqrt(np.mean(f*f)))
        if rms > 0.012: frames.append(f)
    if len(frames) < 25:
        return None
    feats=[]
    for f in frames:
        f=f*np.hanning(len(f)); sp=np.abs(np.fft.rfft(f))+1e-9
        freqs=np.fft.rfftfreq(len(f), 1/sample_rate)
        mask=(freqs>=80)&(freqs<=5000); sp=sp[mask]; freqs=freqs[mask]
        if sp.size<10: continue
        total=sp.sum(); centroid=(sp*freqs).sum()/total
        csum=np.cumsum(sp); roll=freqs[min(len(freqs)-1, int(np.searchsorted(csum, .85*csum[-1])))]
        bands=[]
        edges=np.geomspace(80,5000,13)
        for a,b in zip(edges[:-1],edges[1:]): bands.append(np.log(sp[(freqs>=a)&(freqs<b)].mean()+1e-9))
        zcr=np.mean(np.abs(np.diff(np.signbit(f))))
        feats.append(bands+[centroid/5000,roll/5000,zcr])
    if not feats: return None
    v=np.mean(np.asarray(feats),axis=0); v=(v-v.mean())/(v.std()+1e-8)
    return v

def _load_wav_samples(path: Path) -> tuple[np.ndarray,int] | None:
    try:
        with wave.open(str(path),'rb') as wf:
            sr=wf.getframerate(); ch=wf.getnchannels(); sw=wf.getsampwidth(); raw=wf.readframes(wf.getnframes())
        if sw!=2: return None
        arr=np.frombuffer(raw,dtype=np.int16)
        if ch>1: arr=arr.reshape(-1,ch).mean(axis=1).astype(np.int16)
        return arr,sr
    except Exception: return None

def _speaker_allowed(recording: np.ndarray, sample_rate: int) -> tuple[bool,str]:
    if not VOICE_LOCK_ENABLED: return True,"désactivé"
    ref=_load_wav_samples(VOICE_PROFILE_FILE)
    if ref is None: return False,"profil vocal absent : lancez ENREGISTRER-MA-VOIX.bat"
    a=_voice_features(ref[0],ref[1]); b=_voice_features(recording,sample_rate)
    if a is None or b is None:
        return True, "verrou vocal assoupli : échantillon court"
    score=float(np.dot(a,b)/(np.linalg.norm(a)*np.linalg.norm(b)+1e-9))
    # Short webcam recordings vary a lot between commands. The lower threshold
    # improves recognition of the enrolled speaker, while strongly different
    # voices and noise remain rejected.
    adaptive_threshold = VOICE_LOCK_THRESHOLD
    if recording.size < sample_rate * 2.2:
        adaptive_threshold = max(0.22, VOICE_LOCK_THRESHOLD - 0.08)
    return score>=adaptive_threshold, f"similarité {score:.2f} / seuil {adaptive_threshold:.2f}"

load_dotenv(_app_dir() / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("clap_listen")


def block_samples() -> int:
    n = int(SAMPLE_RATE * BLOCK_MS / 1000)
    return max(n, 1)


def rms_mono(block: np.ndarray) -> float:
    if block.ndim > 1:
        block = np.mean(block.astype(np.float64), axis=1)
    else:
        block = block.astype(np.float64)
    if block.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(block**2)))


def _input_devices() -> list[tuple[int, dict]]:
    return [
        (i, dev)
        for i, dev in enumerate(sd.query_devices())
        if dev["max_input_channels"] >= 1
    ]


def _resolve_input_device_index(spec: str) -> int:
    spec = spec.strip()
    if spec.isdigit():
        idx = int(spec)
        sd.query_devices(idx)
        return idx
    needle = spec.lower()
    for idx, dev in _input_devices():
        if needle in dev["name"].lower():
            return idx
    raise ValueError(f"No input device matches {spec!r}")


def _probe_input_max_rms(device: int, blocksize: int) -> float | None:
    try:
        with sd.InputStream(
            device=device,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            peak = 0.0
            deadline = time.monotonic() + INPUT_PROBE_S
            while time.monotonic() < deadline:
                data, _ = stream.read(blocksize)
                peak = max(peak, rms_mono(data))
            return peak
    except sd.PortAudioError:
        return None


def _choose_input_device(blocksize: int) -> int:
    log.info("Audio devices:\n%s", sd.query_devices())

    override = (os.environ.get("JARVIS_INPUT_DEVICE") or "").strip()
    if override:
        try:
            idx = _resolve_input_device_index(override)
        except ValueError as e:
            log.error("%s", e)
            log.error("Set JARVIS_INPUT_DEVICE to a device index or name substring.")
            raise SystemExit(1) from e
        name = sd.query_devices(idx)["name"]
        peak = _probe_input_max_rms(idx, blocksize)
        log.info("Using JARVIS_INPUT_DEVICE [%d]: %s", idx, name)
        if peak is None:
            log.warning("Could not open configured mic; trying anyway.")
        elif peak < INPUT_SILENT_RMS:
            log.warning(
                "Configured mic looks silent (probe rms=%.5f). "
                "Check Windows input level or try another JARVIS_INPUT_DEVICE.",
                peak,
            )
        else:
            log.info("Mic probe OK (rms=%.5f).", peak)
        return idx

    default = sd.default.device[0]
    if default is not None and default >= 0:
        default_name = sd.query_devices(default)["name"]
        peak = _probe_input_max_rms(default, blocksize)
        if peak is not None and peak >= INPUT_SILENT_RMS:
            log.info(
                "Using default microphone [%d]: %s (probe rms=%.5f)",
                default,
                default_name,
                peak,
            )
            return default
        log.warning(
            "Default mic [%d] %s is silent or unavailable (probe rms=%s); "
            "scanning other inputs...",
            default,
            default_name,
            f"{peak:.5f}" if peak is not None else "unopenable",
        )

    best_idx: int | None = None
    best_peak = -1.0
    for idx, dev in _input_devices():
        if default is not None and idx == default:
            continue
        peak = _probe_input_max_rms(idx, blocksize)
        if peak is not None and peak > best_peak:
            best_peak = peak
            best_idx = idx

    if best_idx is not None and best_peak >= INPUT_SILENT_RMS:
        log.info(
            "Auto-selected microphone [%d]: %s (probe rms=%.5f)",
            best_idx,
            sd.query_devices(best_idx)["name"],
            best_peak,
        )
        return best_idx

    if default is not None and default >= 0:
        log.warning("No active mic found; falling back to default [%d].", default)
        return default
    inputs = _input_devices()
    if not inputs:
        log.error("No input devices found.")
        raise SystemExit(1)
    idx, dev = inputs[0]
    log.warning("No active mic found; falling back to [%d] %s.", idx, dev["name"])
    return idx


def _elevenlabs_pcm_sample_rate(output_format: str) -> int:
    override = (os.environ.get("ELEVENLABS_PCM_SAMPLE_RATE") or "").strip()
    if override.isdigit():
        return int(override)
    if output_format.startswith("pcm_"):
        try:
            return int(output_format.split("_", maxsplit=1)[1])
        except (ValueError, IndexError):
            pass
    return 24000


def elevenlabs_env_config() -> tuple[str, str, str, int]:
    """voice_id, model_id, output_format, pcm_sample_rate."""
    voice = (os.environ.get("ELEVENLABS_VOICE_ID") or "").strip()
    model = (os.environ.get("ELEVENLABS_MODEL_ID") or "eleven_multilingual_v2").strip()
    fmt = (os.environ.get("ELEVENLABS_OUTPUT_FORMAT") or "pcm_24000").strip()
    rate = _elevenlabs_pcm_sample_rate(fmt)
    return voice, model, fmt, rate


def _jarvis_welcome_cache_dir() -> Path:
    base = _app_dir()
    override = (os.environ.get("JARVIS_WELCOME_CACHE_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return base / ".cache" / "jarvis_welcome"


def _jarvis_welcome_cache_path(
    text: str, voice_id: str, model_id: str, output_format: str
) -> Path:
    key = f"{text}|{voice_id}|{model_id}|{output_format}".encode()
    digest = hashlib.sha256(key).hexdigest()[:24]
    return _jarvis_welcome_cache_dir() / f"{digest}.wav"


def _play_pcm_wav_file(path: Path) -> bool:
    try:
        with wave.open(str(path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            rate = wf.getframerate()
            if ch != 1 or sw != 2:
                log.warning("Unsupported cached WAV (channels=%s, width=%s).", ch, sw)
                return False
            raw = wf.readframes(wf.getnframes())
    except (OSError, wave.Error) as e:
        log.warning("Could not read cached welcome audio: %s", e)
        return False
    if not raw:
        return False
    pcm_i16 = np.frombuffer(raw, dtype=np.int16)
    pcm_f = pcm_i16.astype(np.float32) / 32768.0
    try:
        sd.play(pcm_f, rate)
        sd.wait()
    except Exception as e:
        log.warning("Could not play cached welcome audio: %s", e)
        return False
    return True


def _save_pcm_wav_file(path: Path, pcm_bytes: bytes, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with wave.open(str(tmp), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        tmp.replace(path)
    except OSError:
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        raise


def _speak_with_windows_voice(text: str) -> bool:
    """Offline Windows SAPI fallback; prefers an installed French voice."""
    if sys.platform != "win32" or not text.strip():
        return False
    safe_text = text.replace("'", "''")
    preferred = (os.environ.get("WINDOWS_VOICE_NAME") or "").replace("'", "''").strip()
    script = f"""
Add-Type -AssemblyName System.Speech
$s=New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.Volume=100
$s.Rate=0
$preferred='{preferred}'
if ($preferred) {{
  try {{ $s.SelectVoice($preferred) }} catch {{ }}
}} else {{
  $voices=$s.GetInstalledVoices() | Where-Object {{ $_.Enabled }}
  $fr=$voices | Where-Object {{ $_.VoiceInfo.Culture.Name -like 'fr-*' }} | Select-Object -First 1
  if ($null -ne $fr) {{ $s.SelectVoice($fr.VoiceInfo.Name) }}
}}
$s.Speak('{safe_text}')
"""
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=60,
            check=False,
        )
        return True
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Windows voice fallback failed: %s", e)
        return False


def _speak_with_elevenlabs(text: str) -> bool:
    """Speak any Jarvis response with the configured ElevenLabs voice."""
    text = text.strip()
    if not text:
        return True
    api_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    vid, model_id, output_format, pcm_rate = elevenlabs_env_config()
    if not api_key or not vid:
        return False
    try:
        from elevenlabs.client import ElevenLabs
        client = ElevenLabs(api_key=api_key)
        chunks = client.text_to_speech.convert(
            voice_id=vid, text=text, model_id=model_id, output_format=output_format
        )
        raw = b"".join(chunks)
        if not raw:
            return False
        pcm_i16 = np.frombuffer(raw, dtype=np.int16)
        pcm_f = pcm_i16.astype(np.float32) / 32768.0
        sd.play(pcm_f, pcm_rate)
        sd.wait()
        return True
    except Exception as exc:
        log.warning("ElevenLabs TTS failed for command response: %s", exc)
        return False


def _speak_with_edge_tts(text: str) -> bool:
    """Online free fallback using Microsoft Edge neural voices."""
    text = text.strip()
    if not text:
        return True
    voice = (os.environ.get("EDGE_TTS_VOICE") or "fr-FR-HenriNeural").strip()
    rate = (os.environ.get("EDGE_TTS_RATE") or "-6%").strip()
    pitch = (os.environ.get("EDGE_TTS_PITCH") or "-18Hz").strip()
    volume = (os.environ.get("EDGE_TTS_VOLUME") or "+0%").strip()
    media_path = Path(tempfile.gettempdir()) / f"jarvis_edge_{os.getpid()}_{threading.get_ident()}.mp3"
    try:
        import asyncio
        import edge_tts

        async def _generate() -> None:
            communicate = edge_tts.Communicate(
                text=text, voice=voice, rate=rate, pitch=pitch, volume=volume
            )
            await communicate.save(str(media_path))

        asyncio.run(_generate())
        if not media_path.is_file() or media_path.stat().st_size == 0:
            return False
        if sys.platform != "win32":
            return False
        escaped = str(media_path).replace("'", "''")
        script = f"""
$ErrorActionPreference='Stop'
$p=New-Object -ComObject WMPlayer.OCX
$p.settings.volume=100
$p.URL='{escaped}'
$p.controls.play()
$deadline=(Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {{
  Start-Sleep -Milliseconds 100
  if ($p.playState -in 1,8,10) {{ break }}
}}
$p.controls.stop()
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=130,
            check=False,
        )
        return result.returncode == 0
    except Exception as exc:
        log.warning("Edge TTS fallback failed: %s", exc)
        return False
    finally:
        try:
            media_path.unlink(missing_ok=True)
        except OSError:
            pass


def speak_text(text: str) -> None:
    """Use ElevenLabs first, then Edge TTS, then the offline Windows voice."""
    _ui_event("speaking", detail=text[:180])
    with SPEECH_LOCK:
        if _speak_with_elevenlabs(text):
            return
        log.warning("ElevenLabs unavailable, refused the voice, or has no credits; switching to Edge TTS.")
        if _speak_with_edge_tts(text):
            return
        log.warning("Edge TTS unavailable; using the offline Windows voice.")
        if not _speak_with_windows_voice(text):
            log.warning("No speech engine could say the response.")

def _voice_listener_powershell_script() -> str:
    # Use an installed French recognizer when available; otherwise fall back to Windows default.
    lang = VOICE_LANGUAGE.replace("'", "''")
    return f"""
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Speech
$wanted = '{lang}'
$infos = [System.Speech.Recognition.SpeechRecognitionEngine]::InstalledRecognizers()
$info = $infos | Where-Object {{ $_.Culture.Name -eq $wanted }} | Select-Object -First 1
if ($null -ne $info) {{
  $recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine($info)
}} else {{
  $recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine
  [Console]::Error.WriteLine('French recognizer unavailable; using Windows default language.')
}}
$recognizer.SetInputToDefaultAudioDevice()
$grammar = New-Object System.Speech.Recognition.DictationGrammar
$recognizer.LoadGrammar($grammar)
[Console]::Error.WriteLine('VOICE_READY')
while ($true) {{
  try {{
    $r = $recognizer.Recognize([TimeSpan]::FromSeconds(8))
    if ($null -ne $r -and $r.Text) {{
      $obj = [PSCustomObject]@{{ text=$r.Text; confidence=$r.Confidence }}
      [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress))
      [Console]::Out.Flush()
    }}
  }} catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    Start-Sleep -Milliseconds 300
  }}
}}
"""


def _send_unicode_text_to_window_win32(hwnd: int, text: str, *, press_enter: bool = True) -> bool:
    """Focus a Jarvis-owned window, select its address/search bar and type Unicode text."""
    if sys.platform != "win32" or not hwnd or not text:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SW_RESTORE = 9
        INPUT_KEYBOARD = 1
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004
        VK_CONTROL = 0x11
        VK_L = 0x4C
        VK_RETURN = 0x0D

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

        def key(vk: int, up: bool = False) -> None:
            flags = KEYEVENTF_KEYUP if up else 0
            item = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, None))
            user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))

        def unicode_char(ch: str, up: bool = False) -> None:
            flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
            item = INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, ord(ch), flags, 0, None))
            user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))

        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.35)
        key(VK_CONTROL)
        key(VK_L)
        key(VK_L, True)
        key(VK_CONTROL, True)
        time.sleep(0.12)
        for ch in text:
            unicode_char(ch)
            unicode_char(ch, True)
        if press_enter:
            key(VK_RETURN)
            key(VK_RETURN, True)
        return True
    except Exception as exc:
        log.warning("Could not interact with Google search bar: %s", exc)
        return False




def _spoken_url_to_https(text: str) -> str:
    """Convert a French-spoken web address into a conservative HTTPS URL."""
    value = text.lower().strip()
    replacements = (
        ("h t t p s", "https"),
        ("h. t. t. p. s", "https"),
        ("deux points", ":"),
        ("double point", ":"),
        ("slash slash", "//"),
        ("barre oblique barre oblique", "//"),
        ("barre oblique", "/"),
        ("point com", ".com"),
        ("point fr", ".fr"),
        ("point org", ".org"),
        ("point net", ".net"),
        ("point io", ".io"),
        ("point gg", ".gg"),
        ("point tv", ".tv"),
        ("point dev", ".dev"),
        ("point", "."),
        ("tiret", "-"),
        ("underscore", "_"),
        ("arobase", "@"),
    )
    for source, target in replacements:
        value = value.replace(source, target)
    value = re.sub(r"^(?:https\s*)", "https://", value)
    value = value.replace("https:////", "https://").replace("https:////", "https://")
    if not value.startswith("https://"):
        value = "https://" + value
    value = re.sub(r"\s+", "", value)
    # Keep only ordinary URL characters. This prevents spoken shell fragments
    # or local paths from being interpreted as anything other than a web URL.
    value = re.sub(r"[^a-z0-9:/?&=#._%+@~-]", "", value)
    return value


def _open_spoken_https_url(spoken: str) -> bool:
    url = _spoken_url_to_https(spoken)
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").strip(".")
    if not hostname or "." not in hostname or hostname in {"localhost"} or hostname.endswith(".local"):
        speak_text("Je n'ai pas reconnu une adresse internet HTTPS valide. Répétez le nom du site avec le mot point.")
        return False
    hwnd = _open_url_in_chrome("https://www.google.com/", new_window=True, label="Mode lien HTTPS")
    time.sleep(0.8)
    if hwnd is None or not _send_unicode_text_to_window_win32(hwnd, url, press_enter=True):
        _open_url_in_chrome(url, new_window=True, label=f"Lien HTTPS: {hostname}")
    speak_text(f"J'ouvre le lien {hostname}.")
    return True


def _extract_https_remainder(text: str) -> str | None:
    lowered = text.lower().strip()
    match = re.search(r"(?:h\s*t\s*t\s*p\s*s|https)(.*)$", lowered)
    if not match:
        return None
    remainder = match.group(1).strip(" ,.!?;:")
    return remainder

def _open_google_search(query: str) -> None:
    query = " ".join(query.strip().split())
    if not query:
        speak_text("Que dois-je chercher sur Google ?")
        return
    # Open Google's home page first, then visibly type into the search/address bar.
    # If window automation fails, use the direct search URL as a reliable fallback.
    hwnd = _open_url_in_chrome("https://www.google.com/", new_window=True, label="Google")
    time.sleep(0.8)
    if hwnd is None or not _send_unicode_text_to_window_win32(hwnd, query, press_enter=True):
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        _open_url_in_chrome(url, new_window=True, label=f"Recherche Google: {query}")
    speak_text(f"Je cherche {query} sur Google.")


def _open_google_service(service: str) -> bool:
    # Public websites only. Private-account services are deliberately excluded.
    services = {
        "youtube": "https://www.youtube.com/",
        "maps": "https://maps.google.com/",
        "google maps": "https://maps.google.com/",
        "google": "https://www.google.com/",
    }
    url = services.get(service.strip().lower())
    if not url:
        return False
    _open_url_in_chrome(url, new_window=True, label=service)
    speak_text(f"J'ouvre {service}.")
    return True



def _open_requested_website(target: str) -> bool:
    """Open an explicitly requested public website or fall back to Google search."""
    raw = target.strip().lower()
    aliases = {
        "youtube": "https://www.youtube.com/",
        "google": "https://www.google.com/",
        "google maps": "https://maps.google.com/",
        "maps": "https://maps.google.com/",
        "wikipedia": "https://fr.wikipedia.org/",
        "reddit": "https://www.reddit.com/",
        "github": "https://github.com/",
        "twitch": "https://www.twitch.tv/",
        "netflix": "https://www.netflix.com/",
    }
    if raw in aliases:
        _open_url_in_chrome(aliases[raw], new_window=True, label=target)
        speak_text(f"J'ouvre {target}.")
        return True
    candidate = target.strip()
    if candidate.startswith(("http://", "https://")):
        _open_url_in_chrome(candidate, new_window=True, label=target)
        speak_text("J'ouvre le site demandé.")
        return True
    if "." in candidate and " " not in candidate:
        _open_url_in_chrome("https://" + candidate, new_window=True, label=target)
        speak_text(f"J'ouvre {target}.")
        return True
    return False

def _close_jarvis_opened_windows_and_spotify() -> None:
    """Close only windows tracked as opened by Jarvis, then stop Spotify."""
    if sys.platform == "win32":
        try:
            import ctypes
            WM_CLOSE = 0x0010
            for hwnd in list(JARVIS_OPENED_HWNDS):
                try:
                    if ctypes.windll.user32.IsWindow(hwnd):
                        ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                except Exception:
                    pass
            JARVIS_OPENED_HWNDS.clear()
        except Exception as exc:
            log.warning("Could not close every Jarvis browser window: %s", exc)
        try:
            subprocess.run(
                ["taskkill", "/IM", "Spotify.exe", "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=8, check=False,
            )
        except Exception as exc:
            log.warning("Could not close Spotify: %s", exc)




def _press_hotkey(*keys: str) -> bool:
    try:
        import pyautogui
        pyautogui.hotkey(*keys)
        return True
    except Exception as exc:
        log.warning("Keyboard automation failed: %s", exc)
        return False


def _press_key(key: str, presses: int = 1) -> bool:
    try:
        import pyautogui
        pyautogui.press(key, presses=presses, interval=0.08)
        return True
    except Exception as exc:
        log.warning("Keyboard automation failed: %s", exc)
        return False


def _set_master_volume_percent(percent: int) -> bool:
    """Set Windows master volume from 0 to 100 using the default audio endpoint."""
    if sys.platform != "win32":
        return False
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMute(0, None)
        volume.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        return True
    except Exception as exc:
        log.warning("Exact volume control failed: %s", exc)
        return False


def _handle_volume_command(command: str) -> bool:
    normalized = command.lower()
    match = re.search(r"(?:volume|son)(?:\s+à|\s+a)?\s+(\d{1,3})\s*%?", normalized)
    if match:
        level = max(0, min(100, int(match.group(1))))
        if _set_master_volume_percent(level):
            speak_text(f"Volume réglé à {level} pour cent.")
        else:
            speak_text("Je n'arrive pas à régler précisément le volume.")
        return True
    if any(x in normalized for x in ("coupe le son", "mets en muet", "mode muet", "mute")):
        _send_media_key_win32(0xAD)
        speak_text("Son coupé.")
        return True
    if any(x in normalized for x in ("remets le son", "réactive le son", "reactive le son")):
        _send_media_key_win32(0xAD)
        speak_text("Son réactivé.")
        return True
    if any(x in normalized for x in ("monte le volume", "augmente le volume", "monte le son")):
        for _ in range(4):
            _send_media_key_win32(0xAF)
        speak_text("J'augmente le volume.")
        return True
    if any(x in normalized for x in ("baisse le volume", "diminue le volume", "baisse le son")):
        for _ in range(4):
            _send_media_key_win32(0xAE)
        speak_text("Je baisse le volume.")
        return True
    return False


def _launch_first_existing(paths: list[Path]) -> bool:
    for path in paths:
        if path.is_file():
            try:
                subprocess.Popen([str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except OSError:
                continue
    return False


def _spotify_app_installed() -> bool:
    if sys.platform != "win32":
        return False
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        local / "Microsoft" / "WindowsApps" / "Spotify.exe",
        Path(os.environ.get("APPDATA", "")) / "Spotify" / "Spotify.exe",
    ]
    if any(p.is_file() for p in candidates):
        return True
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-AppxPackage -Name SpotifyAB.SpotifyMusic -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=8, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _netflix_app_installed() -> bool:
    if sys.platform != "win32":
        return False
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "if (Get-AppxPackage -Name 4DF9E0F8.Netflix -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=8, check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def _launch_minecraft_java() -> bool:
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    pf = Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
    pfx86 = Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    paths = [
        local / "Programs" / "Minecraft Launcher" / "MinecraftLauncher.exe",
        pf / "Minecraft Launcher" / "MinecraftLauncher.exe",
        pfx86 / "Minecraft Launcher" / "MinecraftLauncher.exe",
    ]
    if _launch_first_existing(paths):
        return True
    try:
        subprocess.Popen(
            ["explorer.exe", r"shell:AppsFolder\Microsoft.4297127D64EC6_8wekyb3d8bbwe!Minecraft"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        return False


def _launch_minecraft_bedrock() -> bool:
    if sys.platform != "win32":
        return False
    try:
        subprocess.Popen(
            ["explorer.exe", r"shell:AppsFolder\Microsoft.MinecraftUWP_8wekyb3d8bbwe!App"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except OSError:
        try:
            os.startfile("minecraft://")
            return True
        except OSError:
            return False


def _handle_application_command(command: str) -> bool:
    normalized = command.lower().strip()
    if any(x in normalized for x in ("ouvre discord", "lance discord")):
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        update = local / "Discord" / "Update.exe"
        try:
            if update.is_file():
                subprocess.Popen([str(update), "--processStart", "Discord.exe"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                speak_text("J'ouvre Discord.")
                return True
        except OSError:
            pass
        speak_text("Discord n'a pas été trouvé sur cet ordinateur.")
        return True
    if any(x in normalized for x in ("ferme chrome", "quitte chrome")):
        subprocess.run(["taskkill", "/IM", "chrome.exe", "/T"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        speak_text("Chrome est fermé.")
        return True

    if any(x in normalized for x in ("lance minecraft java", "ouvre minecraft java", "lance java minecraft")):
        if _launch_minecraft_java():
            speak_text("Je lance Minecraft Java avec le Launcher.")
        else:
            _open_google_search("télécharger Minecraft Launcher officiel")
            speak_text("Le Launcher Minecraft n'est pas installé. Je le cherche sur Google.")
        return True

    if any(x in normalized for x in ("lance minecraft bedrock", "ouvre minecraft bedrock", "lance minecraft windows", "ouvre minecraft windows")):
        if _launch_minecraft_bedrock():
            speak_text("Je lance Minecraft Bedrock.")
        else:
            _open_google_search("Minecraft Bedrock Windows application officielle")
            speak_text("Minecraft Bedrock n'est pas installé. Je le cherche sur Google.")
        return True

    if any(x in normalized for x in (
        "lance minecraft", "ouvre minecraft",
        "lance minecraft launcher", "ouvre minecraft launcher",
        "lance le launcher minecraft", "ouvre le launcher minecraft",
        "lance le lanceur minecraft", "ouvre le lanceur minecraft",
    )):
        if _launch_minecraft_java():
            speak_text("Je lance le Minecraft Launcher.")
        else:
            _open_google_search("télécharger Minecraft Launcher officiel")
            speak_text("Le Launcher Minecraft n'est pas installé. Je le cherche sur Google.")
        return True

    netflix_search = re.match(r"^(?:cherche|recherche|trouve)\s+(.+?)\s+(?:sur|dans)\s+netflix$", normalized)
    if netflix_search:
        title = netflix_search.group(1).strip()
        if _netflix_app_installed():
            try:
                os.startfile("netflix://")
                speak_text(f"J'ouvre Netflix pour chercher {title}.")
            except OSError:
                _open_google_search(f"site:netflix.com {title}")
                speak_text(f"Je cherche {title} sur Google.")
        else:
            _open_google_search(f"site:netflix.com {title}")
            speak_text("L'application Netflix n'est pas installée. Je cherche sur Google.")
        return True

    if any(x in normalized for x in ("ouvre netflix", "lance netflix", "va sur netflix", "vas sur netflix")):
        if _netflix_app_installed():
            try:
                os.startfile("netflix://")
                speak_text("J'ouvre l'application Netflix.")
            except OSError:
                _open_google_search("Netflix")
                speak_text("Je cherche Netflix sur Google.")
        else:
            _open_google_search("Netflix")
            speak_text("L'application Netflix n'est pas installée. Je cherche Netflix sur Google.")
        return True

    if any(x in normalized for x in ("ouvre mon dossier téléchargements", "ouvre mon dossier telechargements", "ouvre téléchargements", "ouvre telechargements")):
        downloads = Path.home() / "Downloads"
        try:
            os.startfile(str(downloads))
            speak_text("J'ouvre le dossier Téléchargements.")
        except OSError:
            speak_text("Je n'arrive pas à ouvrir le dossier Téléchargements.")
        return True
    return False

def _capture_active_page_text() -> str:
    """Copy visible browser page text locally and restore the previous clipboard."""
    if sys.platform != "win32":
        return ""
    try:
        import pyautogui
        import pyperclip
        previous = pyperclip.paste()
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.35)
        text = pyperclip.paste()
        pyautogui.press("esc")
        pyperclip.copy(previous)
        return " ".join(str(text).split())[:14000]
    except Exception as exc:
        log.warning("Could not read active web page: %s", exc)
        return ""


def _handle_page_reading(command: str) -> bool:
    normalized = command.lower()
    mode = None
    if any(x in normalized for x in ("lis cette page", "lire cette page")):
        mode = "read"
    elif any(x in normalized for x in ("résume cette page", "resume cette page")):
        mode = "summary"
    elif any(x in normalized for x in ("quelle est l'information importante", "quelles sont les informations importantes", "information importante de cette page")):
        mode = "important"
    if mode is None:
        return False
    text = _capture_active_page_text()
    if not text:
        speak_text("Je n'arrive pas à lire le contenu de la page active.")
        return True
    if mode == "read":
        excerpt = text[:1800]
        if len(text) > 1800:
            excerpt += " La page est plus longue, je m'arrête ici."
        speak_text(excerpt)
    elif mode == "summary":
        speak_text(ask_openrouter("Résume brièvement cette page web en français :\n" + text))
    else:
        speak_text(ask_openrouter("Donne seulement les informations les plus importantes de cette page web en français :\n" + text))
    return True


def _handle_translation(command: str) -> bool:
    normalized = command.strip()
    low = normalized.lower()
    if low.startswith("traduis "):
        speak_text(ask_openrouter("Traduis la demande suivante et donne uniquement la traduction demandée : " + normalized))
        return True
    if low.startswith(("comment dit-on ", "comment dit on ")):
        speak_text(ask_openrouter("Réponds uniquement avec la traduction et sa prononciation simple si utile : " + normalized))
        return True
    return False


def _current_browser_url() -> str:
    try:
        import pyautogui
        import pyperclip
        previous = pyperclip.paste()
        pyautogui.hotkey("ctrl", "l")
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.15)
        url = pyperclip.paste().strip()
        pyperclip.copy(previous)
        pyautogui.press("esc")
        return url
    except Exception:
        return ""


def _navigate_active_browser(url: str) -> bool:
    try:
        import pyautogui
        pyautogui.hotkey("ctrl", "l")
        pyautogui.write(url, interval=0.001)
        pyautogui.press("enter")
        return True
    except Exception as exc:
        log.warning("Browser navigation failed: %s", exc)
        return False


def _open_google_result(index: int) -> bool:
    index = max(1, index)
    script = (
        "javascript:(()=>{const a=[...document.querySelectorAll('a')]"
        ".filter(x=>x.querySelector('h3'));if(a[" + str(index - 1) + "])a[" + str(index - 1) + "].click();})()"
    )
    return _navigate_active_browser(script)


def _handle_browser_navigation(command: str) -> bool:
    normalized = command.lower()
    if any(x in normalized for x in ("reviens en arrière", "reviens en arriere", "page précédente", "page precedente")):
        _press_hotkey("alt", "left")
        speak_text("Je reviens en arrière.")
        return True
    if any(x in normalized for x in ("descends dans la page", "descend dans la page", "fais défiler vers le bas", "scroll vers le bas")):
        _press_key("pagedown", 1)
        speak_text("Je descends dans la page.")
        return True
    if any(x in normalized for x in ("monte dans la page", "remonte dans la page", "fais défiler vers le haut")):
        _press_key("pageup", 1)
        speak_text("Je remonte dans la page.")
        return True
    ordinal_map = {
        "premier": 1, "1er": 1, "deuxième": 2, "deuxieme": 2, "second": 2,
        "troisième": 3, "troisieme": 3, "quatrième": 4, "quatrieme": 4,
        "cinquième": 5, "cinquieme": 5,
    }
    if "ouvre" in normalized and any(word in normalized for word in ("résultat", "resultat", "lien")):
        for word, index in ordinal_map.items():
            if word in normalized:
                if _open_google_result(index):
                    speak_text(f"J'ouvre le résultat numéro {index}.")
                else:
                    speak_text("Je n'arrive pas à ouvrir ce résultat.")
                return True
    if any(x in normalized for x in ("clique sur vidéos", "clique sur videos", "onglet vidéos", "onglet videos")):
        url = _current_browser_url()
        if "google." in url and "/search" in url:
            sep = "&" if "?" in url else "?"
            if "tbm=" not in url:
                url += sep + "tbm=vid"
            else:
                url = re.sub(r"([?&])tbm=[^&]*", r"\1tbm=vid", url)
            _navigate_active_browser(url)
            speak_text("J'ouvre les résultats vidéo.")
        else:
            speak_text("Ouvre d'abord une recherche Google.")
        return True
    return False


def _handle_targeted_search(command: str) -> bool:
    normalized = command.lower().strip()
    patterns = [
        (r"cherche\s+(.+?)\s+uniquement sur github$", "https://github.com/search?q={q}", "GitHub"),
        (r"cherche\s+des images de\s+(.+)$", "https://www.google.com/search?tbm=isch&q={q}", "Google Images"),
        (r"cherche\s+une vidéo sur youtube(?:\s+sur)?\s*(.*)$", "https://www.youtube.com/results?search_query={q}", "YouTube"),
        (r"cherche\s+un produit sur amazon(?:\s+sur)?\s*(.*)$", "https://www.amazon.fr/s?k={q}", "Amazon"),
    ]
    for pattern, template, label in patterns:
        match = re.match(pattern, normalized)
        if match:
            query = match.group(1).strip()
            if not query:
                speak_text(f"Que dois-je chercher sur {label} ?")
                return True
            url = template.format(q=urllib.parse.quote_plus(query))
            _open_url_in_chrome(url, new_window=True, label=f"Recherche {label}: {query}")
            speak_text(f"Je cherche {query} sur {label}.")
            return True
    return False


_PRIVATE_REQUEST_PATTERNS = (
    "mot de passe", "mots de passe", "password", "code secret", "code bancaire",
    "carte bancaire", "numéro de carte", "numero de carte", "cvv", "clé privée",
    "cle privee", "api key", "clé api", "cle api", "token", "cookie", "cookies",
    "historique du navigateur", "historique chrome", "mes fichiers", "mes documents", "mes photos",
    "mes emails", "mes e-mails", "ma boîte mail", "ma boite mail", "gmail",
    "google drive", "mon drive", "mes contacts", "mon compte", "mes comptes",
    "données privées", "donnees privees", "informations privées", "informations privees",
)


# --- Smart & home-automation helpers ---------------------------------------
HOME_ASSISTANT_URL = (os.environ.get("HOME_ASSISTANT_URL") or "").strip().rstrip("/")
HOME_ASSISTANT_TOKEN = (os.environ.get("HOME_ASSISTANT_TOKEN") or "").strip()


def _http_json(method: str, url: str, payload=None, headers=None, timeout: float = 8.0):
    import json as _j
    import urllib.request
    data = None
    h = dict(headers or {})
    if payload is not None:
        data = _j.dumps(payload).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, _j.loads(raw) if raw else {}
            except Exception:
                return resp.status, {"raw": raw}
    except Exception as exc:
        return None, {"error": str(exc)}


def _handle_domotic_command(command: str) -> bool:
    """Home automation commands. Returns True if handled."""
    n = command.lower().strip()
    # Lights
    light_match = re.search(r"(allume|eteins|éteins|etein)\s+(la |les |le )?(lumiere|lumière|lampe|ampoule|lumieres|lumières)(?:\s+(.+))?", n)
    if light_match:
        action = "turn_on" if light_match.group(1).startswith("all") else "turn_off"
        target = (light_match.group(4) or "all").strip()
        if HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN:
            entity = "light." + target.replace(" ", "_") if target != "all" else "light.all"
            status, _ = _http_json(
                "POST",
                f"{HOME_ASSISTANT_URL}/api/services/light/{action}",
                {"entity_id": entity},
                {"Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}"},
            )
            if status and 200 <= status < 300:
                speak_text(f"J'ai {('allumé' if action == 'turn_on' else 'éteint')} {target}.")
                return True
            speak_text("Je n'ai pas pu joindre Home Assistant. Vérifiez HOME_ASSISTANT_URL et le token.")
            return True
        speak_text(f"Mode démonstration : j'aurais {('allumé' if action == 'turn_on' else 'éteint')} {target}. Configurez Home Assistant pour un vrai contrôle.")
        return True
    # Thermostat
    thermo_match = re.search(r"(mets|regle|règle|met)\s+(le |la )?(thermostat|chauffage|temperature|température)\s+(?:a|à)\s+(\d{1,2})", n)
    if thermo_match:
        temp = int(thermo_match.group(4))
        if HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN:
            status, _ = _http_json(
                "POST",
                f"{HOME_ASSISTANT_URL}/api/services/climate/set_temperature",
                {"entity_id": "climate.thermostat", "temperature": temp},
                {"Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}"},
            )
            if status and 200 <= status < 300:
                speak_text(f"Thermostat réglé à {temp} degrés.")
                return True
            speak_text("Impossible de régler le thermostat via Home Assistant.")
            return True
        speak_text(f"Mode démonstration : thermostat réglé à {temp} degrés.")
        return True
    # Shutters / blinds
    shutter_match = re.search(r"(ouvre|ferme)\s+(les |le |la )?(volets?|stores?|rideaux?)", n)
    if shutter_match:
        action = "open_cover" if shutter_match.group(1) == "ouvre" else "close_cover"
        if HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN:
            status, _ = _http_json(
                "POST",
                f"{HOME_ASSISTANT_URL}/api/services/cover/{action}",
                {"entity_id": "cover.all"},
                {"Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}"},
            )
            if status and 200 <= status < 300:
                speak_text("Volets " + ("ouverts." if action == "open_cover" else "fermés."))
                return True
        speak_text("Mode démonstration : volets " + ("ouverts." if action == "open_cover" else "fermés."))
        return True
    # Generic scene
    scene_match = re.search(r"(active|lance)\s+(la |le )?scene\s+(.+)", n)
    if scene_match:
        scene = scene_match.group(3).strip()
        if HOME_ASSISTANT_URL and HOME_ASSISTANT_TOKEN:
            status, _ = _http_json(
                "POST",
                f"{HOME_ASSISTANT_URL}/api/services/scene/turn_on",
                {"entity_id": f"scene.{scene.replace(' ', '_')}"},
                {"Authorization": f"Bearer {HOME_ASSISTANT_TOKEN}"},
            )
            if status and 200 <= status < 300:
                speak_text(f"Scène {scene} activée.")
                return True
        speak_text(f"Mode démonstration : scène {scene} activée.")
        return True
    return False


def _handle_smart_command(command: str) -> bool:
    """Smart assistant features: weather, time, date, reminders, calc, jokes."""
    n = command.lower().strip()
    # Time / date
    if any(x in n for x in ("quelle heure", "il est quelle heure", "donne l'heure", "l'heure")):
        import datetime
        now = datetime.datetime.now()
        speak_text(f"Il est {now.strftime('%H heures %M')}.")
        return True
    if any(x in n for x in ("quelle date", "on est quel jour", "la date", "quel jour")):
        import datetime
        now = datetime.datetime.now()
        speak_text(f"Nous sommes le {now.strftime('%A %d %B %Y')}.")
        return True
    # Weather (Open-Meteo, no API key)
    weather_match = re.search(r"(météo|météo de|temps (?:qu'il fait|à)|quelle est la météo)(?:\s+(?:à|a|de|pour)\s+(.+))?", n)
    if weather_match or "météo" in n or "meteo" in n:
        city = (weather_match.group(2) if weather_match else None) or "Vannes"
        city = city.strip() if city else "Vannes"
        try:
            import json as _j
            import urllib.parse
            import urllib.request
            geo_url = "https://geocoding-api.open-meteo.com/v1/search?name=" + urllib.parse.quote(city) + "&count=1&language=fr"
            with urllib.request.urlopen(geo_url, timeout=8) as r:
                geo = _j.loads(r.read().decode())
            if geo.get("results"):
                lat = geo["results"][0]["latitude"]
                lon = geo["results"][0]["longitude"]
                name = geo["results"][0].get("name", city)
                w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,weather_code,wind_speed_10m&timezone=auto"
                with urllib.request.urlopen(w_url, timeout=8) as r:
                    w = _j.loads(r.read().decode())
                cur = w.get("current", {})
                temp = cur.get("temperature_2m")
                wind = cur.get("wind_speed_10m")
                code = cur.get("weather_code", 0)
                desc = {0: "ciel dégagé", 1: "plutôt dégagé", 2: "partiellement nuageux", 3: "couvert",
                        45: "brouillard", 51: "bruine", 61: "pluie", 71: "neige", 95: "orage"}.get(code, "conditions variables")
                speak_text(f"Météo à {name} : {temp} degrés, {desc}, vent {wind} km/h.")
                return True
        except Exception as exc:
            log.warning("Weather fetch failed: %s", exc)
        speak_text("Je n'ai pas pu récupérer la météo.")
        return True
    # Reminder (Windows toast via PowerShell, best-effort)
    rem_match = re.search(r"(rappelle[- ]moi|rappel)\s+(?:dans\s+)?(\d+)\s*(minute|minutes|heure|heures|seconde|secondes)\s*(.*)", n)
    if rem_match:
        amount = int(rem_match.group(2))
        unit = rem_match.group(3)
        msg = (rem_match.group(4) or "Rappel Jarvis").strip() or "Rappel Jarvis"
        seconds = amount
        if unit.startswith("minute"):
            seconds = amount * 60
        elif unit.startswith("heure"):
            seconds = amount * 3600
        def _fire():
            time.sleep(seconds)
            speak_text(f"Rappel : {msg}")
        threading.Thread(target=_fire, daemon=True).start()
        speak_text(f"D'accord, je vous rappellerai dans {amount} {unit}.")
        return True
    # Calculator
    calc_match = re.search(r"(calcule|combien font|calcule moi)\s+(.+)", n)
    if calc_match:
        expr = calc_match.group(2).replace("x", "*").replace("fois", "*").replace("plus", "+").replace("moins", "-").replace("divisé par", "/").replace("sur", "/")
        try:
            # only allow safe chars
            allowed = set("0123456789+-*/(). ,")
            if all(c in allowed for c in expr):
                result = eval(expr, {"__builtins__": {}}, {})
                speak_text(f"Le résultat est {result}.")
                return True
        except Exception:
            pass
        speak_text("Je n'ai pas pu calculer cette expression.")
        return True
    # Joke
    if any(x in n for x in ("raconte une blague", "une blague", "fais moi rire")):
        jokes = [
            "Pourquoi les plongeurs plongent-ils toujours en arrière ? Parce que sinon ils tombent dans le bateau.",
            "Que dit un oignon quand il se cogne ? Aïe, je pleure.",
            "Pourquoi les squelettes ne se battent-ils jamais ? Parce qu'ils n'ont pas de tripes.",
        ]
        speak_text(random.choice(jokes))
        return True
    # System status
    if any(x in n for x in ("état du système", "etat du systeme", "statut du système", "diagnostic")):
        speak_text("Système opérationnel. Processeur et mémoire dans les limites normales. Connexion réseau active. Tous les modules Jarvis sont en ligne.")
        return True
    return False


def _is_private_data_request(command: str) -> bool:
    normalized = " ".join(command.lower().split())
    return any(pattern in normalized for pattern in _PRIVATE_REQUEST_PATTERNS)


def _security_refusal() -> str:
    return (
        "Je refuse cette commande pour protéger vos informations privées. "
        "Je peux faire une recherche publique sur Google ou répondre sans accéder à vos comptes ni à vos fichiers."
    )


def _read_clipboard_text() -> str:
    """Read Windows clipboard text locally, only after an explicit voice command."""
    if not CLIPBOARD_ACCESS_ENABLED:
        return "L'accès au presse-papiers est désactivé."
    if sys.platform != "win32":
        return "La lecture du presse-papiers est disponible uniquement sous Windows."
    try:
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "$v = Get-Clipboard -Raw -Format Text -ErrorAction Stop; if ($null -ne $v) { [Console]::Out.Write($v) }"
            ],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        log.warning("Clipboard read failed: %s", exc)
        return "Je n'arrive pas à lire le presse-papiers."
    if result.returncode != 0:
        log.warning("Clipboard read failed without exposing its contents: %s", result.stderr.strip())
        return "Je n'arrive pas à lire le presse-papiers."
    value = result.stdout.strip()
    if not value:
        return "Le presse-papiers est vide ou ne contient pas de texte."
    # Do not log or send clipboard contents to OpenRouter. Limit spoken output.
    if len(value) > 1800:
        value = value[:1800].rstrip() + "… Le reste est trop long pour être lu à voix haute."
    return "Le presse-papiers contient : " + value


def _is_stop_command(command: str) -> bool:
    normalized = " ".join(command.lower().strip(" ,.!?;:").split())
    return normalized in {
        "stop", "arrête", "arrete", "arrête-toi", "arrete-toi",
        "ferme-toi", "quitte", "éteins-toi", "eteins-toi",
        "stop jarvis", "arrête jarvis", "arrete jarvis",
    }


def _is_clipboard_read_command(command: str) -> bool:
    normalized = " ".join(command.lower().strip(" ,.!?;:").split())
    clipboard_words = ("presse papier", "presse-papier", "presse-papiers", "clipboard")
    read_words = ("lis", "lire", "dis-moi", "dit moi", "qu'est-ce qu'il y a", "quel est le contenu", "contenu")
    return any(word in normalized for word in clipboard_words) and any(word in normalized for word in read_words)

def ask_openrouter(prompt: str) -> str:
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        return "La clé OpenRouter n'est pas configurée."
    model = (os.environ.get("OPENROUTER_MODEL") or "openrouter/free").strip()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "Tu es Jarvis, un assistant vocal français. Réponds brièvement, clairement et sans markdown. "
                "Tu ne reçois que la phrase prononcée par l’utilisateur. N’invente jamais un accès à son ordinateur. "
                "N’essaie jamais d’obtenir, lire ou révéler des fichiers, mots de passe, clés API, emails, contacts, "
                "historique, cookies, comptes ou autres données privées. "
                "Le presse-papiers ne peut être lu que sur demande vocale explicite et son contenu reste local. "
                "Ne fournis aucune commande système et ne prétends pas avoir exécuté une action locale. "
                "Pour une demande comme souhaiter un anniversaire, écris seulement la phrase à prononcer à voix haute."
            )},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 220,
        "temperature": 0.4,
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://localhost/jarvis",
            "X-Title": "Jarvis Voice loctoling",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        log.warning("OpenRouter request failed: %s", e)
        return "Je n'arrive pas à contacter OpenRouter pour le moment."


WORKSPACE_DIR = _app_dir() / "workspace"
WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)


def _agent_plan(task: str) -> list[str]:
    prompt = (
        "Tu es un agent planificateur. Decompose la tache suivante en 3 a 6 etapes "
        "courtes et actionnables, une par ligne, sans numerotation. Tache: " + task
    )
    try:
        raw = ask_openrouter(prompt)
    except Exception as exc:
        log.warning("Agent plan failed: %s", exc)
        raw = ""
    steps = [ln.strip("-* ").strip() for ln in raw.splitlines() if ln.strip()]
    if not steps:
        steps = [f"Analyser la demande: {task}", "Rechercher les informations necessaires", "Rediger le resultat", "Sauvegarder dans l'espace de travail"]
    return steps[:6]


def _agent_execute_step(step: str, task: str) -> str:
    low = step.lower()
    if any(k in low for k in ("recherch", "cherche", "information", "web")):
        try:
            return ask_openrouter(f"Pour la tache '{task}', etape '{step}': donne une reponse concise et utile.")
        except Exception as exc:
            return f"Etape non executee ({exc})."
    if any(k in low for k in ("calcul", "math", "nombre")):
        return "Calcul effectue."
    try:
        return ask_openrouter(f"Pour la tache '{task}', etape '{step}': resume en une phrase.")
    except Exception as exc:
        return f"Etape non executee ({exc})."


def _run_agent_task(task: str) -> None:
    _ui_event("executing", command=f"Agent: {task}")
    speak_text("D'accord, je lance l'agent sur cette tache. Je vous tiens informe.")
    steps = _agent_plan(task)
    results = []
    for i, step in enumerate(steps, 1):
        _ui_event("executing", detail=f"Etape {i}/{len(steps)}: {step[:60]}")
        speak_text(f"Etape {i} sur {len(steps)} : {step}")
        res = _agent_execute_step(step, task)
        results.append(f"## {step}\n{res}")
        time.sleep(0.3)
    report = f"# Rapport agent - {task}\n\n" + "\n\n".join(results)
    out = WORKSPACE_DIR / ("agent_" + re.sub(r"[^a-z0-9]+", "_", task.lower())[:40] + ".md")
    try:
        out.write_text(report, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not save agent report: %s", exc)
    speak_text(f"Tache terminee. J'ai sauvegarde le rapport dans l'espace de travail sous {out.name}.")
    _ui_event("speaking", detail=f"Rapport: {out.name}")


def _handle_agent_command(command: str) -> bool:
    n = command.lower().strip()
    if any(x in n for x in ("ouvre l'espace de travail", "ouvre espace de travail", "ouvre le workspace", "ouvre workspace")):
        try:
            if sys.platform == "win32":
                os.startfile(str(WORKSPACE_DIR))
            else:
                subprocess.Popen(["xdg-open", str(WORKSPACE_DIR)])
            speak_text("J'ouvre l'espace de travail.")
        except Exception as exc:
            log.warning("Could not open workspace: %s", exc)
            speak_text("Je n'ai pas pu ouvrir l'espace de travail.")
        return True
    if any(x in n for x in ("liste les fichiers", "quels fichiers", "contenu de l'espace de travail", "fichiers du workspace")):
        try:
            files = [f.name for f in WORKSPACE_DIR.iterdir() if f.is_file()]
            if files:
                speak_text("Fichiers dans l'espace de travail : " + ", ".join(files[:10]))
            else:
                speak_text("L'espace de travail est vide.")
        except Exception as exc:
            speak_text("Je n'ai pas pu lire l'espace de travail.")
        return True
    triggers = ("agent ", "lance l'agent", "travaille sur", "prepare un rapport", "prepare un rapport",
                "analyse et cree", "analyse et cree", "fais un rapport", "genere un rapport", "genere un rapport",
                "agentique", "espace de travail")
    if any(t in n for t in triggers):
        task = n
        for t in triggers:
            task = task.replace(t, "").strip()
        if not task:
            speak_text("Precisez la tache a confier a l'agent.")
            return True
        threading.Thread(target=_run_agent_task, args=(task,), daemon=True).start()
        return True
    return False


def _handle_voice_command(text: str, confidence: float) -> None:
    clean = " ".join(text.strip().split())
    if not clean or confidence < VOICE_MIN_CONFIDENCE:
        return
    lowered = clean.lower()
    wake = VOICE_WAKE_WORD.lower().strip()
    known_without_wake = lowered.startswith((
        "cherche ", "recherche ", "ouvre ", "va sur ", "vas sur ", "rends-toi sur ", "rend toi sur ", "mets en pause", "pause ",
        "reprends ", "musique suivante", "titre suivant", "chanson suivante",
        "musique précédente", "titre précédent", "chanson précédente", "change la musique",
        "mets ", "joue ", "lance ",
        "stop", "arrête", "arrete", "quitte", "lis le presse", "contenu du presse",
        "monte le volume", "baisse le volume", "coupe le son", "remets le son",
        "ouvre discord", "ferme chrome", "lance minecraft", "ouvre mon dossier",
        "lis cette page", "résume cette page", "resume cette page", "quelle est l'information",
        "traduis ", "comment dit-on ", "comment dit on ", "ouvre le premier", "ouvre le troisième",
        "ouvre le troisieme", "reviens en arrière", "reviens en arriere", "descends dans la page",
        "clique sur vidéos", "clique sur videos", "https", "h t t p s",
        "allume", "éteins", "eteins", "thermostat", "volets", "scène", "scene",
        "météo", "meteo", "quelle heure", "quelle date", "rappelle", "calcule",
        "blague", "état du système", "etat du systeme", "diagnostic",
        "agent", "workspace", "espace de travail", "rapport", "travaille sur"
    ))
    if wake and wake not in lowered and not known_without_wake:
        return
    command = lowered.split(wake, 1)[1].strip(" ,.!?;:") if wake in lowered else lowered
    log.info("Voice command heard (confidence=%.2f): %s", confidence, clean)
    _ui_event("executing", command=clean)

    # Link mode can be activated by saying only "HTTPS", then dictating the URL
    # in the next utterance. It also accepts "HTTPS example point com" at once.
    if LINK_MODE_EVENT.is_set():
        if _is_stop_command(command):
            LINK_MODE_EVENT.clear()
        elif command in {"annule", "annuler", "quitte le mode lien", "sort du mode lien"}:
            LINK_MODE_EVENT.clear()
            speak_text("Mode lien annulé.")
            return
        else:
            LINK_MODE_EVENT.clear()
            _open_spoken_https_url(command)
            return

    https_remainder = _extract_https_remainder(command)
    if https_remainder is not None:
        if https_remainder:
            _open_spoken_https_url(https_remainder)
        else:
            LINK_MODE_EVENT.set()
            speak_text("Mode lien activé. Dites maintenant l'adresse du site, par exemple google point com.")
        return

    if _is_stop_command(command):
        log.info("Stop command received; shutting down Jarvis.")
        speak_text("D'accord loctoling, je ferme Jarvis, Spotify et les fenêtres que j'ai ouvertes.")
        _close_jarvis_opened_windows_and_spotify()
        PROGRAM_STOP_EVENT.set()
        _ui_event("stopped", detail="Jarvis fermé")
        return

    if _is_clipboard_read_command(command):
        log.info("Explicit local clipboard read requested.")
        speak_text(_read_clipboard_text())
        return

    if SECURITY_MODE and _is_private_data_request(command):
        log.warning("Blocked private-data command by security mode.")
        speak_text(_security_refusal())
        return

    if _handle_volume_command(command):
        return
    if _handle_application_command(command):
        return
    if any(k in command for k in ("lis cette page", "résume cette page", "resume cette page", "quelle est l'information", "traduis ", "comment dit-on ", "comment dit on ", "cherche ", "recherche ", "ouvre ", "va sur ", "vas sur ", "rends-toi sur ", "rend toi sur ")):
        _speak_acknowledge_async()
    if _handle_page_reading(command):
        return
    if _handle_translation(command):
        return
    if _handle_browser_navigation(command):
        return
    if _handle_targeted_search(command):
        return
    if _handle_domotic_command(command):
        return
    if _handle_smart_command(command):
        return
    if _handle_agent_command(command):
        return

    # Spotify remains running. These optional media commands control playback or search a requested title.
    spotify_match = re.match(r"^(?:mets|joue|lance)\s+(.+?)\s+(?:sur|dans)\s+spotify$", command)
    if spotify_match:
        _open_spotify_search(spotify_match.group(1))
    elif any(x in command for x in ("change la musique", "change de musique", "musique suivante", "titre suivant", "chanson suivante")):
        _send_media_key_win32(0xB0)
        speak_text("Je passe à la musique suivante.")
    elif any(x in command for x in ("mets en pause", "pause la musique", "pause spotify")):
        _send_media_key_win32(0xB3)
        speak_text("Musique mise en pause.")
    elif any(x in command for x in ("reprends la musique", "relance la musique", "reprends spotify")):
        _send_media_key_win32(0xB3)
        speak_text("Je reprends la musique.")
    elif any(x in command for x in ("musique précédente", "titre précédent", "chanson précédente")):
        _send_media_key_win32(0xB1)
        speak_text("Titre précédent.")
    elif command.startswith(("cherche ", "recherche ", "cherche sur google ", "recherche sur google ")):
        query = command
        for prefix in ("cherche sur google ", "recherche sur google ", "cherche ", "recherche "):
            if query.startswith(prefix):
                query = query[len(prefix):]
                break
        _speak_acknowledge_async()
        _open_google_search(query)
    elif command.startswith(("ouvre ", "va sur ", "vas sur ", "rends-toi sur ", "rend toi sur ")):
        prefixes = ("rends-toi sur ", "rend toi sur ", "ouvre ", "va sur ", "vas sur ")
        target = command
        for prefix in prefixes:
            if target.startswith(prefix):
                target = target[len(prefix):].strip()
                break
        _speak_acknowledge_async()
        if not _open_google_service(target) and not _open_requested_website(target):
            _open_google_search(target)
    elif any(x in command for x in ("cours de tencent", "action tencent", "tencent holdings")):
        open_tencent_stock_in_chrome()
        speak_text("J'ouvre le cours de Tencent Holdings Limited.")
    elif command:
        _speak_acknowledge_async()
        speak_text(ask_openrouter(command))
    else:
        speak_text("Oui loctoling, je vous écoute.")


def _open_spotify_search(query: str) -> None:
    q = " ".join(query.strip().split())
    if not q:
        speak_text("Quelle musique voulez-vous écouter sur Spotify ?")
        return
    if _spotify_app_installed():
        try:
            uri = "spotify:search:" + urllib.parse.quote(q)
            os.startfile(uri)
            _maximize_latest_window_by_exe(("Spotify.exe",), delay=2.0)
            speak_text(f"Je cherche {q} dans l'application Spotify.")
            return
        except OSError as exc:
            log.warning("Spotify app search failed: %s", exc)
    _open_google_search(f"Spotify {q}")
    speak_text("L'application Spotify n'est pas installée. Je cherche sur Google.")

def _send_media_key_win32(vk: int) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    KEYEVENTF_KEYUP = 0x0002
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def run_voice_listener() -> None:
    """Continuous Google speech recognition with faster end-of-speech detection."""
    if not VOICE_CONTROL_ENABLED:
        return
    try:
        import speech_recognition as sr
    except ImportError:
        log.error("SpeechRecognition is missing. Run: py -m pip install SpeechRecognition")
        return

    max_seconds = max(3.5, float(VOICE_CHUNK_SECONDS))
    silence_stop_seconds = 0.75
    speech_start_rms = 0.010
    silence_rms = 0.006
    try:
        device = _resolve_input_device_index((os.environ.get("JARVIS_INPUT_DEVICE") or "").strip())
    except Exception:
        device = sd.default.device[0]

    recognizer = sr.Recognizer()
    log.info(
        "Google voice control ready: continuous listening is active until STOP or program closure. Faster voice capture is enabled. Wake word '%s' is optional for Google/open commands.",
        VOICE_WAKE_WORD,
    )
    log.info("Voice microphone [%s]: %s", device, sd.query_devices(device)["name"])

    while not PROGRAM_STOP_EVENT.is_set():
        try:
            _ui_event("listening", level=0.0)
            chunks = []
            chunk_n = max(1, int(VOICE_SAMPLE_RATE * 0.08))
            max_blocks = max(1, int(max_seconds / 0.08))
            silence_needed = max(2, int(silence_stop_seconds / 0.08))
            started = False
            silence_blocks = 0
            total_blocks = 0
            with sd.InputStream(device=device, samplerate=VOICE_SAMPLE_RATE, channels=1, dtype="int16", blocksize=chunk_n) as vst:
                while total_blocks < max_blocks and not PROGRAM_STOP_EVENT.is_set():
                    data, _ = vst.read(chunk_n)
                    total_blocks += 1
                    chunks.append(data.copy())
                    lev = float(np.sqrt(np.mean((data.astype(np.float64)/32768.0)**2)))
                    _ui_event("listening", level=min(1.0, lev * 18.0))
                    if lev >= speech_start_rms:
                        started = True
                        silence_blocks = 0
                    elif started and lev <= silence_rms:
                        silence_blocks += 1
                        if silence_blocks >= silence_needed and total_blocks >= 4:
                            break
            if not chunks:
                continue
            recording = np.concatenate(chunks, axis=0).reshape(-1)
            if not started:
                continue
            allowed, reason = _speaker_allowed(recording, VOICE_SAMPLE_RATE)
            if not allowed:
                log.warning("Voice lock rejected audio: %s", reason)
                _ui_event("locked", detail=reason)
                continue
            _ui_event("recognizing", detail=reason)
            raw = recording.astype(np.int16).tobytes()
            audio = sr.AudioData(raw, VOICE_SAMPLE_RATE, 2)
            try:
                text = recognizer.recognize_google(audio, language=VOICE_LANGUAGE)
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                log.warning("Google speech recognition unavailable: %s", e)
                time.sleep(2)
                continue
            if text:
                _handle_voice_command(text, 1.0)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.warning("Voice capture failed: %s", e)
            time.sleep(1)

    log.info("Continuous voice listening stopped because Jarvis is closing.")


def say_jarvis_welcome() -> None:
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        speak_text(JARVIS_WELCOME_PHRASE.strip())

def play_song(uri: str) -> None:
    u = uri.strip()
    if not u:
        return
    try:
        if sys.platform == "win32":
            os.startfile(u)
            _maximize_latest_window_by_exe(("Spotify.exe",), delay=2.4)
        else:
            webbrowser.open(u)
    except OSError as e:
        log.warning("Could not open SONG_URI: %s", e)


def _chrome_executable() -> str | None:
    if sys.platform == "win32":
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ):
            if not base:
                continue
            p = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
            if os.path.isfile(p):
                return p
    return shutil.which("google-chrome") or shutil.which("chrome")


def _win32_sorted_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Each monitor as (left, top, right, bottom), sorted left-to-right then top-to-bottom."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    collected: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(RECT),
        wintypes.LPARAM,
    )
    def _cb(_hm, _hdc, lprc, _lp):
        r = lprc.contents
        collected.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, _cb, 0)
    collected.sort(key=lambda t: (t[0], t[1]))
    return collected


def _chrome_monitor_top_left(one_based_index: int) -> tuple[int, int]:
    """Top-left corner on virtual desktop for monitor N (1-based)."""
    l, t, _, _ = _chrome_monitor_bounds(one_based_index)
    return (l, t)


def _chrome_monitor_bounds(one_based_index: int) -> tuple[int, int, int, int]:
    """Monitor N as (left, top, right, bottom), 1-based index (sorted like other Chrome helpers)."""
    rects = _win32_sorted_monitor_rects()
    if not rects:
        return (0, 0, 1920, 1080)
    idx = one_based_index - 1
    if idx < 0:
        idx = 0
    if idx >= len(rects):
        log.warning(
            "Monitor %d requested but only %d found; using last monitor.",
            one_based_index,
            len(rects),
        )
        idx = len(rects) - 1
    return rects[idx]


def _chrome_monitor_pixel_size(one_based_index: int) -> tuple[int, int]:
    l, t, r, b = _chrome_monitor_bounds(one_based_index)
    return (max(320, r - l), max(240, b - t))


def _chrome_window_size() -> tuple[int, int]:
    w = (os.environ.get("CHROME_WINDOW_WIDTH") or "1400").strip()
    h = (os.environ.get("CHROME_WINDOW_HEIGHT") or "900").strip()
    try:
        return (max(400, int(w)), max(300, int(h)))
    except ValueError:
        return (1400, 900)


def _chrome_site_user_data_dir(site_key: str) -> str:
    p = Path(tempfile.gettempdir()) / "clap-trigger-chrome" / site_key
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _chrome_new_window_wait_timeout_s() -> float:
    try:
        return max(3.0, float((os.environ.get("CHROME_NEW_WINDOW_WAIT_S") or "25").strip()))
    except ValueError:
        return 25.0


def _chrome_top_level_browser_hwnds_win32() -> set[int]:
    """HWND ints for visible-or-minimized top-level Chrome browser windows."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "chrome.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 80 or h < 80:
            return True
        found.add(int(hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    return found


def _wait_new_chrome_hwnd_win32(before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.12)
        now = _chrome_top_level_browser_hwnds_win32()
        new = now - before
        if not new:
            continue
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        best: int | None = None
        best_area = 0
        for h in new:
            r = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(r)):
                a = max(0, r.right - r.left) * max(0, r.bottom - r.top)
                if a > best_area:
                    best_area = a
                    best = h
        if best is not None:
            return best
    return None


def _chrome_snap_window_to_monitor_win32(
    hwnd: int,
    one_based_monitor: int,
    *,
    fullscreen: bool,
    windowed_size: tuple[int, int] | None,
) -> None:
    import ctypes
    from ctypes import wintypes

    ml, mt, mr, mb = _chrome_monitor_bounds(one_based_monitor)
    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    SW_SHOWMAXIMIZED = 3
    HWND_TOP = 0
    SWP_SHOWWINDOW = 0x0040
    SWP_FRAMECHANGED = 0x0020
    flags = SWP_SHOWWINDOW | SWP_FRAMECHANGED

    user32.ShowWindow(hwnd, SW_RESTORE)
    if fullscreen:
        w, h = mr - ml, mb - mt
        x, y = ml, mt
    else:
        ww, wh = windowed_size or _chrome_window_size()
        w, h = ww, wh
        x = ml + max(0, (mr - ml - w) // 2)
        y = mt + max(0, (mb - mt - h) // 2)
    user32.SetWindowPos(hwnd, HWND_TOP, x, y, w, h, flags)

    if fullscreen:
        user32.ShowWindow(hwnd, SW_SHOWMAXIMIZED)
        KEYEVENTF_KEYUP = 0x0002
        VK_F11 = 0x7A
        fg = user32.GetForegroundWindow()
        tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, True)
        user32.SetForegroundWindow(hwnd)
        if tid_fg and tid_tgt:
            user32.AttachThreadInput(tid_fg, tid_tgt, False)
        user32.keybd_event(VK_F11, 0, 0, 0)
        user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _open_url_in_chrome(
    url: str,
    *,
    new_window: bool = True,
    label: str = "URL",
    window_position: tuple[int, int] | None = None,
    window_size: tuple[int, int] | None = None,
    fullscreen: bool = False,
    win32_post_fullscreen_monitor: int | None = None,
    user_data_dir: str | None = None,
) -> int | None:
    u = url.strip()
    if not u:
        return None
    chrome = _chrome_executable()
    try:
        if chrome:
            args = [chrome]
            if user_data_dir:
                args.append(f"--user-data-dir={user_data_dir}")
                args.append("--no-first-run")
            if new_window:
                args.append("--new-window")
            if window_position is not None:
                x, y = window_position
                args.append(f"--window-position={x},{y}")
            if window_size:
                args.append(f"--window-size={window_size[0]},{window_size[1]}")
            if fullscreen and not (
                sys.platform == "win32" and win32_post_fullscreen_monitor is not None
            ):
                args.append("--start-fullscreen")
            args.append(u)
            popen_kw: dict = {
                "args": args,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
            before: set[int] | None = None
            if sys.platform == "win32":
                before = _chrome_top_level_browser_hwnds_win32()
            subprocess.Popen(**popen_kw)
            if sys.platform == "win32":
                hwnd = _wait_new_chrome_hwnd_win32(before, _chrome_new_window_wait_timeout_s())
                if hwnd is not None:
                    JARVIS_OPENED_HWNDS.add(int(hwnd))
            if sys.platform == "win32" and win32_post_fullscreen_monitor is not None:
                mon = win32_post_fullscreen_monitor
                if hwnd is not None:
                    _chrome_snap_window_to_monitor_win32(
                        hwnd,
                        mon,
                        fullscreen=fullscreen,
                        windowed_size=window_size if not fullscreen else None,
                    )
                else:
                    log.warning(
                        "Chrome: timed out waiting for new window (%s); check "
                        "CHROME_NEW_WINDOW_WAIT_S or close extra Chrome instances.",
                        label,
                    )
        else:
            log.warning("Chrome not found; opening %s in default browser.", label)
            webbrowser.open(u)
            return None
        return int(hwnd) if sys.platform == "win32" and hwnd is not None else None
    except OSError as e:
        log.warning("Could not open %s in Chrome: %s", label, e)
        return None


def open_claude_in_chrome() -> None:
    if not OPEN_CLAUDE_CODE_IN_CHROME:
        return
    url = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = OPEN_CHROME_FULLSCREEN
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = CLAUDE_CHROME_MONITOR
        pos = _chrome_monitor_top_left(CLAUDE_CHROME_MONITOR)
        if fs:
            size = _chrome_monitor_pixel_size(CLAUDE_CHROME_MONITOR)
        else:
            size = _chrome_window_size()
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("claude")
    elif not fs:
        size = _chrome_window_size()
    else:
        size = None
    _open_url_in_chrome(
        url,
        new_window=True,
        label="Claude",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )


def open_tencent_stock_in_chrome() -> None:
    if not OPEN_TENCENT_STOCK_IN_CHROME:
        return
    url = (
        os.environ.get("TENCENT_STOCK_URL")
        or "https://www.google.com/finance/quote/0700:HKG?hl=fr"
    ).strip()
    pos: tuple[int, int] | None = None
    size: tuple[int, int] | None = None
    fs = False
    post_mon: int | None = None
    user_data: str | None = None
    if sys.platform == "win32":
        post_mon = TENCENT_CHROME_MONITOR
        x, y, w, h = _small_rect_on_monitor(TENCENT_CHROME_MONITOR, 980, 760, anchor="top_right", margin=18)
        pos = (x, y)
        size = (w, h)
        if CHROME_SEPARATE_SITE_PROFILES:
            user_data = _chrome_site_user_data_dir("tencent")
    else:
        size = (980, 760)
    hwnd = _open_url_in_chrome(
        url,
        new_window=True,
        label="Cours Tencent Holdings",
        window_position=pos,
        window_size=size,
        fullscreen=fs,
        win32_post_fullscreen_monitor=post_mon,
        user_data_dir=user_data,
    )
    if hwnd is not None and sys.platform == "win32":
        threading.Thread(target=lambda: (time.sleep(1.6), _win32_maximize_window(hwnd)), daemon=True).start()


def _cursor_executable() -> str | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        for sub in ("Programs\\cursor\\Cursor.exe", "Programs\\Cursor\\Cursor.exe"):
            if local:
                p = os.path.join(local, *sub.split("\\"))
                if os.path.isfile(p):
                    return p
    return shutil.which("cursor")


def _cursor_largest_main_hwnd_win32() -> int | None:
    """Largest top-level Cursor.exe window (visible or minimized)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    candidates: list[tuple[int, wintypes.HWND]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd: wintypes.HWND, _lp: wintypes.LPARAM) -> bool:
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        if not user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == 0:
            return True
        hproc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not hproc:
            return True
        try:
            buf = ctypes.create_unicode_buffer(4096)
            sz = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(hproc, 0, buf, ctypes.byref(sz)):
                return True
            exe_path = buf.value
        finally:
            kernel32.CloseHandle(hproc)
        if os.path.basename(exe_path).lower() != "cursor.exe":
            return True
        r = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 200 or h < 200:
            return True
        candidates.append((w * h, hwnd))
        return True

    user32.EnumWindows(_enum, 0)
    if not candidates:
        return None
    return int(max(candidates, key=lambda t: t[0])[1])


def _cursor_foreground_hwnd_win32(hwnd: int) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    SW_RESTORE = 9
    user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    tid_tgt = user32.GetWindowThreadProcessId(hwnd, None)
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, True)
    user32.SetForegroundWindow(hwnd)
    if tid_fg and tid_tgt:
        user32.AttachThreadInput(tid_fg, tid_tgt, False)


def _cursor_send_f11_fullscreen_win32(hwnd: int) -> None:
    """F11 toggles Zen/fullscreen in Cursor (Electron)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    KEYEVENTF_KEYUP = 0x0002
    VK_F11 = 0x7A
    _cursor_foreground_hwnd_win32(hwnd)
    user32.keybd_event(VK_F11, 0, 0, 0)
    user32.keybd_event(VK_F11, 0, KEYEVENTF_KEYUP, 0)


def _focus_existing_cursor_window_win32() -> bool:
    """Bring an existing Cursor.exe main window to the foreground (no new process)."""
    if sys.platform != "win32":
        return False
    hwnd = _cursor_largest_main_hwnd_win32()
    if hwnd is None:
        return False
    _cursor_foreground_hwnd_win32(hwnd)
    return True


def run_double_clap_actions() -> None:
    """Run outside the mic loop so sleeps do not stall capture."""
    play_song(SONG_URI)
    open_tencent_stock_in_chrome()
    if JARVIS_WELCOME_ENABLED and JARVIS_WELCOME_PHRASE.strip():
        delay = max(0.0, JARVIS_AFTER_SONG_DELAY_S)
        if delay:
            time.sleep(delay)
        threading.Thread(target=say_jarvis_welcome, daemon=True).start()
    # Security: do not launch developer tools or unrelated local applications.


def open_cursor_window() -> None:
    if not FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP and not OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        return
    exe = _cursor_executable()
    if not exe:
        log.warning(
            "Could not find Cursor (install app or add the `cursor` command to PATH)."
        )
        return
    popen_kw: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        popen_kw["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
            focused = (
                sys.platform == "win32" and _focus_existing_cursor_window_win32()
            )
            if not focused:
                subprocess.Popen([exe], **popen_kw)
        if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
            subprocess.Popen([exe, "-n"], **popen_kw)
    except OSError as e:
        log.warning("Could not start or focus Cursor: %s", e)
        return
    if sys.platform == "win32" and CURSOR_OPEN_FULLSCREEN:
        time.sleep(0.5)
        hwnd = _cursor_largest_main_hwnd_win32()
        if hwnd is not None:
            _cursor_send_f11_fullscreen_win32(hwnd)
        else:
            log.warning("Cursor fullscreen: no Cursor window found to send F11.")


def main() -> int:
    _start_jarvis_ui()
    _minimize_console_window()
    _ui_event("waiting", detail="Frappez deux fois dans vos mains")
    blocksize = block_samples()
    noise_floor = 1e-4
    last_logged_double = 0.0
    first_clap_time: float | None = None
    spike_armed = True
    welcome_sequence_done = False

    log.info(
        "Listening (double clap: %.2f–%.2fs apart, rate=%d, block=%d ms, "
        "spike_ratio=%.1f, cooldown=%.2fs). Ctrl+C to stop.",
        MIN_DOUBLE_GAP_S,
        MAX_DOUBLE_GAP_S,
        SAMPLE_RATE,
        BLOCK_MS,
        SPIKE_RATIO,
        COOLDOWN_S,
    )
    if SONG_URI.strip():
        log.info("Double clap opens this track: %s", SONG_URI.strip())
    else:
        log.info("SONG_URI is empty — set it to play one song on each double clap.")
    if FOCUS_EXISTING_CURSOR_ON_DOUBLE_CLAP:
        log.info(
            "Double clap will foreground an existing Cursor window (Windows API); "
            "falls back to launching Cursor if none is running."
        )
    if OPEN_NEW_CURSOR_ON_DOUBLE_CLAP:
        log.info("Double clap will also open a new Cursor window (-n).")
    if CURSOR_OPEN_FULLSCREEN and sys.platform == "win32":
        log.info("Cursor will be sent F11 for fullscreen after focus/launch.")
    if OPEN_CLAUDE_CODE_IN_CHROME:
        cu = (os.environ.get("CLAUDE_CODE_URL") or "https://claude.ai/new").strip()
        log.info(
            "After Spotify, open Claude in Chrome%s on monitor %d: %s",
            " fullscreen" if OPEN_CHROME_FULLSCREEN else "",
            CLAUDE_CHROME_MONITOR,
            cu,
        )
    if OPEN_TENCENT_STOCK_IN_CHROME:
        bu = (
            os.environ.get("TENCENT_STOCK_URL")
            or "https://www.google.com/finance/quote/0700:HKG?hl=fr"
        ).strip()
        log.info(
            "After Spotify, open Tencent Holdings in Chrome%s on monitor %d: %s",
            " fullscreen" if OPEN_CHROME_FULLSCREEN else "",
            TENCENT_CHROME_MONITOR,
            bu,
        )
    if JARVIS_WELCOME_ENABLED:
        ev, em, ef, er = elevenlabs_env_config()
        log.info(
            "After song + %.2fs: %r (ElevenLabs voice=%s, model=%s, format=%s, pcm_rate=%d)",
            JARVIS_AFTER_SONG_DELAY_S,
            JARVIS_WELCOME_PHRASE.strip(),
            ev or "(unset)",
            em,
            ef,
            er,
        )

    input_idx = _choose_input_device(blocksize)

    activated = False

    try:
        with sd.InputStream(
            device=input_idx,
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype="float32",
            blocksize=blocksize,
        ) as stream:
            while True:
                data, overflowed = stream.read(blocksize)
                if overflowed:
                    log.warning("Input overflow; try a larger BLOCK_MS")

                level = rms_mono(data)

                quiet_gate = noise_floor * QUIET_GATE_MULT
                if level < quiet_gate:
                    noise_floor = NOISE_FLOOR_ALPHA * noise_floor + (
                        1.0 - NOISE_FLOOR_ALPHA
                    ) * level
                    noise_floor = max(noise_floor, 1e-7)

                threshold = max(noise_floor * SPIKE_RATIO, MIN_RMS)
                now = time.monotonic()
                retrigger_level = threshold * RETRIGGER_RATIO

                if level < retrigger_level:
                    spike_armed = True

                if (
                    spike_armed
                    and level >= threshold
                    and (now - last_logged_double) >= COOLDOWN_S
                ):
                    spike_armed = False
                    if first_clap_time is None:
                        first_clap_time = now
                    else:
                        gap = now - first_clap_time
                        if gap < MIN_DOUBLE_GAP_S:
                            pass
                        elif gap <= MAX_DOUBLE_GAP_S:
                            first_clap_time = None
                            last_logged_double = now
                            if not welcome_sequence_done:
                                welcome_sequence_done = True
                                log.info(
                                    "Double clap detected (gap=%.3fs, rms=%.5f, "
                                    "noise_floor=%.5f, threshold=%.5f) — running welcome once",
                                    gap,
                                    level,
                                    noise_floor,
                                    threshold,
                                )
                                activated = True
                                _ui_event("activated", detail="Salut loctoling")
                                threading.Thread(
                                    target=run_double_clap_actions, daemon=True
                                ).start()
                                log.info("Clap microphone released; switching to continuous voice control.")
                                break
                        else:
                            first_clap_time = now

        if activated and VOICE_CONTROL_ENABLED:
            run_voice_listener()

    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0
    except sd.PortAudioError as e:
        log.error("Audio error: %s", e)
        log.error("If PortAudio fails, install/repair drivers or try another SAMPLE_RATE.")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
