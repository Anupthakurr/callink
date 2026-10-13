import sys
import os
import traceback

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
import os
import queue
import threading
import time
import traceback
import webbrowser

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
from web_server import WebGatewayServer, load_settings, DEFAULT_SETTINGS
import knowledge_manager as km


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


def find_hfp_devices(quiet: bool = False, force_refresh: bool = False):
    """Discovers Windows Bluetooth Hands-Free HF Audio input and output devices.
    Prioritizes WASAPI endpoints when available, falling back smoothly to WDM-KS."""
    if force_refresh:
        try:
            sd._terminate()
            sd._initialize()
        except Exception:
            pass

    try:
        devices = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception as e:
        if not quiet:
            print(f"[Audio Error] Could not query audio devices: {e}")
        return None, None

    # Host API preference ranking: WASAPI (most stable) > DirectSound > MME > WDM-KS
    api_ranks = {"windows wasapi": 4, "windows directsound": 3, "mme": 2, "windows wdm-ks": 1}

    def get_api_score(dev):
        hostapi_idx = dev.get("hostapi", -1)
        if 0 <= hostapi_idx < len(apis):
            api_name = apis[hostapi_idx].get("name", "").lower()
            return api_ranks.get(api_name, 0)
        return 0

    best_in_idx = None
    best_in_score = -1

    best_out_idx = None
    best_out_score = -1

    for i, device in enumerate(devices):
        name = device["name"].lower()
        if "hands-free" not in name and "handsfree" not in name:
            continue

        # Score based on exact name match & host API stability
        name_score = 10 if "hands-free hf audio" in name else 5
        score = name_score + get_api_score(device)

        if device["max_input_channels"] > 0 and score > best_in_score:
            best_in_idx = i
            best_in_score = score

        if device["max_output_channels"] > 0 and score > best_out_score:
            best_out_idx = i
            best_out_score = score

    if best_in_idx is None or best_out_idx is None:
        if not quiet:
            print("[INFO] Bluetooth HFP device not yet detected. Ready for phone pairing via Phone Link.")
        return None, None

    if not quiet:
        api_in = apis[devices[best_in_idx]["hostapi"]]["name"] if 0 <= devices[best_in_idx]["hostapi"] < len(apis) else "?"
        api_out = apis[devices[best_out_idx]["hostapi"]]["name"] if 0 <= devices[best_out_idx]["hostapi"] < len(apis) else "?"
        print(f"HFP INPUT  : [{best_in_idx}] {devices[best_in_idx]['name']} ({api_in})")
        print(f"HFP OUTPUT : [{best_out_idx}] {devices[best_out_idx]['name']} ({api_out})")

    return best_in_idx, best_out_idx


HFP_RATE = 16000       # native rate of the Windows HFP endpoint
HFP_BLOCK = 320        # 20ms block @ 16kHz (320 samples = 640 bytes)

# Gemini Live API audio contract:
#   input  (you -> model):  16-bit PCM, mono, 16 kHz
#   output (model -> you):  16-bit PCM, mono, 24 kHz
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Barge-in VAD parameters (for client-side speech detection while AI is playing)
VAD_RMS_THRESHOLD = 4200
VAD_TRIGGER_CONSECUTIVE = 8       # ~160ms of sustained speech above threshold
VAD_INITIAL_GRACE_PERIOD = 0.60   # 600ms grace period after AI speech starts before client barge-in triggers


class HFPBridge:
    """Reads/writes the Windows Bluetooth HFP device.
    Features a high-resilience byte-level FIFO playback buffer with Jitter Prebuffering
    to eliminate audio stuttering, clicks, and dropouts over Bluetooth SCO."""

    def __init__(self, in_dev=None, out_dev=None):
        self.to_ai = queue.Queue(maxsize=300)

        self.input_device = in_dev
        self.output_device = out_dev

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
        # 120ms pre-buffer cushion (16000 samples/sec * 2 bytes/sample * 0.12s = 3840 bytes)
        self.prebuffer_target_bytes = 3840

    def input_callback(self, indata, frames, time_info, status):
        try:
            pcm16 = indata[:, 0].tobytes()
            self.total_input_frames += 1
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
        with self._playback_lock:
            return len(self._playback_bytes) > 0 or not self._is_buffering

    def output_callback(self, outdata, frames, time_info, status):
        try:
            outdata.fill(0)
            bytes_needed = frames * 2  # 16-bit mono = 2 bytes per frame
            raw_chunk = b""

            with self._playback_lock:
                # If still collecting initial jitter cushion, output silence
                if self._is_buffering:
                    pass
                elif len(self._playback_bytes) == 0:
                    # If queue has completely drained, reset buffering for next utterance
                    self._is_buffering = True
                    self.playback_started_at = None
                else:
                    if self.playback_started_at is None:
                        self.playback_started_at = time.time()

                    bytes_to_pull = min(len(self._playback_bytes), bytes_needed)
                    raw_chunk = bytes(self._playback_bytes[:bytes_to_pull])
                    del self._playback_bytes[:bytes_to_pull]

                    # If buffer is now empty, re-arm buffering
                    if len(self._playback_bytes) == 0:
                        self._is_buffering = True
                        self.playback_started_at = None

            if raw_chunk:
                samples = len(raw_chunk) // 2
                data = memoryview(raw_chunk).cast("h")
                outdata[:samples, 0] = data
                self.total_output_frames += samples

            # Keep latest chunk for instant RMS telemetry
            out_bytes = outdata[:, 0].tobytes()
            self.last_played_chunk = out_bytes

            # Queue every played frame sequentially into the recording FIFO (no duplicates, no drops)
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

    def clear_playback(self):
        """Immediately drain pending audio from playback buffer for instantaneous barge-in."""
        self.last_interrupted_at = time.time()
        self.playback_started_at = None
        self.last_played_chunk = b"\x00" * 640
        with self._playback_lock:
            drained_bytes = len(self._playback_bytes)
            self._playback_bytes.clear()
            self._is_buffering = True
        # Drain recorder queue so interrupted audio cuts off immediately
        while not self.ai_record_queue.empty():
            try:
                self.ai_record_queue.get_nowait()
            except Exception:
                break
        if drained_bytes > 0:
            print(f"[BARGE-IN] Drained {drained_bytes} pending audio bytes from playback buffer.")

    def is_healthy(self) -> bool:
        """Check whether both input and output streams exist and are active."""
        try:
            in_ok = self.input_stream is not None and self.input_stream.active
            out_ok = self.output_stream is not None and self.output_stream.active
            return in_ok and out_ok
        except Exception:
            return False

    def start(self, max_retries: int = 10, retry_delay: float = 0.35):
        for attempt in range(1, max_retries + 1):
            if self.input_device is None or self.output_device is None:
                in_dev, out_dev = find_hfp_devices(quiet=True, force_refresh=(attempt > 1))
                if in_dev is None or out_dev is None:
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                        continue
                    raise RuntimeError(
                        "Bluetooth Hands-Free (HFP) endpoint not found. "
                        "Please ensure phone is paired via Microsoft Phone Link."
                    )
                self.input_device = in_dev
                self.output_device = out_dev

            try:
                print(f"Opening HFP input [{self.input_device}]...")
                self.input_stream = sd.InputStream(
                    device=self.input_device,
                    samplerate=HFP_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=HFP_BLOCK,
                    callback=self.input_callback,
                )
                print("HFP input opened.")

                print(f"Opening HFP output [{self.output_device}]...")
                self.output_stream = sd.OutputStream(
                    device=self.output_device,
                    samplerate=HFP_RATE,
                    channels=1,
                    dtype="int16",
                    blocksize=HFP_BLOCK,
                    callback=self.output_callback,
                )
                print("HFP output opened.")

                self.input_stream.start()
                print("HFP input stream started.")

                self.output_stream.start()
                print("HFP output stream started.")

                print("HFP audio bridge running.\n")
                return
            except Exception as e:
                self.stop()
                if attempt < max_retries:
                    print(f"[Audio Link] Bluetooth SCO hardware link negotiating ({type(e).__name__}). Retrying ({attempt}/{max_retries}) in {retry_delay}s...")
                    time.sleep(retry_delay)
                    # Re-verify device indices in case Windows adjusted endpoints upon SCO connect
                    in_dev, out_dev = find_hfp_devices(quiet=True, force_refresh=True)
                    if in_dev is not None and out_dev is not None:
                        self.input_device = in_dev
                        self.output_device = out_dev
                else:
                    print(f"[Audio Error] Failed to start Bluetooth HFP audio streams after {max_retries} attempts.")
                    raise

    def stop(self):
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


def resample_for_hfp(pcm_24k: bytes, bridge: HFPBridge = None) -> bytes:
    """Gemini's output audio (24kHz) -> HFP playback rate (16kHz)."""
    state = bridge._out_resample_state if bridge else None
    pcm_16k, new_state = audioop.ratecv(pcm_24k, 2, 1, GEMINI_OUT_RATE, HFP_RATE, state)
    if bridge:
        bridge._out_resample_state = new_state
    return pcm_16k


async def run_gemini_bridge(
    bridge: HFPBridge,
    stop_event: asyncio.Event,
    recorder: CallRecorder = None,
    web_server: WebGatewayServer = None,
    prewarmer: GeminiPrewarmer = None
):
    """Owns the Gemini Live session for the duration of one call.
    Streams caller audio to Gemini, plays synthesized replies into phone call,
    records synchronized dual-track stereo WAV audio, and streams telemetry to Web UI."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured! Please configure it on the Web Platform.")

    settings = load_settings()
    voice_name = settings.get("voice_name", "Puck")
    lang = settings.get("language", "en")
    base_inst = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
    system_instruction = km.get_effective_system_instruction(base_inst, language=lang)
    vad_threshold = settings.get("vad_threshold", VAD_RMS_THRESHOLD)

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
    )

    is_initial_connect = True

    while not stop_event.is_set():
        # Verify cellular call is still active
        try:
            if not PhoneCallManager.get_is_call_active():
                break
        except Exception:
            pass

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

            # Proactively prompt Gemini to greet caller on pickup
            if is_initial_connect:
                print("[Gateway] Requesting immediate AI greeting...")
                try:
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[
                                types.Part(
                                    text=(
                                        "[A caller has just connected to the phone call. "
                                        "Immediately greet them warmly and concisely in one short sentence, "
                                        "such as 'Hello! Thanks for calling, how can I help you today?']"
                                    )
                                )
                            ],
                        ),
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Gateway Warning] Could not send initial greeting trigger: {e}")
                is_initial_connect = False
            else:
                print("[Gateway] Seamlessly reconnected mid-call! Continuing conversation.")

            current_caller_text = ""
            current_ai_text = ""

            async def sender():
                """Drain caller audio, write stereo recording, stream to Gemini, monitor barge-in."""
                nonlocal current_ai_text
                consecutive_speech_chunks = 0
                sent_chunks = 0
                zero_chunks = 0
                last_report = time.time()
                latest_rms = 0

                while not stop_event.is_set() and not session_stop_event.is_set():
                    now = time.time()
                    if now - last_report >= 2.0:
                        last_report = now

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

                    try:
                        await session.send_realtime_input(
                            audio=types.Blob(
                                data=pcm16,
                                mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                            )
                        )
                        sent_chunks += 1
                    except Exception as e:
                        if not stop_event.is_set() and not session_stop_event.is_set():
                            print(f"[Sender] Error sending audio: {type(e).__name__}: {e}")
                        session_stop_event.set()
                        break

            async def receiver():
                """Pull Gemini's spoken response, resample, push to jitter buffer, and stream transcripts."""
                nonlocal current_caller_text, current_ai_text
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
                            audio_data = None
                            if server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        audio_data = part.inline_data.data
                                        break

                            # If an interruption recently occurred, drop remaining stale audio from that turn
                            if time.time() - bridge.last_interrupted_at < 0.2:
                                audio_data = None

                            if audio_data is not None:
                                pcm16_hfp = resample_for_hfp(audio_data, bridge)
                                bridge.push_playback_audio(pcm16_hfp)

                            # If turn completes, flush jitter buffer & finalize completed turns
                            if server_content.turn_complete:
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
            if active_session:
                await _run_session_pipeline(active_session)
            else:
                if is_initial_connect:
                    print("Connecting to Gemini Live API...")
                else:
                    print("\n[Gateway] Mid-call reconnect: Connecting fresh Gemini Live session...")

                async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as fresh_session:
                    await _run_session_pipeline(fresh_session)

        except Exception as e:
            if not stop_event.is_set():
                print(f"[Gemini Live] Session error: {type(e).__name__}: {e}")

        # Check if cellular call is still active
        try:
            call_still_active = PhoneCallManager.get_is_call_active()
        except Exception:
            call_still_active = False

        if not stop_event.is_set() and call_still_active:
            print("\n[Gateway] Cellular call is still ongoing! Reconnecting to a fresh Gemini Live session in 0.5s...")
            await asyncio.sleep(0.5)
        else:
            break

    print("Gemini Live session closed.")


async def watch_call_and_hfp(bridge: HFPBridge, stop_event: asyncio.Event):
    """Monitors cellular call state and checks Bluetooth HFP health."""
    loop = asyncio.get_running_loop()
    inactive_count = 0
    start_time = time.time()

    while not stop_event.is_set():
        try:
            call_active = PhoneCallManager.get_is_call_active()
        except Exception:
            call_active = False

        if not call_active:
            inactive_count += 1
            threshold = 8 if bridge.is_healthy() else 5
            if inactive_count >= threshold:
                print("\nCellular call became inactive (PhoneCallManager reported call ended).")
                stop_event.set()
                break
        else:
            inactive_count = 0

        # Stream health check with a 3.0s grace period
        if time.time() - start_time > 3.0:
            if not bridge.is_healthy():
                print("\n[WARNING] Bluetooth HFP link dropped mid-call! Attempting to reconnect...")
                bridge.stop()

                reconnected = False
                for _ in range(5):
                    if not PhoneCallManager.get_is_call_active() or stop_event.is_set():
                        break
                    try:
                        in_dev, out_dev = await loop.run_in_executor(None, find_hfp_devices, True)
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

        await asyncio.sleep(0.2)


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
            devs = sd.query_devices()
            clean_in = clean_device_name(devs[cached_in_dev]["name"])
            clean_out = clean_device_name(devs[cached_out_dev]["name"])
            web_server.state["hfp_input_name"] = clean_in
            web_server.state["hfp_output_name"] = clean_out
            web_server.state["hfp_connected"] = True
            if "(" in clean_in and ")" in clean_in:
                import re
                m = re.search(r'\(([^)]+)\)', clean_in)
                if m:
                    web_server.state["hfp_alias"] = m.group(1).strip()
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

            # Strictly ignore VoIP calls ringing or active on the PC (WhatsApp Desktop, Teams, Skype, Zoom)
            if (call_active or call_incoming) and is_pc_voip_call_active():
                now = time.time()
                if now - last_voip_notice > 5.0:
                    last_voip_notice = now
                    print(f"[{time.strftime('%H:%M:%S')}] [Telephony] ℹ️ Ignoring PC VoIP call (WhatsApp Desktop / Messenger). Callink strictly handles cellular SIM calls.")
                await asyncio.sleep(0.3)
                continue

            if call_active:
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
                    lang = curr_settings.get("language", "en")
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

        print("\n" + "="*50)
        print(" >>> CELLULAR GSM CALL ACTIVE - ENGAGING AI VOICE GATEWAY <<<")
        print("="*50)

        # Mute background hardware monitor during call to protect audio threads
        from web_server import set_in_call_state
        set_in_call_state(True)

        # Initialize Dual-Track Audio Recorder
        recorder = CallRecorder(call_type="gsm")
        recorder.start()

        # Update Server State
        web_server.state["status"] = "in_call"
        web_server.state["call_type"] = "gsm"
        web_server.state["call_id"] = recorder.call_id
        web_server.state["call_start_time"] = time.time()
        web_server.state["live_transcripts"] = []
        await web_server.broadcast("call_started", {"call_id": recorder.call_id, "call_type": "gsm"})

        # Allow Windows Bluetooth SCO hardware link 350ms to settle upon pickup
        await asyncio.sleep(0.35)

        # Re-resolve endpoints to capture freshly activated WASAPI or updated endpoints
        active_in, active_out = find_hfp_devices(quiet=True, force_refresh=True)
        if active_in is not None and active_out is not None:
            bridge = HFPBridge(in_dev=active_in, out_dev=active_out)
        else:
            bridge = HFPBridge(in_dev=cached_in_dev, out_dev=cached_out_dev)

        try:
            bridge.start()
            stop_event = asyncio.Event()
            await asyncio.gather(
                run_gemini_bridge(bridge, stop_event, recorder, web_server, prewarmer=prewarmer),
                watch_call_and_hfp(bridge, stop_event),
            )
        except Exception as e:
            print(f"\n[Gateway Session Error]: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
        finally:
            if prewarmer:
                await prewarmer.release()
                prewarmer = None

            try:
                bridge.stop()
            except Exception:
                pass

            set_in_call_state(False)

            # Finalize call audio recording and broadcast to web platform
            metadata = recorder.finish()
            web_server.state["status"] = "standby"
            web_server.state["call_type"] = None
            web_server.state["call_id"] = None
            web_server.state["call_start_time"] = None
            web_server.state["caller_rms"] = 0
            web_server.state["ai_rms"] = 0
            web_server.state["phone_call_incoming"] = False
            web_server.state["phone_call_active"] = False
            await web_server.broadcast("call_ended", {"metadata": metadata})

            print("="*50)
            print(f" >>> CELLULAR CALL ENDED: Saved recording {metadata.get('wav_file')} ({metadata.get('duration_seconds')}s) <<<")
            print("="*50 + "\n")

        # Wait until cellular call is fully cleared before returning to standby
        while PhoneCallManager.get_is_call_active():
            await asyncio.sleep(0.1)


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
