import sys
import os
import traceback

# Prevent duplicate execution if imported by other modules (e.g. web_server)
if __name__ == '__main__':
    sys.modules['gsm_gemini_gateway'] = sys.modules.get('__main__')

sys.coinit_flags = 0

# Support windowed / no-console execution with file logging
_log_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else os.getcwd()
_log_file = os.path.join(_log_dir, "callink_runtime.log")

class TeeLogger:
    def __init__(self, orig_stream=None):
        self.orig_stream = orig_stream
        try:
            self.f = open(_log_file, "a", encoding="utf-8")
        except Exception:
            self.f = None

    def write(self, text):
        if not text:
            return
        if self.orig_stream:
            try:
                self.orig_stream.write(text)
                self.orig_stream.flush()
            except Exception:
                pass
        if self.f:
            try:
                self.f.write(text)
                self.f.flush()
            except Exception:
                pass
        try:
            from web_server import add_terminal_log
            add_terminal_log(text)
        except Exception:
            pass

    def flush(self):
        if self.orig_stream:
            try: self.orig_stream.flush()
            except Exception: pass
        if self.f:
            try: self.f.flush()
            except Exception: pass

    def reconfigure(self, **kwargs):
        if self.orig_stream and hasattr(self.orig_stream, "reconfigure"):
            try: self.orig_stream.reconfigure(**kwargs)
            except Exception: pass

if not isinstance(sys.stdout, TeeLogger):
    _orig_stdout = sys.stdout
    _orig_stderr = sys.stderr
    logger = TeeLogger(_orig_stdout)
    sys.stdout = logger
    sys.stderr = logger

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import asyncio
import audioop
import ctypes
from ctypes import wintypes
import os
import queue
import re
import threading
import time
import traceback
import webbrowser
import winreg
import comtypes
import comtypes.client
from comtypes import GUID, COMMETHOD, HRESULT, POINTER, IUnknown

# MUST import winrt before sounddevice to ensure COM is initialized in MTA mode
try:
    import winrt
    from winrt.windows.applicationmodel.calls import PhoneCallManager
    import winrt.windows.devices.bluetooth as bt
    import winrt.windows.devices.radios as radios
    import winrt.windows.devices.enumeration as enum
except Exception:
    pass

import sounddevice as sd
import numpy as np

try:
    from scipy.signal import resample_poly
    _HAVE_SCIPY = True
except ImportError:
    _HAVE_SCIPY = False


class HFPResampler:
    """High-fidelity 24kHz -> 16kHz downsampler using audioop.ratecv with continuous internal state.
    Maintains mathematically perfect sample continuity across WebSocket chunks to eliminate all
    boundary crackle, clicking, fluttering, and voice distortion."""
    def __init__(self):
        self._state = None

    def resample(self, pcm_24k: bytes) -> bytes:
        if not pcm_24k:
            return b""
        pcm_16k, self._state = audioop.ratecv(pcm_24k, 2, 1, GEMINI_OUT_RATE, HFP_RATE, self._state)
        if pcm_16k:
            # Mild voice presence gain (+1.2 dB = 1.15x) for crystal-clear telephone audibility
            pcm_16k = audioop.mul(pcm_16k, 2, 1.15)
        return pcm_16k

    def reset(self):
        self._state = None


import multiprocessing
multiprocessing.freeze_support()

def ensure_single_instance():
    """Prevents multiple Callink processes from running concurrently and fighting over Phone Link / audio."""
    try:
        import psutil
        my_pid = os.getpid()
        my_ppid = os.getppid() if hasattr(os, "getppid") else None
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                pid = proc.info['pid']
                if pid == my_pid or pid == my_ppid:
                    continue
                pname = (proc.info.get('name') or '').lower()
                is_duplicate = False
                if pname == 'callink.exe':
                    is_duplicate = True
                elif 'python' in pname:
                    cmdline = proc.info.get('cmdline') or []
                    if any('gsm_gemini_gateway.py' in str(arg).lower() for arg in cmdline):
                        is_duplicate = True
                if is_duplicate:
                    print("\n[System Warning] Another instance of Callink is already running.")
                    print("[System Warning] Opening active dashboard in browser and exiting duplicate instance...")
                    try:
                        import webbrowser
                        webbrowser.open("http://localhost:5000")
                    except Exception:
                        pass
                    sys.exit(0)
            except Exception:
                pass
        return True
    except Exception:
        return True

if __name__ == '__main__':
    ensure_single_instance()

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# pip install google-genai
from google import genai
from google.genai import types

from call_recorder import CallRecorder
from web_server import WebGatewayServer, load_settings, DEFAULT_SETTINGS, set_in_call_state
import knowledge_manager as km
import outbound_dialer as dialer


def ensure_gemini_api_key(allow_web_prompt: bool = True) -> str:
    """Validates GEMINI_API_KEY presence, prompting the user if missing."""
    key = os.environ.get("GEMINI_API_KEY")
    if key and key.strip():
        return key.strip()

    # Try loading from local directory next to the executable / script
    app_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else os.getcwd()
    env_file = os.path.join(app_dir, ".env")
    if not os.path.exists(env_file):
        env_file = os.path.join(os.getcwd(), ".env")

    if os.path.exists(env_file):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
            key = os.environ.get("GEMINI_API_KEY")
            if key and key.strip():
                return key.strip()
        except Exception:
            pass

    if allow_web_prompt:
        print("================================================================")
        print(" [SETUP] Gemini API Key is not set in environment or .env")
        print(" You can configure it directly on the Callink Web Dashboard!")
        print(" Or enter it now in this console.")
        print(" Get a free key at: https://aistudio.google.com/apikey")
        print("================================================================")

    # If stdin is an interactive terminal and not packaged headless
    try:
        if sys.stdin and sys.stdin.isatty():
            key_input = input("Enter GEMINI_API_KEY (or press Enter to configure via Web UI): ").strip()
            if key_input:
                os.environ["GEMINI_API_KEY"] = key_input
                try:
                    with open(env_file, "w") as f:
                        f.write(f"GEMINI_API_KEY={key_input}\n")
                    print(f"[SETUP] API key saved to {env_file}\n")
                except Exception:
                    pass
                return key_input
    except Exception:
        pass

    return ""


def prevent_sleep():
    """Instructs Windows to prevent entering sleep/standby while the gateway is active."""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception:
        pass


def start_stay_awake_heartbeat():
    """Starts a continuous daemon thread that refreshes Windows stay-awake state every 15s."""
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ES_DISPLAY_REQUIRED = 0x00000002

    def heartbeat_worker():
        while True:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
            except Exception:
                pass
            time.sleep(15)

    thread = threading.Thread(target=heartbeat_worker, daemon=True, name="StayAwakeHeartbeat")
    thread.start()


def allow_sleep():
    """Restores default Windows sleep/standby behavior."""
    ES_CONTINUOUS = 0x80000000
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass


def is_pc_voip_call_active() -> bool:
    """Detects if a PC desktop VoIP application (WhatsApp, Teams, Skype, Zoom, Telegram)
    is actively ringing or in a call on the computer's soundcard, so Callink can ignore it
    and ensure it only engages for genuine cellular SIM calls from the paired phone."""
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            p = s.Process
            if p and any(app in p.name().lower() for app in ["whatsapp", "telegram", "skype", "teams", "zoom"]):
                if s.State == 1:  # AudioSessionStateActive
                    return True
    except Exception:
        pass
    return False


def set_phone_link_audio_silenced(silenced: bool = True):
    """Silences Microsoft Phone Link (PhoneExperienceHost) on BOTH PC speakers and PC microphone
    without spamming COM calls, completely eliminating audio leaks, clicking, and beep artifacts."""
    try:
        import comtypes
        com_initialized = False
        try:
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        try:
            from pycaw.pycaw import AudioUtilities, IAudioSessionManager2, ISimpleAudioVolume, IAudioSessionControl2
            from comtypes import CLSCTX_ALL
            import psutil

            # 1. Silence Speaker Output (Render)
            for s in AudioUtilities.GetAllSessions():
                p = s.Process
                if p and "phoneexperiencehost" in p.name().lower():
                    vol = s.SimpleAudioVolume
                    target_mute = 1 if silenced else 0
                    if vol.GetMute() != target_mute:
                        vol.SetMute(target_mute, None)
                    if silenced and vol.GetMasterVolume() > 0.0:
                        vol.SetMasterVolume(0.0, None)
                    elif not silenced and vol.GetMasterVolume() == 0.0:
                        vol.SetMasterVolume(1.0, None)

            # 2. Silence Microphone Input (Capture)
            try:
                mic = AudioUtilities.GetMicrophone()
                if mic:
                    mgr = mic.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                    enumerator = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
                    for i in range(enumerator.GetCount()):
                        ctl = enumerator.GetSession(i)
                        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                        pid = ctl2.GetProcessId()
                        if pid > 0:
                            try:
                                proc_name = psutil.Process(pid).name().lower()
                            except Exception:
                                proc_name = ""
                            if "phoneexperiencehost" in proc_name:
                                vol = ctl.QueryInterface(ISimpleAudioVolume)
                                target_mute = 1 if silenced else 0
                                if vol.GetMute() != target_mute:
                                    vol.SetMute(target_mute, None)
                                if silenced and vol.GetMasterVolume() > 0.0:
                                    vol.SetMasterVolume(0.0, None)
                                elif not silenced and vol.GetMasterVolume() == 0.0:
                                    vol.SetMasterVolume(1.0, None)
            except Exception:
                pass
        finally:
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
    except Exception:
        pass


class PhoneLinkAudioSilencer:
    """Watchdog thread that ensures Phone Link's speaker & mic stay muted without redundant calls."""
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="PhoneLinkSilencer")
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            set_phone_link_audio_silenced(True)
            time.sleep(1.0)

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        set_phone_link_audio_silenced(False)


HFP_RATE = 16000       # native rate of the Windows HFP endpoint
HFP_BLOCK = 320        # 20ms block @ 16kHz (320 samples = 640 bytes)

# Gemini Live API audio contract:
#   input  (you -> model):  16-bit PCM, mono, 16 kHz
#   output (model -> you):  16-bit PCM, mono, 24 kHz
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Barge-in VAD parameters (for client-side speech detection while AI is playing)
VAD_RMS_THRESHOLD = 950
VAD_TRIGGER_CONSECUTIVE = 2       # ~40ms of sustained speech above threshold
VAD_INITIAL_GRACE_PERIOD = 0.20   # 200ms grace period after AI speech starts before client barge-in triggers


# ==============================================================================
# WINDOWS CORE AUDIO (WASAPI) NATIVE COM INTERFACES
# ==============================================================================
CLSID_MMDeviceEnumerator = GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
IID_IMMDeviceEnumerator = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
IID_IMMDevice = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
IID_IAudioRenderClient = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")

class WAVEFORMATEX(ctypes.Structure):
    _fields_ = [
        ("wFormatTag", wintypes.WORD),
        ("nChannels", wintypes.WORD),
        ("nSamplesPerSec", wintypes.DWORD),
        ("nAvgBytesPerSec", wintypes.DWORD),
        ("nBlockAlign", wintypes.WORD),
        ("wBitsPerSample", wintypes.WORD),
        ("cbSize", wintypes.WORD),
    ]

class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (['out'], POINTER(POINTER(ctypes.c_byte)), "ppData"), (['out'], POINTER(wintypes.DWORD), "pNumFramesToRead"), (['out'], POINTER(wintypes.DWORD), "pdwFlags"), (['out'], POINTER(ctypes.c_ulonglong), "pu64DevicePosition"), (['out'], POINTER(ctypes.c_ulonglong), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (['in'], wintypes.DWORD, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize", (['out'], POINTER(wintypes.DWORD), "pNumFramesInNextPacket")),
    ]

class IAudioRenderClient(IUnknown):
    _iid_ = IID_IAudioRenderClient
    _methods_ = [
        COMMETHOD([], HRESULT, "GetBuffer", (['in'], wintypes.DWORD, "NumFramesRequested"), (['out'], POINTER(POINTER(ctypes.c_byte)), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (['in'], wintypes.DWORD, "NumFramesWritten"), (['in'], wintypes.DWORD, "dwFlags")),
    ]

class IAudioClient(IUnknown):
    _iid_ = IID_IAudioClient
    _methods_ = [
        COMMETHOD([], HRESULT, "Initialize", (['in'], ctypes.c_int, "ShareMode"), (['in'], wintypes.DWORD, "StreamFlags"), (['in'], ctypes.c_longlong, "hnsBufferDuration"), (['in'], ctypes.c_longlong, "hnsPeriodicity"), (['in'], POINTER(WAVEFORMATEX), "pFormat"), (['in'], ctypes.c_void_p, "AudioSessionGuid")),
        COMMETHOD([], HRESULT, "GetBufferSize", (['out'], POINTER(wintypes.DWORD), "pNumBufferFrames")),
        COMMETHOD([], HRESULT, "GetStreamLatency", (['out'], POINTER(ctypes.c_longlong), "phnsLatency")),
        COMMETHOD([], HRESULT, "GetCurrentPadding", (['out'], POINTER(wintypes.DWORD), "pNumPaddingFrames")),
        COMMETHOD([], HRESULT, "IsFormatSupported", (['in'], ctypes.c_int, "ShareMode"), (['in'], POINTER(WAVEFORMATEX), "pFormat"), (['out'], POINTER(POINTER(WAVEFORMATEX)), "ppClosestMatch")),
        COMMETHOD([], HRESULT, "GetMixFormat", (['out'], POINTER(POINTER(WAVEFORMATEX)), "ppDeviceFormat")),
        COMMETHOD([], HRESULT, "GetDevicePeriod", (['out'], POINTER(ctypes.c_longlong), "phnsDefaultDevicePeriod"), (['out'], POINTER(ctypes.c_longlong), "phnsMinimumDevicePeriod")),
        COMMETHOD([], HRESULT, "Start"),
        COMMETHOD([], HRESULT, "Stop"),
        COMMETHOD([], HRESULT, "Reset"),
        COMMETHOD([], HRESULT, "SetEventHandle", (['in'], wintypes.HANDLE, "eventHandle")),
        COMMETHOD([], HRESULT, "GetService", (['in'], POINTER(GUID), "iid"), (['out'], POINTER(POINTER(IUnknown)), "ppv")),
    ]

class IMMDevice(IUnknown):
    _iid_ = IID_IMMDevice
    _methods_ = [
        COMMETHOD([], HRESULT, "Activate", (['in'], POINTER(GUID), "iid"), (['in'], wintypes.DWORD, "dwClsCtx"), (['in'], ctypes.c_void_p, "pActivationParams"), (['out'], POINTER(POINTER(IUnknown)), "ppInterface")),
        COMMETHOD([], HRESULT, "OpenPropertyStore", (['in'], wintypes.DWORD, "stgmAccess"), (['out'], POINTER(POINTER(IUnknown)), "ppProperties")),
        COMMETHOD([], HRESULT, "GetId", (['out'], POINTER(wintypes.LPWSTR), "ppstrId")),
        COMMETHOD([], HRESULT, "GetState", (['out'], POINTER(wintypes.DWORD), "pdwState")),
    ]

class IMMDeviceEnumerator(IUnknown):
    _iid_ = IID_IMMDeviceEnumerator
    _methods_ = [
        COMMETHOD([], HRESULT, "EnumAudioEndpoints", (['in'], ctypes.c_int, "dataFlow"), (['in'], wintypes.DWORD, "dwStateMask"), (['out'], POINTER(POINTER(IUnknown)), "ppDevices")),
        COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (['in'], ctypes.c_int, "dataFlow"), (['in'], ctypes.c_int, "role"), (['out'], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
        COMMETHOD([], HRESULT, "GetDevice", (['in'], wintypes.LPCWSTR, "pwstrId"), (['out'], POINTER(POINTER(IMMDevice)), "ppDevice")),
    ]

_last_bth_phone_name = "Bluetooth Phone"


def find_active_bthhfenum_endpoints(preferred_phone: str = ""):
    """Finds active Bluetooth HFP audio endpoints (capture & render) via Windows MMDevice registry & COM.
    Filters out unplugged devices and prioritizes active connected phones.
    Returns (capture_id, render_id, device_name) or (None, None, None)."""
    bth_map = {}
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Enum\BTHHFENUM\BthHFPAudio")
        for i in range(winreg.QueryInfoKey(k)[0]):
            sub = winreg.EnumKey(k, i)
            skey = winreg.OpenKey(k, sub)
            try:
                friendly, _ = winreg.QueryValueEx(skey, "FriendlyName")
                m = re.search(r'\((.*?)\)', friendly)
                phone_name = m.group(1) if m else friendly
                bth_map[sub.lower()] = phone_name
            except Exception:
                pass
    except Exception:
        pass

    candidates = []
    cap_active = {}
    ren_active = {}

    for flow, flow_code, target_dict in [("Capture", "0.0.1.00000000", cap_active), ("Render", "0.0.0.00000000", ren_active)]:
        reg_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\{flow}"
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
            for i in range(winreg.QueryInfoKey(key)[0]):
                guid = winreg.EnumKey(key, i)
                dkey = winreg.OpenKey(key, guid)
                state, _ = winreg.QueryValueEx(dkey, "DeviceState")
                if state != 1:  # Only DEVICE_STATE_ACTIVE (1)
                    continue
                pkey = winreg.OpenKey(key, rf"{guid}\Properties")
                for v_idx in range(winreg.QueryInfoKey(pkey)[1]):
                    _, val_data, _ = winreg.EnumValue(pkey, v_idx)
                    val_str = str(val_data).lower()
                    if "bthhfenum" in val_str or "bthhfpaudio" in val_str:
                        for sub_id, name in bth_map.items():
                            if sub_id in val_str:
                                target_dict[sub_id] = (f"{{{flow_code}}}.{guid}", name)
                                break
                        else:
                            target_dict[guid] = (f"{{{flow_code}}}.{guid}", "Bluetooth Phone")
                        break
        except Exception:
            pass

    common_keys = set(cap_active.keys()) & set(ren_active.keys())
    for k in common_keys:
        cap_id, name = cap_active[k]
        ren_id, _ = ren_active[k]
        candidates.append((cap_id, ren_id, name))

    if not candidates:
        if cap_active and ren_active:
            c_k = next(iter(cap_active))
            r_k = next(iter(ren_active))
            return cap_active[c_k][0], ren_active[r_k][0], cap_active[c_k][1]
        return None, None, None

    if preferred_phone:
        p_low = preferred_phone.lower()
        for cap_id, ren_id, name in candidates:
            if p_low in name.lower():
                return cap_id, ren_id, name
        print(f"[Audio Link] Preferred phone '{preferred_phone}' not active. Using connected phone '{candidates[0][2]}'.")

    return candidates[0]


def find_hfp_devices(quiet: bool = False, force_refresh: bool = False):
    """Discovers Windows Bluetooth Hands-Free HF Audio input and output endpoints.
    Directly routes through native Windows WDM-KS PortAudio streams, guaranteeing
    active bidirectional SCO packet delivery for both incoming caller audio and AI playback."""
    global _last_bth_phone_name
    preferred_phone = os.environ.get("PHONE_NAME", "").strip()

    # 1. Query Windows registry to detect active connected phone name
    _, _, active_phone_name = find_active_bthhfenum_endpoints(preferred_phone=preferred_phone)
    if active_phone_name:
        _last_bth_phone_name = active_phone_name
        if not preferred_phone:
            preferred_phone = active_phone_name

    try:
        devices = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception as e:
        if not quiet:
            print(f"[Audio Error] Could not query audio devices: {e}")
        return None, None

    # Host API preference ranking: Windows WDM-KS is the native kernel driver for bthhfenum.sys
    api_ranks = {"windows wdm-ks": 10, "windows wasapi": 4, "windows directsound": 3, "mme": 2}

    def get_api_score(dev):
        hostapi_idx = dev.get("hostapi", -1)
        if 0 <= hostapi_idx < len(apis):
            api_name = apis[hostapi_idx].get("name", "").lower()
            return api_ranks.get(api_name, 0)
        return 0

    preferred_phone_low = preferred_phone.lower()

    def score_device(i, dev, is_input: bool):
        name = dev["name"].lower()
        if "hands-free" not in name and "handsfree" not in name:
            return -1

        channels = dev["max_input_channels"] if is_input else dev["max_output_channels"]
        if channels <= 0:
            return -1

        score = 0

        # 1. Connected phone name match
        if preferred_phone_low and preferred_phone_low in name:
            score += 50

        # 2. Prefer telephony endpoints created by Microsoft Phone Link (Hands-Free HF Audio)
        if "hands-free hf audio" in name:
            score += 25
        elif "hands-free" in name:
            score += 5

        # 3. Prefer 16kHz native rate (wideband mSBC)
        sr = int(dev.get("default_samplerate", 0))
        if sr == HFP_RATE:
            score += 20
        elif sr == 8000:
            score += 5

        # 4. Host API stability
        score += get_api_score(dev)

        # 5. VALIDATE: Test that PortAudio can actually configure this endpoint at HFP_RATE
        try:
            if is_input:
                sd.check_input_settings(device=i, samplerate=HFP_RATE, channels=1, dtype="int16")
            else:
                sd.check_output_settings(device=i, samplerate=HFP_RATE, channels=1, dtype="int16")
            score += 30
        except Exception:
            try:
                if is_input:
                    sd.check_input_settings(device=i, samplerate=8000, channels=1, dtype="int16")
                else:
                    sd.check_output_settings(device=i, samplerate=8000, channels=1, dtype="int16")
                score += 5
            except Exception:
                score -= 100

        return score

    best_in_idx = None
    best_in_score = -1

    best_out_idx = None
    best_out_score = -1

    for i, device in enumerate(devices):
        in_s = score_device(i, device, is_input=True)
        if in_s > best_in_score:
            best_in_idx = i
            best_in_score = in_s

        out_s = score_device(i, device, is_input=False)
        if out_s > best_out_score:
            best_out_idx = i
            best_out_score = out_s

    if best_in_idx is None or best_out_idx is None or best_in_score < 0 or best_out_score < 0:
        if not quiet:
            print("[INFO] Bluetooth HFP device not yet detected. Ready for phone pairing via Phone Link.")
        return None, None

    if not quiet:
        api_in = apis[devices[best_in_idx]["hostapi"]]["name"] if 0 <= devices[best_in_idx]["hostapi"] < len(apis) else "?"
        api_out = apis[devices[best_out_idx]["hostapi"]]["name"] if 0 <= devices[best_out_idx]["hostapi"] < len(apis) else "?"
        print(f"HFP INPUT  : [{best_in_idx}] {devices[best_in_idx]['name']} ({api_in})")
        print(f"HFP OUTPUT : [{best_out_idx}] {devices[best_out_idx]['name']} ({api_out})")

    return best_in_idx, best_out_idx


class HFPBridge:
    """Reads/writes the Windows Bluetooth HFP device.
    Features native Windows Core Audio (WASAPI) hardware streaming with automatic PCM conversion,
    and a high-resilience byte-level FIFO playback buffer with Jitter Prebuffering
    to eliminate audio stuttering, clicks, and dropouts over Bluetooth SCO."""

    def __init__(self, in_dev=None, out_dev=None):
        self.to_ai = queue.Queue(maxsize=300)

        self.input_device = in_dev
        self.output_device = out_dev

        self.is_wasapi = isinstance(in_dev, str) and isinstance(out_dev, str) and in_dev.startswith("{")
        self.phone_name = _last_bth_phone_name
        self._wasapi_running = False
        self._cap_thread = None
        self._ren_thread = None

        self.input_stream = None
        self.output_stream = None

        self._out_resample_state = None
        self.total_input_frames = 0
        self.total_output_frames = 0

        self.playback_started_at = None
        self.last_interrupted_at = 0.0
        self.last_played_chunk = b"\x00" * 640
        self.ai_record_queue = queue.Queue(maxsize=1000)

        # High-resilience byte-level playback buffer with Jitter Prebuffering
        self._playback_lock = threading.Lock()
        self._playback_bytes = bytearray()
        self._is_buffering = True
        # 80ms pre-buffer cushion (16000 samples/sec * 2 bytes/sample * 0.08s = 2560 bytes)
        self.prebuffer_target_bytes = 2560
        self.has_sco_audio = False
        self.resampler = HFPResampler()
        self.last_audio_sent_time = 0.0
        self._empty_since = None

    def input_callback(self, indata, frames, time_info, status):
        """PortAudio fallback callback for standard audio devices."""
        try:
            pcm16 = indata[:, 0].tobytes()
            self.total_input_frames += frames
            try:
                self.to_ai.put_nowait(pcm16)
            except queue.Full:
                pass
        except Exception as e:
            print("HFP INPUT CALLBACK ERROR:", e)

    def push_playback_audio(self, pcm16: bytes):
        """Append synthesized AI audio to the playback buffer and disengage buffering once threshold is met."""
        if not pcm16:
            return
        with self._playback_lock:
            self._playback_bytes.extend(pcm16)
            self._empty_since = None
            if self._is_buffering and len(self._playback_bytes) >= self.prebuffer_target_bytes:
                self._is_buffering = False
                if self.playback_started_at is None:
                    self.playback_started_at = time.time()

    def flush_turn_playback(self):
        """Called when Gemini signals turn_complete to ensure remaining buffered audio plays even if under threshold."""
        with self._playback_lock:
            if len(self._playback_bytes) > 0:
                self._is_buffering = False
                if self.playback_started_at is None:
                    self.playback_started_at = time.time()

    def has_pending_playback(self) -> bool:
        """Returns True if AI audio is currently buffered or actively rendering out through the speaker."""
        with self._playback_lock:
            if len(self._playback_bytes) > 0:
                return True
            # Even if buffer just drained, hardware DAC is still outputting the last block (~40-60ms)
            if self.last_audio_sent_time > 0 and (time.time() - self.last_audio_sent_time < 0.08):
                return True
            return False

    def output_callback(self, outdata, frames, time_info, status):
        """High-fidelity PortAudio callback for Bluetooth HFP audio playback.
        Features continuous jitter buffer playout with edge cosine tapering to eliminate all crackle and static."""
        try:
            outdata.fill(0)
            bytes_needed = frames * 2  # 16-bit mono = 2 bytes per frame
            raw_chunk = b""

            with self._playback_lock:
                if self._is_buffering:
                    # Waiting for initial pre-buffer threshold before starting turn
                    pass
                elif len(self._playback_bytes) == 0:
                    now = time.time()
                    if self._empty_since is None:
                        self._empty_since = now
                    elif now - self._empty_since >= 0.50:
                        # Turn playback has completely finished (500ms of true silence)! Reset prebuffering for next turn
                        self._is_buffering = True
                        self.playback_started_at = None
                else:
                    self._empty_since = None
                    if self.playback_started_at is None:
                        self.playback_started_at = time.time()

                    bytes_to_pull = min(len(self._playback_bytes), bytes_needed)
                    raw_chunk = bytes(self._playback_bytes[:bytes_to_pull])
                    del self._playback_bytes[:bytes_to_pull]
                    if len(self._playback_bytes) == 0:
                        self._empty_since = time.time()

            if raw_chunk:
                self.last_audio_sent_time = time.time()
                samples = len(raw_chunk) // 2
                data = np.frombuffer(raw_chunk, dtype=np.int16).copy()

                # If partial block (tail of speech buffer), apply gentle 16-sample cosine fade to prevent clicks
                if samples < frames and samples > 16:
                    fade = np.linspace(1.0, 0.0, 16, dtype=np.float32)
                    data[-16:] = (data[-16:].astype(np.float32) * fade).astype(np.int16)

                outdata[:samples, 0] = data
                self.total_output_frames += samples

            out_bytes = outdata[:, 0].tobytes()
            self.last_played_chunk = out_bytes

            try:
                self.ai_record_queue.put_nowait(out_bytes)
            except queue.Full:
                try:
                    self.ai_record_queue.get_nowait()
                    self.ai_record_queue.put_nowait(out_bytes)
                except Exception:
                    pass

        except Exception as e:
            print("HFP OUTPUT CALLBACK ERROR:", e)

    def _capture_worker(self):
        """Native WASAPI Capture Worker Thread (Microphone from phone over Bluetooth SCO).
        Non-blocking SCO probe: if audio has not been transferred to PC yet (zero packets for >0.4s),
        the client is immediately released and sleeps 0.25s so Phone Link / Android can negotiate
        the Bluetooth SCO channel without any device contention or dropouts."""
        comtypes.CoInitialize()
        try:
            chunk_remainder = bytearray()
            while self._wasapi_running:
                client = None
                try:
                    enum_obj = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)
                    dev = enum_obj.GetDevice(self.input_device)
                    p_client = dev.Activate(ctypes.byref(IID_IAudioClient), 7, None)
                    client = p_client.QueryInterface(IAudioClient)

                    wfx = WAVEFORMATEX()
                    wfx.wFormatTag = 1  # WAVE_FORMAT_PCM
                    wfx.nChannels = 1
                    wfx.nSamplesPerSec = 16000
                    wfx.wBitsPerSample = 16
                    wfx.nBlockAlign = 2
                    wfx.nAvgBytesPerSec = 32000
                    wfx.cbSize = 0

                    # Automatic hardware conversion to device format with standard quality resampler
                    flags = 0x80000000 | 0x08000000  # AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
                    client.Initialize(0, flags, 10000000, 0, ctypes.byref(wfx), None)
                    p_cap = client.GetService(ctypes.byref(IID_IAudioCaptureClient))
                    capture = p_cap.QueryInterface(IAudioCaptureClient)

                    client.Start()

                    probe_start = time.time()
                    last_packet_time = probe_start
                    ever_received_data = False
                    consecutive_errs = 0

                    while self._wasapi_running:
                        got_packet = False
                        while self._wasapi_running:
                            try:
                                pkt = capture.GetNextPacketSize()
                                consecutive_errs = 0
                            except Exception:
                                consecutive_errs += 1
                                if consecutive_errs > 8:
                                    break
                                time.sleep(0.02)
                                continue

                            if pkt == 0:
                                break
                            try:
                                p_data, num_frames, flags_val, dev_pos, qpc_pos = capture.GetBuffer()
                            except Exception:
                                consecutive_errs += 1
                                if consecutive_errs > 8:
                                    break
                                time.sleep(0.02)
                                continue

                            if num_frames == 0 or not p_data:
                                try:
                                    capture.ReleaseBuffer(0)
                                except Exception:
                                    pass
                                break

                            got_packet = True
                            last_packet_time = time.time()
                            ever_received_data = True
                            self.has_sco_audio = True
                            byte_count = num_frames * 2
                            if flags_val & 0x02:  # AUDCLNT_BUFFERFLAGS_SILENT
                                raw_data = b"\x00" * byte_count
                            else:
                                raw_data = ctypes.string_at(p_data, byte_count)
                            try:
                                capture.ReleaseBuffer(num_frames)
                            except Exception:
                                break

                            chunk_remainder.extend(raw_data)
                            while len(chunk_remainder) >= 640:
                                pcm16 = bytes(chunk_remainder[:640])
                                del chunk_remainder[:640]
                                self.total_input_frames += 320
                                try:
                                    self.to_ai.put_nowait(pcm16)
                                except queue.Full:
                                    pass

                        if consecutive_errs > 8:
                            # Too many consecutive COM errors — force re-init
                            break

                        if not got_packet:
                            if not ever_received_data and (time.time() - probe_start > 0.4):
                                # Zero audio frames delivered: call is still on mobile!
                                # Release audio client immediately so user can click 'Transfer to PC' with zero lock/contention!
                                break
                            elif ever_received_data and (time.time() - last_packet_time > 2.0):
                                # Audio stream stopped (e.g. transferred back to phone or call ended)
                                self.has_sco_audio = False
                                break
                            time.sleep(0.01)

                except Exception as ex:
                    # Device state transition (e.g. transfer to PC / SCO connect)
                    if self._wasapi_running:
                        time.sleep(0.05)
                finally:
                    if client:
                        try:
                            client.Stop()
                        except Exception:
                            pass
                    # If waiting for SCO to connect, brief pause before next gentle probe to keep endpoint unlocked
                    if self._wasapi_running and not self.has_sco_audio:
                        time.sleep(0.25)
                    elif self._wasapi_running:
                        time.sleep(0.05)
        finally:
            comtypes.CoUninitialize()

    def _render_worker(self):
        """Native WASAPI Render Worker Thread (Speaker playback to phone over Bluetooth SCO)."""
        comtypes.CoInitialize()
        try:
            while self._wasapi_running:
                # Wait until capture worker confirms incoming audio packets from SCO
                # so the render client never locks the Bluetooth endpoint before Transfer to PC!
                if not self.has_sco_audio:
                    time.sleep(0.1)
                    continue

                client = None
                try:
                    enum_obj = comtypes.client.CreateObject(CLSID_MMDeviceEnumerator, interface=IMMDeviceEnumerator)
                    dev = enum_obj.GetDevice(self.output_device)
                    p_client = dev.Activate(ctypes.byref(IID_IAudioClient), 7, None)
                    client = p_client.QueryInterface(IAudioClient)

                    wfx = WAVEFORMATEX()
                    wfx.wFormatTag = 1  # WAVE_FORMAT_PCM
                    wfx.nChannels = 1
                    wfx.nSamplesPerSec = 16000
                    wfx.wBitsPerSample = 16
                    wfx.nBlockAlign = 2
                    wfx.nAvgBytesPerSec = 32000
                    wfx.cbSize = 0

                    flags = 0x80000000 | 0x08000000  # AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
                    # 500ms buffer (5000000 hns) to provide substantial cushion for Bluetooth SCO hardware packets
                    client.Initialize(0, flags, 5000000, 0, ctypes.byref(wfx), None)
                    buf_size = client.GetBufferSize()

                    p_rend = client.GetService(ctypes.byref(IID_IAudioRenderClient))
                    render = p_rend.QueryInterface(IAudioRenderClient)

                    # Pre-roll the entire buffer with digital silence before Start().
                    # This ensures that upon SCO connection, Bluetooth hardware immediately transmits packets,
                    # satisfying Android's 3.0s watchdog timer and preventing call handover dropouts!
                    try:
                        p_buf = render.GetBuffer(buf_size)
                        ctypes.memset(p_buf, 0, buf_size * 2)
                        render.ReleaseBuffer(buf_size, 0)
                    except Exception:
                        pass

                    client.Start()

                    consecutive_errs = 0
                    while self._wasapi_running:
                        try:
                            padding = client.GetCurrentPadding()
                            consecutive_errs = 0
                        except Exception:
                            consecutive_errs += 1
                            if consecutive_errs > 8:
                                break
                            time.sleep(0.01)
                            continue

                        available = buf_size - padding
                        if available >= 320:  # 20ms frame blocks @ 16kHz
                            frames_to_write = min(available, 1600)
                            bytes_needed = frames_to_write * 2
                            raw_chunk = b""

                            with self._playback_lock:
                                if self._is_buffering:
                                    pass
                                elif len(self._playback_bytes) == 0:
                                    now = time.time()
                                    if self._empty_since is None:
                                        self._empty_since = now
                                    elif now - self._empty_since >= 0.50:
                                        self._is_buffering = True
                                        self.playback_started_at = None
                                else:
                                    self._empty_since = None
                                    if self.playback_started_at is None:
                                        self.playback_started_at = time.time()
                                    bytes_to_pull = min(len(self._playback_bytes), bytes_needed)
                                    raw_chunk = bytes(self._playback_bytes[:bytes_to_pull])
                                    del self._playback_bytes[:bytes_to_pull]
                                    if len(self._playback_bytes) == 0:
                                        self._empty_since = time.time()

                            try:
                                p_buffer = render.GetBuffer(frames_to_write)
                            except Exception:
                                time.sleep(0.01)
                                continue

                            if raw_chunk:
                                self.last_audio_sent_time = time.time()
                                chunk_len = len(raw_chunk)
                                ctypes.memmove(p_buffer, raw_chunk, chunk_len)
                                if chunk_len < bytes_needed:
                                    ctypes.memset(ctypes.cast(p_buffer, ctypes.c_void_p).value + chunk_len, 0, bytes_needed - chunk_len)
                                self.total_output_frames += frames_to_write
                                out_bytes = raw_chunk.ljust(bytes_needed, b"\x00")
                            else:
                                ctypes.memset(p_buffer, 0, bytes_needed)
                                out_bytes = b"\x00" * bytes_needed

                            try:
                                render.ReleaseBuffer(frames_to_write, 0)
                            except Exception:
                                pass

                            self.last_played_chunk = out_bytes

                            try:
                                self.ai_record_queue.put_nowait(out_bytes)
                            except queue.Full:
                                try:
                                    self.ai_record_queue.get_nowait()
                                    self.ai_record_queue.put_nowait(out_bytes)
                                except Exception:
                                    pass
                        else:
                            time.sleep(0.005)

                except Exception as ex:
                    # Device state transition (e.g. transfer to PC / SCO connect)
                    if self._wasapi_running:
                        time.sleep(0.05)
                finally:
                    if client:
                        try:
                            client.Stop()
                        except Exception:
                            pass
                    # Immediate re-init without stalling Bluetooth SCO watchdog
                    if self._wasapi_running:
                        time.sleep(0.05)
        finally:
            comtypes.CoUninitialize()

    def clear_playback(self):
        """Immediately drain pending audio from playback buffer for instantaneous barge-in."""
        self.last_interrupted_at = time.time()
        self.playback_started_at = None
        self.last_audio_sent_time = 0.0
        self._empty_since = None
        self.last_played_chunk = b"\x00" * 640
        with self._playback_lock:
            drained_bytes = len(self._playback_bytes)
            self._playback_bytes.clear()
            self._is_buffering = True
        if hasattr(self, "resampler"):
            self.resampler.reset()
        # Drain recorder queue so interrupted audio cuts off immediately
        while not self.ai_record_queue.empty():
            try:
                self.ai_record_queue.get_nowait()
            except Exception:
                break
        if drained_bytes > 0:
            print(f"[BARGE-IN] Drained {drained_bytes} pending audio bytes from playback buffer.")

    def is_healthy(self) -> bool:
        """Check whether both input and output streams / worker threads exist and are active."""
        if self.is_wasapi:
            return (
                self._wasapi_running
                and (self._cap_thread is not None and self._cap_thread.is_alive())
                and (self._ren_thread is not None and self._ren_thread.is_alive())
            )
        try:
            in_ok = self.input_stream is not None and self.input_stream.active
            out_ok = self.output_stream is not None and self.output_stream.active
            return in_ok and out_ok
        except Exception:
            return False

    def start(self, max_retries: int = 35, retry_delay: float = 0.35, is_outbound: bool = False) -> bool:
        if is_outbound:
            # Outbound cellular calls take time to ring on the mobile network until recipient answers!
            max_retries = 90
            retry_delay = 0.5

        inactive_streak = 0
        for attempt in range(1, max_retries + 1):
            if is_outbound:
                # 1. Check if user cancelled from dashboard
                try:
                    import outbound_dialer as dialer
                    if dialer.is_dial_cancelled():
                        print("[Audio Link] Outbound call cancelled by user via dashboard.")
                        dialer.cancel_outbound_dial(force_hangup=True)
                        return False
                except Exception:
                    pass

            # 2. Check call active status via WinRT telephony
            try:
                from winrt.windows.applicationmodel.calls import PhoneCallManager
                is_active = PhoneCallManager.get_is_call_active()
            except Exception:
                is_active = False

            if is_outbound:
                if not is_active:
                    elapsed = int(attempt * retry_delay)
                    if attempt == 1 or attempt % 4 == 0:
                        print(f"[Audio Link] ⏳ Outbound call initializing ({elapsed}s elapsed)...")
                    time.sleep(retry_delay)
                    continue
                else:
                    elapsed = int(attempt * retry_delay)
                    print(f"\n[Audio Link] 🔔 Cellular call placed ({elapsed}s elapsed)! Audio bridge engaged...")
                    time.sleep(0.35)  # Allow cellular hardware audio channel 350ms to stabilize
            else:
                # For incoming calls only: if caller hung up before audio bridge opened
                if not is_active:
                    inactive_streak += 1
                    if inactive_streak >= 6:
                        print("[Audio Link] Incoming call ceased before audio bridge opened.")
                        return False
                else:
                    inactive_streak = 0

            # 3. Dynamic re-resolution: refresh and ensure endpoints match active connected phone
            in_dev, out_dev = find_hfp_devices(quiet=True, force_refresh=(attempt > 1 or is_outbound))
            if in_dev is not None and out_dev is not None:
                self.input_device = in_dev
                self.output_device = out_dev
                self.is_wasapi = isinstance(in_dev, str) and isinstance(out_dev, str) and in_dev.startswith("{")
                self.phone_name = _last_bth_phone_name

            if self.input_device is None or self.output_device is None:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise RuntimeError(
                    "Bluetooth Hands-Free (HFP) endpoint not found. "
                    "Please ensure phone is paired via Microsoft Phone Link."
                )

            try:
                # 4. Native Windows WASAPI Engine for Bluetooth HFP
                if self.is_wasapi:
                    self._wasapi_running = True
                    self._cap_thread = threading.Thread(target=self._capture_worker, daemon=True, name="WASAPICapture")
                    self._ren_thread = threading.Thread(target=self._render_worker, daemon=True, name="WASAPIRender")
                    self._cap_thread.start()
                    self._ren_thread.start()
                    time.sleep(0.15)
                    if self.is_healthy():
                        print(f"[Audio Link] WASAPI Bluetooth HFP audio bridge running successfully ({self.phone_name}).\n")
                        return True
                    else:
                        raise RuntimeError("WASAPI worker threads did not start properly.")

                # 5. PortAudio Fallback Engine
                if not self.output_stream:
                    self.output_stream = sd.OutputStream(
                        device=self.output_device,
                        samplerate=HFP_RATE,
                        channels=1,
                        dtype="int16",
                        blocksize=HFP_BLOCK,
                        latency=0.08,
                        callback=self.output_callback,
                    )
                    self.output_stream.start()

                if not self.input_stream:
                    self.input_stream = sd.InputStream(
                        device=self.input_device,
                        samplerate=HFP_RATE,
                        channels=1,
                        dtype="int16",
                        blocksize=HFP_BLOCK,
                        latency=0.08,
                        callback=self.input_callback,
                    )
                    self.input_stream.start()

                print(f"[Audio Link] PortAudio Bluetooth HFP audio bridge running successfully ({self.phone_name}).\n")
                return True
            except Exception as e:
                self.stop()
                if attempt < max_retries:
                    err_str = str(e)
                    if "WdmSyncIoctl" in err_str or "PaErrorCode -9999" in err_str:
                        print(f"[Audio Link] ⏳ Synchronizing Bluetooth SCO voice link with phone ({attempt}/{max_retries})...")
                    else:
                        print(f"[Audio Link] Bluetooth SCO hardware link negotiating ({type(e).__name__}: {e}). Retrying ({attempt}/{max_retries}) in {retry_delay}s...")
                    time.sleep(retry_delay)
                else:
                    if is_outbound:
                        print(f"[Audio Link] Outbound call not answered by recipient after {int(max_retries * retry_delay)}s.")
                        return False
                    else:
                        print(f"[Audio Error] Failed to start Bluetooth HFP audio streams after {max_retries} attempts ({type(e).__name__}: {e}).")
                        return False

        if is_outbound:
            print(f"[Audio Link] Outbound call not answered by recipient after {int(max_retries * retry_delay)}s.")
        return False

    def stop(self):
        if self.is_wasapi:
            self._wasapi_running = False
            self.has_sco_audio = False
            if self._cap_thread:
                self._cap_thread.join(timeout=1.0)
                self._cap_thread = None
            if self._ren_thread:
                self._ren_thread.join(timeout=1.0)
                self._ren_thread = None

        if self.input_stream:
            try:
                self.input_stream.stop()
                self.input_stream.close()
            except Exception:
                pass
            self.input_stream = None

        if self.output_stream:
            try:
                self.output_stream.stop()
                self.output_stream.close()
            except Exception:
                pass
            self.output_stream = None


class GeminiPrewarmer:
    """Pre-connects Gemini Live API session while the phone is ringing.
    Ensures zero connect latency when the caller or phone answers."""

    def __init__(self, api_key: str, model: str, config: types.LiveConnectConfig):
        self.api_key = api_key
        self.model = model
        self.config = config
        self.client = genai.Client(api_key=api_key)
        self.session = None
        self._connect_task = None
        self._ready_event = asyncio.Event()
        self._release_event = asyncio.Event()
        self._is_connecting = False

    def is_active(self) -> bool:
        return self._is_connecting

    def is_ready(self) -> bool:
        return self._ready_event.is_set() and self.session is not None

    def start_prewarm(self):
        if self._is_connecting:
            return
        self._is_connecting = True
        self._ready_event.clear()
        self._release_event.clear()
        self._connect_task = asyncio.create_task(self._worker())

    async def _worker(self):
        try:
            print("[Pre-warm] Incoming call detected — pre-connecting Gemini Live API in background...")
            async with self.client.aio.live.connect(model=self.model, config=self.config) as session:
                self.session = session
                self._ready_event.set()
                print("[Pre-warm] Gemini Live session pre-connected & ready for call pickup!")
                await self._release_event.wait()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[Pre-warm Error]: {e}")
        finally:
            self.session = None
            self._ready_event.clear()
            self._is_connecting = False

    async def acquire_session(self, timeout: float = 2.5):
        """Wait if currently connecting, or return existing ready session."""
        if not self._is_connecting:
            return None
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return self.session
        except asyncio.TimeoutError:
            print("[Pre-warm] Pre-warmed session timed out. Connecting fresh session.")
            return None

    async def release(self):
        self._release_event.set()
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
        self._is_connecting = False


_global_resampler = None

def resample_for_hfp(pcm_24k: bytes, bridge: HFPBridge = None) -> bytes:
    """Gemini's output audio (24kHz) -> HFP playback rate (16kHz)."""
    global _global_resampler
    res = bridge.resampler if (bridge and hasattr(bridge, "resampler")) else None
    if not res:
        if _global_resampler is None:
            _global_resampler = HFPResampler()
        res = _global_resampler
    return res.resample(pcm_24k)


async def run_gemini_bridge(
    bridge: HFPBridge,
    stop_event: asyncio.Event,
    recorder: CallRecorder = None,
    web_server: WebGatewayServer = None,
    prewarmer: GeminiPrewarmer = None,
    outbound_info: dict = None
):
    """Owns the Gemini Live session for the duration of one call.
    Streams caller audio to Gemini, plays synthesized replies into phone call,
    records synchronized dual-track stereo WAV audio, and streams telemetry to Web UI."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured! Please configure it on the Web Platform.")

    settings = load_settings()
    voice_name = settings.get("voice_name", "Puck")
    lang = settings.get("language", "auto")
    base_inst = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
    if outbound_info:
        base_inst = base_inst + dialer.build_outbound_context(outbound_info, language=lang)
    system_instruction = km.get_effective_system_instruction(base_inst, language=lang)
    vad_threshold = settings.get("vad_threshold", VAD_RMS_THRESHOLD)
    if vad_threshold > 1500:
        vad_threshold = 950

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        realtime_input_config=types.RealtimeInputConfig(
            automatic_activity_detection=types.AutomaticActivityDetection(
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                silence_duration_ms=450,
                prefix_padding_ms=100,
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    is_initial_connect = True

    while not stop_event.is_set():
        session_stop_event = asyncio.Event()

        # Check if we have a pre-warmed session ready from incoming ring
        active_session = None
        if is_initial_connect and prewarmer and prewarmer.is_active():
            print("[Gateway] Acquiring pre-warmed Gemini Live session...")
            active_session = await prewarmer.acquire_session(timeout=2.0)
            if active_session:
                print("[Gateway] Pre-warmed session successfully acquired (zero connect latency)!")

        async def _run_session_pipeline(session):
            nonlocal is_initial_connect
            print("Gemini Live session connected & active.")

            # Flush stale audio buffered in to_ai during connection
            flushed = 0
            while not bridge.to_ai.empty():
                try:
                    bridge.to_ai.get_nowait()
                    flushed += 1
                except queue.Empty:
                    break
            if flushed > 0:
                print(f"[Audio] Flushed {flushed} stale frames from pre-connection buffer.")

            is_initial_greeting_active = False
            greeting_sent_time = 0.0

            # Proactively prompt Gemini to speak first on pickup
            if is_initial_connect:
                if outbound_info:
                    print(f"[Gateway] ⏳ Outbound call to {outbound_info.get('contact_name')} is ringing... Awaiting recipient pickup...")
                    pickup_detected = False
                    pickup_start = time.time()
                    consecutive_voice_chunks = 0

                    while not stop_event.is_set() and not session_stop_event.is_set():
                        # 1. Check if Phone Link reports call connected / duration timer active
                        try:
                            if dialer.is_phone_link_call_connected():
                                print("[Gateway] 🔔 Phone Link reports call connected / duration active!")
                                pickup_detected = True
                                break
                        except Exception:
                            pass

                        # 2. Check audio line for recipient voice (RMS > 140)
                        # Wait at least 0.5s to bypass carrier dialing tones and connection clicks
                        elapsed = time.time() - pickup_start
                        while not bridge.to_ai.empty():
                            try:
                                chunk = bridge.to_ai.get_nowait()
                                chunk_rms = audioop.rms(chunk, 2)
                                if elapsed >= 0.5 and chunk_rms > 140:
                                    consecutive_voice_chunks += 1
                                    if consecutive_voice_chunks >= 2:
                                        print(f"[Gateway] 🔔 Recipient voice detected on line (RMS={chunk_rms})!")
                                        pickup_detected = True
                                        break
                                else:
                                    consecutive_voice_chunks = 0
                            except Exception:
                                break

                        if pickup_detected:
                            break

                        # 3. Proactive pickup fallback: after 2.8s of active call, engage proactive AI greeting!
                        if elapsed >= 2.8:
                            print(f"[Gateway] 🔔 Outbound call active ({elapsed:.1f}s elapsed) — engaging proactive AI greeting!")
                            pickup_detected = True
                            break

                        await asyncio.sleep(0.1)

                    if not pickup_detected or stop_event.is_set() or session_stop_event.is_set():
                        is_initial_connect = False
                        return

                # Flush any leftover dialing noise/audio before dispatching greeting
                while not bridge.to_ai.empty():
                    try:
                        bridge.to_ai.get_nowait()
                    except Exception:
                        break

                greeting_sent_time = time.time()
                try:
                    if outbound_info:
                        greeting_turn = dialer.build_outbound_proactive_prompt(outbound_info, language=lang)
                        print(f"[Gateway] 🚀 Outbound call connected to {outbound_info.get('contact_name')}! Proactive opening prompt dispatched to Gemini Live.")
                    else:
                        print("[Gateway] Requesting immediate inbound AI greeting...")
                        if lang in ("hi", "auto"):
                            greeting_turn = "[कॉल कनेक्ट हो गया है। तुरंत बोलें: 'नमस्ते! कॉल करने के लिए धन्यवाद, बताइए मैं आपकी क्या सहायता कर सकता हूँ?']"
                        elif lang == "mai":
                            greeting_turn = "[कॉल कनेक्ट भऽ गेल। तुरंत बोलू: 'प्रणाम! कॉल करय लेल धन्यवाद, कहू हम अहाँक की मद्द कऽ सकैत छी?']"
                        else:
                            greeting_turn = "[Call connected. Immediately say: 'Hello! Thanks for calling, how can I help you today?']"
                    is_initial_greeting_active = True
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[
                                types.Part(text=greeting_turn)
                            ],
                        ),
                        turn_complete=True,
                    )
                except Exception as e:
                    is_initial_greeting_active = False
                    print(f"[Gateway Warning] Could not send initial greeting trigger: {e}")
                is_initial_connect = False
            else:
                print("[Gateway] Seamlessly reconnected mid-call! Continuing conversation.")

            current_caller_text = ""
            current_ai_text = ""

            async def sender():
                """Drain caller audio, write stereo recording, stream to Gemini, monitor barge-in."""
                nonlocal current_ai_text, is_initial_greeting_active, greeting_sent_time
                consecutive_speech_chunks = 0
                sent_chunks = 0
                zero_chunks = 0
                last_report = time.time()
                latest_rms = 0
                send_buffer = bytearray()
                last_send_time = time.time()
                CHUNK_DISPATCH_BYTES = 3200  # 100ms of 16kHz 16-bit mono PCM (1600 samples)

                while not stop_event.is_set() and not session_stop_event.is_set():
                    now = time.time()
                    if now - last_report >= 2.0:
                        last_report = now
                        if sent_chunks > 0:
                            ai_status = "AI Speaking (echo muted)" if bridge.has_pending_playback() else "Line Open"
                            print(f"[Audio Link] 🎙️ Caller Audio: RMS={latest_rms} | Streamed: {sent_chunks} chunks to Gemini Live ({ai_status})")
                        elif bridge.total_input_frames == 0:
                            print("[Audio Link] ⏳ Call connected on PC — waiting for caller voice...")

                    try:
                        pcm16 = bridge.to_ai.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.005)
                        continue
                    except Exception:
                        continue

                    latest_rms = audioop.rms(pcm16, 2)

                    # Pull sequential synchronized AI audio chunk from playback queue
                    # Keep queue buffer synchronized (<100ms cushion) without clock drift
                    while bridge.ai_record_queue.qsize() > 5:
                        try:
                            discard = bridge.ai_record_queue.get_nowait()
                            # If non-silent speech, prioritize it so it isn't discarded
                            if any(discard):
                                current_ai_chunk = discard
                                break
                        except Exception:
                            break
                    else:
                        try:
                            current_ai_chunk = bridge.ai_record_queue.get_nowait()
                        except queue.Empty:
                            current_ai_chunk = b"\x00" * len(pcm16)

                    # Ensure exact sample matching
                    if len(current_ai_chunk) != len(pcm16):
                        if len(current_ai_chunk) > len(pcm16):
                            current_ai_chunk = current_ai_chunk[:len(pcm16)]
                        else:
                            current_ai_chunk = current_ai_chunk + b"\x00" * (len(pcm16) - len(current_ai_chunk))

                    bridge.last_played_chunk = current_ai_chunk
                    ai_rms = audioop.rms(current_ai_chunk, 2) if current_ai_chunk else 0

                    # Update web platform telemetry meters
                    if web_server:
                        web_server.update_audio_levels(latest_rms, ai_rms)

                    # Write dual-track stereo block (Left: Caller, Right: AI)
                    if recorder:
                        recorder.write_stereo_block(pcm16, current_ai_chunk)

                    if not any(pcm16):
                        zero_chunks += 1

                    # Client-side barge-in detection:
                    if bridge.has_pending_playback():
                        ai_speaking_sec = (
                            time.time() - bridge.playback_started_at
                            if bridge.playback_started_at is not None
                            else 0.0
                        )
                        if ai_speaking_sec >= VAD_INITIAL_GRACE_PERIOD:
                            if latest_rms > vad_threshold:
                                consecutive_speech_chunks += 1
                                if consecutive_speech_chunks >= VAD_TRIGGER_CONSECUTIVE:
                                    print(f"[BARGE-IN] Caller voice detected locally (RMS={latest_rms}) - interrupting AI playback.")
                                    bridge.clear_playback()
                                    is_initial_greeting_active = False
                                    if current_ai_text:
                                        ai_interrupted = current_ai_text.strip()
                                        if ai_interrupted:
                                            if recorder:
                                                recorder.add_transcript("model", ai_interrupted + " [interrupted]")
                                            if web_server:
                                                asyncio.create_task(web_server.broadcast("transcript_turn_complete", {"role": "model", "text": ai_interrupted + " [interrupted]"}))
                                        current_ai_text = ""
                                    if recorder:
                                        recorder.add_event("barge_in", {"rms": latest_rms})
                                    if web_server:
                                        asyncio.create_task(web_server.broadcast("barge_in", {"rms": latest_rms}))
                                    consecutive_speech_chunks = 0
                            else:
                                consecutive_speech_chunks = 0
                        else:
                            consecutive_speech_chunks = 0
                    else:
                        consecutive_speech_chunks = 0

                    if is_initial_greeting_active:
                        # Safety timeout: if greeting turn takes more than 10s, resume audio streaming
                        if time.time() - greeting_sent_time > 10.0:
                            is_initial_greeting_active = False
                        else:
                            # Do not stream realtime input while Gemini is generating/speaking the initial greeting turn
                            # Prevents barge-in feedback loops or Gemini getting interrupted by silence frames
                            await asyncio.sleep(0.01)
                            continue

                    # Audio stream sanitization for Gemini Live:
                    # 1. Echo suppression while AI is speaking:
                    # If AI is currently speaking and caller is not interrupting, send pure digital silence
                    # to prevent the AI's synthesized voice from bleeding back into Gemini Live and halting itself.
                    audio_to_send = pcm16
                    if bridge.has_pending_playback():
                        audio_to_send = b"\x00" * len(pcm16)
                    elif latest_rms < 50:
                        # 2. Software Noise Gate:
                        # Ambient background hiss (< 50 RMS) is clamped to pure digital silence.
                        # Legitimate caller speech (even quiet speech > 50 RMS) streams cleanly to Gemini Live.
                        audio_to_send = b"\x00" * len(pcm16)

                    send_buffer.extend(audio_to_send)
                    now_time = time.time()
                    if len(send_buffer) >= CHUNK_DISPATCH_BYTES or (now_time - last_send_time >= 0.10 and len(send_buffer) > 0):
                        to_dispatch = bytes(send_buffer)
                        send_buffer.clear()
                        last_send_time = now_time
                        try:
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=to_dispatch,
                                    mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                                )
                            )
                            sent_chunks += 1
                        except Exception as e:
                            if not stop_event.is_set() and not session_stop_event.is_set():
                                print(f"[Sender] Error sending audio: {type(e).__name__}: {e}")
                            session_stop_event.set()
                            break

                if len(send_buffer) > 0 and not session_stop_event.is_set():
                    try:
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=bytes(send_buffer),
                                mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                            )
                        )
                    except Exception:
                        pass
                    send_buffer.clear()

            async def receiver():
                """Pull Gemini's spoken response, resample, push to jitter buffer, and stream transcripts."""
                nonlocal current_caller_text, current_ai_text, is_initial_greeting_active
                while not stop_event.is_set() and not session_stop_event.is_set():
                    try:
                        async for response in session.receive():
                            if stop_event.is_set() or session_stop_event.is_set():
                                break

                            server_content = response.server_content
                            if server_content is None:
                                continue

                            # Server-side interruption (Gemini Live detected user speech)
                            if server_content.interrupted:
                                is_initial_greeting_active = False
                                print("[BARGE-IN] Server signaled interruption! Clearing playback buffer.")
                                bridge.clear_playback()
                                bridge._out_resample_state = None
                                if current_ai_text:
                                    ai_interrupted = current_ai_text.strip()
                                    if ai_interrupted:
                                        print(f"[AI Transcript (Interrupted)]: {ai_interrupted}")
                                        if recorder:
                                            recorder.add_transcript("model", ai_interrupted + " [interrupted]")
                                        if web_server:
                                            await web_server.broadcast("transcript_turn_complete", {"role": "model", "text": ai_interrupted + " [interrupted]"})
                                    current_ai_text = ""
                                if recorder:
                                    recorder.add_event("server_barge_in")
                                if web_server:
                                    await web_server.broadcast("barge_in", {"type": "server"})
                                continue

                            # 1. Caller real-time speech transcription
                            if server_content.input_transcription and server_content.input_transcription.text:
                                in_text = server_content.input_transcription.text
                                current_caller_text += in_text
                                print(f"[Caller Live]: {in_text.strip()}")
                                if web_server:
                                    await web_server.broadcast("transcript_chunk", {
                                        "role": "caller",
                                        "text": in_text
                                    })

                            # 2. Gemini AI real-time speech synthesis transcription
                            if server_content.output_transcription and server_content.output_transcription.text:
                                out_text = server_content.output_transcription.text
                                # If caller was previously speaking and now AI is replying, finalize caller turn
                                if current_caller_text:
                                    final_caller = current_caller_text.strip()
                                    if final_caller:
                                        print(f"[Caller Transcript]: {final_caller}")
                                        if recorder:
                                            recorder.add_transcript("caller", final_caller)
                                        if web_server:
                                            await web_server.broadcast("transcript_turn_complete", {
                                                "role": "caller",
                                                "text": final_caller
                                            })
                                    current_caller_text = ""

                                current_ai_text += out_text
                                if web_server:
                                    await web_server.broadcast("transcript_chunk", {
                                        "role": "model",
                                        "text": out_text
                                    })

                            # Extract synthesized audio
                            if server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        if time.time() - bridge.last_interrupted_at >= 0.2:
                                            pcm16_hfp = bridge.resampler.resample(part.inline_data.data)
                                            bridge.push_playback_audio(pcm16_hfp)

                            # If turn completes, flush jitter buffer & finalize completed turns
                            if server_content.turn_complete:
                                is_initial_greeting_active = False
                                bridge.flush_turn_playback()
                                if current_caller_text:
                                    final_caller = current_caller_text.strip()
                                    if final_caller:
                                        print(f"[Caller Transcript]: {final_caller}")
                                        if recorder:
                                            recorder.add_transcript("caller", final_caller)
                                        if web_server:
                                            await web_server.broadcast("transcript_turn_complete", {
                                                "role": "caller",
                                                "text": final_caller
                                            })
                                    current_caller_text = ""
                                if current_ai_text:
                                    final_ai = current_ai_text.strip()
                                    if final_ai:
                                        print(f"[AI Transcript]: {final_ai}")
                                        if recorder:
                                            recorder.add_transcript("model", final_ai)
                                        if web_server:
                                            await web_server.broadcast("transcript_turn_complete", {
                                                "role": "model",
                                                "text": final_ai
                                            })
                                    current_ai_text = ""

                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        if not stop_event.is_set() and not session_stop_event.is_set():
                            code = getattr(e, 'code', None)
                            reason = getattr(e, 'reason', '')
                            print(f"[Receiver] Session closed: {type(e).__name__}: {e} (code={code}, reason='{reason}')")
                        session_stop_event.set()
                        break

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(stop_event.wait()),
                    asyncio.create_task(session_stop_event.wait()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()

            send_task.cancel()
            recv_task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)

            # Flush any uncommitted spoken turns upon session completion
            if current_caller_text:
                final_caller = current_caller_text.strip()
                if final_caller:
                    if recorder:
                        recorder.add_transcript("caller", final_caller)
                    if web_server:
                        await web_server.broadcast("transcript_turn_complete", {"role": "caller", "text": final_caller})
                current_caller_text = ""
            if current_ai_text:
                final_ai = current_ai_text.strip()
                if final_ai:
                    if recorder:
                        recorder.add_transcript("model", final_ai)
                    if web_server:
                        await web_server.broadcast("transcript_turn_complete", {"role": "model", "text": final_ai})
                current_ai_text = ""

            # Flush remaining audio from queues into recorder
            if recorder:
                while not bridge.to_ai.empty():
                    try:
                        rem_caller = bridge.to_ai.get_nowait()
                        try:
                            rem_ai = bridge.ai_record_queue.get_nowait()
                        except Exception:
                            rem_ai = b"\x00" * len(rem_caller)
                        recorder.write_stereo_block(rem_caller, rem_ai)
                    except Exception:
                        break

        try:
            # Build appropriate session configuration: initial prompt vs conversational reconnect
            if is_initial_connect:
                current_config = config
            else:
                print("\n[Gateway] Mid-call reconnect: Connecting fresh Gemini Live session with conversational memory...")
                recent_lines = []
                if recorder and recorder.transcripts:
                    for t in recorder.transcripts[-8:]:
                        role_str = "Caller" if t.get("role") in ("caller", "user") else "Representative (You)"
                        txt = (t.get("text") or "").strip()
                        if txt:
                            recent_lines.append(f"{role_str}: {txt}")
                history_text = "\n".join(recent_lines) if recent_lines else "The conversation has been active for several minutes."

                contact_name = (outbound_info.get("contact_name") if outbound_info else None) or "the customer"

                reconnect_inst = (
                    f"{system_instruction}\n\n"
                    f"[CRITICAL: MID-CALL RECONNECTION DIRECTIVE - DO NOT RE-GREET]\n"
                    f"This phone call with {contact_name} is ALREADY IN PROGRESS and active.\n"
                    f"You have already greeted the customer and have been conversing for several minutes.\n"
                    f"STRICT RULES FOR THIS RECONNECTION:\n"
                    f"1. NEVER re-greet the caller. DO NOT say 'Hello', 'Namaste', 'Kaise hain', or re-introduce yourself or your company name.\n"
                    f"2. DO NOT ask 'is this a good time to speak' or restart the pitch.\n"
                    f"3. Recent dialogue before reconnection:\n"
                    f"--- CONVERSATION HISTORY ---\n"
                    f"{history_text}\n"
                    f"--- END HISTORY ---\n"
                    f"4. Seamlessly continue the conversation right where it left off. Respond directly, concisely (1-2 sentences), and naturally to whatever the caller says next."
                )
                current_config = types.LiveConnectConfig(
                    response_modalities=["AUDIO"],
                    system_instruction=reconnect_inst,
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
                        )
                    ),
                    realtime_input_config=types.RealtimeInputConfig(
                        automatic_activity_detection=types.AutomaticActivityDetection(
                            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                            silence_duration_ms=450,
                            prefix_padding_ms=100,
                        )
                    ),
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    output_audio_transcription=types.AudioTranscriptionConfig(),
                )

            if active_session:
                try:
                    await _run_session_pipeline(active_session)
                except Exception as e:
                    print(f"[Gateway] Pre-warmed session ended/failed ({e}). Connecting fresh Gemini Live session...")
                    active_session = None
                    if not stop_event.is_set():
                        async with client.aio.live.connect(model=GEMINI_MODEL, config=current_config) as fresh_session:
                            await _run_session_pipeline(fresh_session)
            else:
                if is_initial_connect:
                    print("Connecting to Gemini Live API...")

                async with client.aio.live.connect(model=GEMINI_MODEL, config=current_config) as fresh_session:
                    await _run_session_pipeline(fresh_session)

        except Exception as e:
            if not stop_event.is_set():
                print(f"[Gemini Live] Session error: {type(e).__name__}: {e}")

        # Check if cellular call is still active or Bluetooth bridge is still running
        try:
            pcm_active = PhoneCallManager.get_is_call_active()
        except Exception:
            pcm_active = False

        pl_active = False
        try:
            pl_active = dialer.is_phone_link_call_present() or dialer.is_phone_link_call_connected()
        except Exception:
            pl_active = False

        bridge_active = bridge.is_healthy()

        call_still_active = not stop_event.is_set() and (pcm_active or pl_active or bridge_active)

        if not stop_event.is_set() and call_still_active:
            print("\n[Gateway] Cellular call is still ongoing! Reconnecting to a fresh Gemini Live session in 0.5s...")
            active_session = None
            await asyncio.sleep(0.5)
        else:
            break

    print("Gemini Live session closed.")


async def watch_call_and_hfp(bridge: HFPBridge, stop_event: asyncio.Event, is_outbound: bool = False):
    """Monitors cellular call state and checks Bluetooth HFP health."""
    loop = asyncio.get_running_loop()
    inactive_count = 0
    start_time = time.time()
    call_connected = not is_outbound  # Inbound calls are already active; outbound calls start in dialing/ringing

    while not stop_event.is_set():
        # Check if outbound call was explicitly cancelled/hung up by user via dashboard
        if is_outbound and dialer.is_dial_cancelled():
            print("\n[Outbound Dialer] Outbound call was cancelled.")
            stop_event.set()
            break

        # Check if Phone Link explicitly reports call ended (only after call was actively connected)
        try:
            if call_connected and dialer.is_phone_link_call_ended():
                inactive_count += 1
                if inactive_count >= 3:
                    print("\n[Phone Link] Explicit call ended event detected.")
                    stop_event.set()
                    break
        except Exception:
            pass

        try:
            pcm_active = PhoneCallManager.get_is_call_active()
        except Exception:
            pcm_active = False

        pl_active = False
        try:
            pl_active = dialer.is_phone_link_call_present() or dialer.is_phone_link_call_connected()
        except Exception:
            pl_active = False

        call_active = pcm_active or pl_active

        if call_active:
            call_connected = True
            inactive_count = 0
        else:
            elapsed = time.time() - start_time
            if is_outbound and not call_connected:
                # During outbound dialing and ringing, PhoneCallManager is False while remote phone rings.
                # Allow up to 45 seconds for recipient to pick up before timing out.
                if elapsed < 45.0:
                    inactive_count = 0
                else:
                    inactive_count += 1
            else:
                # Call was connected or inbound:
                # If Bluetooth HFP is healthy and delivering audio frames, call is actively ongoing!
                if bridge.is_healthy() and bridge.total_input_frames > 0:
                    inactive_count = 0
                else:
                    inactive_count += 1

            # At 1.0s interval, threshold=25 means 25 continuous seconds of silence & no call presence
            threshold = 25
            if inactive_count >= threshold:
                print("\nCellular call became inactive (call ended).")
                stop_event.set()
                break

        # Stream health check with a 5.0s grace period
        if time.time() - start_time > 5.0:
            if not bridge.is_healthy():
                print("\n[WARNING] Bluetooth HFP link dropped mid-call! Attempting to reconnect...")
                bridge.stop()

                reconnected = False
                for _ in range(5):
                    if stop_event.is_set():
                        break
                    try:
                        in_dev, out_dev = await loop.run_in_executor(None, lambda: find_hfp_devices(quiet=True, force_refresh=False))
                        if in_dev is not None and out_dev is not None:
                            bridge.input_device = in_dev
                            bridge.output_device = out_dev
                            bridge.start()
                            print("[INFO] Bluetooth HFP reconnected successfully!\n")
                            reconnected = True
                            break
                    except Exception:
                        bridge.stop()
                        await asyncio.sleep(1.0)

                if not reconnected:
                    print("\n[INFO] Bluetooth link disconnected or call ended. Terminating session.")
                    stop_event.set()
                    break

        await asyncio.sleep(1.0)


async def main_async():
    """Unified Asynchronous Application Entry Point."""
    print("============================================================")
    print("    CALLINK: GSM-TO-GEMINI LIVE VOICE GATEWAY & STUDIO")
    print("============================================================")
    print()

    ensure_gemini_api_key(allow_web_prompt=True)
    prevent_sleep()
    start_stay_awake_heartbeat()

    # 1. Start the Web Platform Server
    web_server = WebGatewayServer(port=5000)
    await web_server.start()

    # Pre-discover and cache Bluetooth HFP endpoints for zero startup latency
    cached_in_dev, cached_out_dev = find_hfp_devices(quiet=True)
    if cached_in_dev is not None and cached_out_dev is not None:
        try:
            from web_server import clean_device_name
            if isinstance(cached_in_dev, int):
                devs = sd.query_devices()
                clean_in = clean_device_name(devs[cached_in_dev]["name"])
                clean_out = clean_device_name(devs[cached_out_dev]["name"])
                alias = ""
                if "(" in clean_in and ")" in clean_in:
                    m = re.search(r'\(([^)]+)\)', clean_in)
                    if m:
                        alias = m.group(1).strip()
            else:
                alias = _last_bth_phone_name
                clean_in = f"Hands-Free HF Audio ({alias})"
                clean_out = f"Hands-Free HF Audio ({alias})"

            web_server.state["hfp_input_name"] = clean_in
            web_server.state["hfp_output_name"] = clean_out
            web_server.state["hfp_connected"] = True
            web_server.state["hfp_alias"] = alias
        except Exception:
            pass

    # 2. Auto-launch dashboard in default browser
    settings = load_settings()
    if settings.get("auto_launch_browser", True):
        def open_browser():
            time.sleep(0.5)
            try:
                webbrowser.open(f"http://127.0.0.1:{web_server.port}")
            except Exception:
                pass
        threading.Thread(target=open_browser, daemon=True, name="BrowserLauncher").start()

    print("============================================================")
    print(f" Web Platform URL : http://localhost:{web_server.port}")
    print(" Telephony Mode   : Automatic GSM Detection (WinRT)")
    print(" Call Audio Mode  : Synchronized Stereo WAV (L: Caller | R: AI)")
    print(" Jitter Buffer    : Active (120ms DynamicSCO Prebuffering)")
    print(" Call Pre-Warming : Engaged on Incoming Ring (Zero Pickup Latency)")
    print("============================================================\n")

    prewarmer = None

    # 3. Main continuous standby loop for GSM cellular calls
    _call_cancel_cooldown_until = 0.0
    while True:
        print(f"[{time.strftime('%H:%M:%S')}] Standby: Monitoring for incoming cellular calls...")

        was_incoming = False
        last_voip_notice = 0.0
        while True:
            try:
                call_active = PhoneCallManager.get_is_call_active()
                call_incoming = PhoneCallManager.get_is_call_incoming()
            except Exception:
                call_active = False
                call_incoming = False

            pl_call_active = False
            try:
                pl_call_active = dialer.is_phone_link_call_present() or dialer.is_phone_link_call_connected()
            except Exception:
                pl_call_active = False

            # Strictly ignore VoIP calls ringing or active on the PC (WhatsApp Desktop, Teams, Skype, Zoom)
            if (call_active or call_incoming or pl_call_active) and is_pc_voip_call_active():
                now = time.time()
                if now - last_voip_notice > 5.0:
                    last_voip_notice = now
                    print(f"[{time.strftime('%H:%M:%S')}] [Telephony] ℹ️ Ignoring PC VoIP call (WhatsApp Desktop / Messenger). Callink strictly handles cellular SIM calls.")
                await asyncio.sleep(0.3)
                continue

            # Break standby loop if an outbound dial is pending OR WinRT/Phone Link reports active call (not in cancel cooldown)
            outbound_pending = dialer.get_active_outbound()
            if outbound_pending:
                break
            if call_active or pl_call_active:
                if time.time() < _call_cancel_cooldown_until:
                    await asyncio.sleep(0.2)
                    continue
                break

            if call_incoming and not was_incoming:
                was_incoming = True
                print("\n[Telephony] 🔔 INCOMING GSM CELLULAR CALL RINGING — PRE-WARMING AI...")
                web_server.state["phone_call_incoming"] = True
                web_server.state["status"] = "ringing"
                await web_server.broadcast("call_ringing", {})

                # Pre-connect Gemini Live API in the background while the phone rings
                api_key = os.environ.get("GEMINI_API_KEY")
                if api_key:
                    curr_settings = load_settings()
                    v_name = curr_settings.get("voice_name", "Puck")
                    lang = curr_settings.get("language", "auto")
                    base_inst = curr_settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
                    sys_inst = km.get_effective_system_instruction(base_inst, language=lang)
                    live_cfg = types.LiveConnectConfig(
                        response_modalities=["AUDIO"],
                        system_instruction=sys_inst,
                        speech_config=types.SpeechConfig(
                            voice_config=types.VoiceConfig(
                                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=v_name)
                            )
                        ),
                        realtime_input_config=types.RealtimeInputConfig(
                            automatic_activity_detection=types.AutomaticActivityDetection(
                                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                                silence_duration_ms=450,
                                prefix_padding_ms=100,
                            )
                        ),
                        input_audio_transcription=types.AudioTranscriptionConfig(),
                        output_audio_transcription=types.AudioTranscriptionConfig(),
                    )
                    prewarmer = GeminiPrewarmer(api_key, GEMINI_MODEL, live_cfg)
                    prewarmer.start_prewarm()

            elif not call_incoming and was_incoming:
                # Ring ceased without being answered (caller hung up / missed call)
                print("[Telephony] Incoming call ceased before pickup (missed/declined). Returning to standby.")
                was_incoming = False
                web_server.state["phone_call_incoming"] = False
                web_server.state["status"] = "standby"
                await web_server.broadcast("call_cancelled", {})
                if prewarmer:
                    await prewarmer.release()
                    prewarmer = None

            await asyncio.sleep(0.1)

        # Check if an outbound call was dialed
        outbound_info = dialer.get_active_outbound()
        is_outbound = bool(outbound_info)
        recorder = None
        started = False

        if is_outbound:
            dialer.ensure_phone_link_visible()
            call_type = "outbound_gsm"
            print("\n" + "="*50)
            print(f" >>> OUTBOUND CELLULAR CALL TO {outbound_info.get('contact_name')} ({outbound_info.get('phone_number')}) <<<")
            print(" >>> AWAITING RECIPIENT PICKUP TO ENGAGE PROACTIVE AI (AI SPEAKS FIRST) <<<")
            print("="*50)
            web_server.state["status"] = "ringing"
            web_server.state["call_type"] = call_type
            web_server.state["outbound_state"] = outbound_info
            await web_server.broadcast("outbound_ringing", {"outbound": outbound_info})

            # Pre-connect Gemini Live API in the background while the outbound call rings (Zero Pickup Latency)
            api_key = os.environ.get("GEMINI_API_KEY")
            if api_key:
                curr_settings = load_settings()
                v_name = curr_settings.get("voice_name", "Puck")
                lang = curr_settings.get("language", "auto")
                base_inst = curr_settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
                if outbound_info:
                    base_inst = base_inst + dialer.build_outbound_context(outbound_info, language=lang)
                sys_inst = km.get_effective_system_instruction(base_inst, language=lang)
                live_cfg = types.LiveConnectConfig(
                    response_modalities=["AUDIO"],
                    system_instruction=sys_inst,
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=v_name)
                        )
                    ),
                    realtime_input_config=types.RealtimeInputConfig(
                        automatic_activity_detection=types.AutomaticActivityDetection(
                            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
                            silence_duration_ms=450,
                            prefix_padding_ms=100,
                        )
                    ),
                    input_audio_transcription=types.AudioTranscriptionConfig(),
                    output_audio_transcription=types.AudioTranscriptionConfig(),
                )
                prewarmer = GeminiPrewarmer(api_key, GEMINI_MODEL, live_cfg)
                prewarmer.start_prewarm()
        else:
            dialer.ensure_phone_link_visible()
            call_type = "gsm"
            print("\n" + "="*50)
            print(" >>> CELLULAR GSM CALL ACTIVE - ENGAGING AI VOICE GATEWAY <<<")
            print("="*50)

        # Mark in-call state immediately across server and background daemons to halt polling and protect Bluetooth SCO
        try:
            set_in_call_state(True)
        except Exception:
            pass


        # Allow Windows Bluetooth SCO hardware link brief moment to settle
        await asyncio.sleep(0.15)

        # Re-resolve endpoints to capture freshly activated WASAPI or updated endpoints
        active_in, active_out = find_hfp_devices(quiet=True, force_refresh=False)
        if active_in is not None and active_out is not None:
            bridge = HFPBridge(in_dev=active_in, out_dev=active_out)
        else:
            bridge = HFPBridge(in_dev=cached_in_dev, out_dev=cached_out_dev)

        silencer = None
        try:
            loop = asyncio.get_running_loop()
            started = await loop.run_in_executor(None, lambda: bridge.start(is_outbound=is_outbound))
            if not started:
                try:
                    set_in_call_state(False)
                except Exception:
                    pass
                if is_outbound:
                    print("[Telephony] Outbound cellular call was not answered or cancelled. Returning to standby.")
                    dialer.cancel_outbound_dial()
                else:
                    print("[Telephony] Cellular incoming call was not answered or audio link could not engage. Returning to standby.")
                if prewarmer:
                    await prewarmer.release()
                    prewarmer = None
                _call_cancel_cooldown_until = time.time() + 4.0
                web_server.state["status"] = "standby"
                web_server.state["call_type"] = None
                web_server.state["outbound_state"] = None
                web_server.state["phone_call_active"] = False
                web_server.state["phone_call_incoming"] = False
                await web_server.broadcast("call_cancelled", {})
                await web_server.broadcast("outbound_cancelled", {})
                await web_server.broadcast("call_ended", {"metadata": None})
                await web_server.broadcast("state_update", web_server.get_current_state_dict())
                # Wait for cellular call to fully cease in WinRT before resuming standby monitoring
                for _ in range(40):
                    try:
                        if not PhoneCallManager.get_is_call_active():
                            break
                    except Exception:
                        break
                    await asyncio.sleep(0.1)
            else:
                # Call is now physically connected and recipient has answered!
                if is_outbound:
                    outbound_info = dialer.get_active_outbound() or outbound_info

                try:
                    set_in_call_state(True)
                except Exception:
                    pass

                # Mute Phone Link on laptop speakers so caller voice does not echo in the room
                silencer = PhoneLinkAudioSilencer()
                silencer.start()

                # Initialize Dual-Track Audio Recorder
                recorder = CallRecorder(call_type=call_type)
                if outbound_info:
                    recorder.extra_metadata = {
                        "direction": "outbound",
                        "outbound_info": outbound_info,
                    }
                recorder.start()

                # Update Server State
                web_server.state["status"] = "in_call"
                web_server.state["call_type"] = call_type
                web_server.state["call_id"] = recorder.call_id
                web_server.state["call_start_time"] = time.time()
                web_server.state["live_transcripts"] = []
                await web_server.broadcast("call_started", {
                    "call_id": recorder.call_id,
                    "call_type": call_type,
                    "outbound": outbound_info
                })
                await web_server.broadcast("state_update", {"state": web_server.get_current_state_dict()})

                stop_event = asyncio.Event()
                await asyncio.gather(
                    run_gemini_bridge(bridge, stop_event, recorder, web_server, prewarmer=prewarmer, outbound_info=outbound_info),
                    watch_call_and_hfp(bridge, stop_event, is_outbound=is_outbound),
                )
        except Exception as e:
            print(f"\n[Gateway Session Error]: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
        finally:
            if silencer:
                try:
                    silencer.stop()
                except Exception:
                    pass
                silencer = None

            if prewarmer:
                await prewarmer.release()
                prewarmer = None

            try:
                bridge.stop()
            except Exception:
                pass

            try:
                set_in_call_state(False)
            except Exception:
                pass

            # ONLY cancel outbound dial if this was genuinely an active outbound call
            try:
                if is_outbound and dialer.get_active_outbound():
                    dialer.cancel_outbound_dial()
                dialer.reset_transfer_state(force=True)
            except Exception:
                pass

            # Finalize call audio recording and broadcast to web platform
            metadata = None
            if started and recorder:
                metadata = recorder.finish()
                print("="*50)
                print(f" >>> CELLULAR CALL ENDED: Saved recording {metadata.get('wav_file')} ({metadata.get('duration_seconds')}s) <<<")
                print("="*50 + "\n")

            web_server.state["status"] = "standby"
            web_server.state["call_type"] = None
            web_server.state["call_id"] = None
            web_server.state["call_start_time"] = None
            web_server.state["caller_rms"] = 0
            web_server.state["ai_rms"] = 0
            web_server.state["phone_call_incoming"] = False
            web_server.state["outbound_state"] = None
            web_server.state["phone_call_active"] = False

            await web_server.broadcast("call_ended", {"metadata": metadata})
            await web_server.broadcast("outbound_cancelled", {})
            await web_server.broadcast("call_cancelled", {})
            await web_server.broadcast("state_update", web_server.get_current_state_dict())

        # Wait up to 3.0s for cellular call to fully clear in WinRT before returning to standby
        clear_wait = 0.0
        while PhoneCallManager.get_is_call_active() and clear_wait < 3.0:
            await asyncio.sleep(0.1)
            clear_wait += 0.1


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nStopping Callink Gateway...")
    except Exception as e:
        print(f"\n[FATAL ERROR IN GATEWAY]: {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        allow_sleep()
        print("Gateway stopped.")


if __name__ == "__main__":
    main()
