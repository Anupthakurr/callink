import audioop
import ctypes
from ctypes import wintypes
import os
import queue
import re
import threading
import time
import winreg
import numpy as np
import sounddevice as sd
import comtypes
from comtypes import GUID, COMMETHOD, HRESULT, POINTER, IUnknown

from audio_resampler import HFPResampler, resample_for_hfp

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


