import sys
import os
import traceback

sys.coinit_flags = 0

# Support windowed / no-console execution with file logging
_log_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv else os.getcwd()
_log_file = os.path.join(_log_dir, "callink_runtime.log")

class TeeLogger:
    def __init__(self):
        try:
            self.f = open(_log_file, "a", encoding="utf-8")
        except Exception:
            self.f = None
    def write(self, text):
        if self.f:
            try:
                self.f.write(text)
                self.f.flush()
            except Exception:
                pass
    def flush(self):
        if self.f:
            try: self.f.flush()
            except Exception: pass
    def reconfigure(self, **kwargs): pass

logger = TeeLogger()
if sys.stdout is None:
    sys.stdout = logger
if sys.stderr is None:
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


def find_hfp_devices(quiet: bool = False):
    """Discovers Windows Bluetooth Hands-Free HF Audio input and output devices."""
    try:
        devices = sd.query_devices()
    except Exception as e:
        if not quiet:
            print(f"[Audio Error] Could not query audio devices: {e}")
        return None, None

    input_device = None
    output_device = None

    for i, device in enumerate(devices):
        name = device["name"].lower()

        # Windows Bluetooth Hands-Free HF Audio endpoint
        if "hands-free hf audio" not in name:
            continue

        if device["max_input_channels"] > 0 and input_device is None:
            input_device = i

        if device["max_output_channels"] > 0 and output_device is None:
            output_device = i

    if input_device is None or output_device is None:
        if not quiet:
            print("[INFO] Bluetooth HFP device not yet detected. Ready for phone pairing via Phone Link.")
        return None, None

    if not quiet:
        print(f"HFP INPUT  : [{input_device}] {devices[input_device]['name']}")
        print(f"HFP OUTPUT : [{output_device}] {devices[output_device]['name']}")

    return input_device, output_device


HFP_RATE = 16000       # native rate of the Windows HFP endpoint
HFP_BLOCK = 320        # 20ms block @ 16kHz (320 samples = 640 bytes)

# Gemini Live API audio contract:
#   input  (you -> model):  16-bit PCM, mono, 16 kHz
#   output (model -> you):  16-bit PCM, mono, 24 kHz
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Barge-in VAD parameters (for client-side speech detection while AI is playing)
VAD_RMS_THRESHOLD = 3600
VAD_TRIGGER_CONSECUTIVE = 5       # ~100ms of sustained speech above threshold
VAD_INITIAL_GRACE_PERIOD = 0.35   # 350ms grace period after AI speech starts before barge-in triggers


class HFPBridge:
    """Reads/writes the HFP device and exposes two queues:
    to_ai (caller's voice, for Gemini) and
    to_hfp (AI's synthesized reply, played into the phone call).
    Also tracks playback state for real-time dual-track conversation recording."""

    def __init__(self, in_dev=None, out_dev=None):
        self.to_ai = queue.Queue(maxsize=300)
        self.to_hfp = queue.Queue(maxsize=1000)

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

    def output_callback(self, outdata, frames, time_info, status):
        try:
            outdata.fill(0)

            try:
                pcm16 = self.to_hfp.get_nowait()
                self.last_played_chunk = pcm16
            except queue.Empty:
                self.playback_started_at = None
                self.last_played_chunk = b"\x00" * 640
                return

            if self.playback_started_at is None:
                self.playback_started_at = time.time()

            samples = len(pcm16) // 2
            data = memoryview(pcm16).cast("h")
            count = min(samples, frames)

            if count > 0:
                outdata[:count, 0] = data[:count]
                self.total_output_frames += 1

        except Exception as e:
            print("HFP OUTPUT CALLBACK ERROR:", e)

    def clear_playback(self):
        """Immediately drain pending audio from to_hfp for instantaneous barge-in."""
        self.last_interrupted_at = time.time()
        self.playback_started_at = None
        self.last_played_chunk = b"\x00" * 640
        drained_chunks = 0
        while not self.to_hfp.empty():
            try:
                self.to_hfp.get_nowait()
                drained_chunks += 1
            except queue.Empty:
                break
        if drained_chunks > 0:
            print(f"[BARGE-IN] Drained {drained_chunks} pending audio chunks from playback queue.")

    def is_healthy(self) -> bool:
        """Check whether both input and output streams exist and are active."""
        try:
            in_ok = self.input_stream is not None and self.input_stream.active
            out_ok = self.output_stream is not None and self.output_stream.active
            return in_ok and out_ok
        except Exception:
            return False

    def start(self):
        # Dynamically discover endpoints if not provided
        if self.input_device is None or self.output_device is None:
            in_dev, out_dev = find_hfp_devices(quiet=True)
            if in_dev is None or out_dev is None:
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
        except Exception:
            self.stop()
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
    web_server: WebGatewayServer = None
):
    """Owns the Gemini Live session for the duration of one call.
    Streams caller audio to Gemini, plays synthesized replies into phone call,
    records synchronized dual-track stereo WAV audio, and streams telemetry to Web UI."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured! Please configure it on the Web Platform.")

    settings = load_settings()
    voice_name = settings.get("voice_name", "Puck")
    system_instruction = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
    vad_threshold = settings.get("vad_threshold", VAD_RMS_THRESHOLD)

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=system_instruction,
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        )
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

        if is_initial_connect:
            print("Connecting to Gemini Live API...")
        else:
            print("\n[Gateway] Mid-call reconnect: Connecting fresh Gemini Live session...")

        try:
            async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
                print("Gemini Live session connected.")
                print("AI Voice Gateway active.\n")

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

                async def sender():
                    """Drain caller audio, write stereo recording, stream to Gemini, monitor barge-in."""
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
                        current_ai_chunk = bridge.last_played_chunk
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
                        if not bridge.to_hfp.empty():
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
                    """Pull Gemini's spoken response, record transcripts, resample and push to HFP."""
                    leftover_bytes = bytearray()
                    chunk_size = 640  # 320 samples @ 16kHz 16-bit mono = exactly HFP_BLOCK

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
                                    print("[BARGE-IN] Server signaled interruption! Clearing playback queue.")
                                    bridge.clear_playback()
                                    leftover_bytes.clear()
                                    bridge._out_resample_state = None
                                    if recorder:
                                        recorder.add_event("server_barge_in")
                                    if web_server:
                                        await web_server.broadcast("barge_in", {"type": "server"})
                                    continue

                                # Extract synthesized audio & text transcripts
                                audio_data = None
                                if server_content.model_turn:
                                    for part in server_content.model_turn.parts:
                                        if part.text:
                                            print(f"[AI Transcript]: {part.text}")
                                            if recorder:
                                                recorder.add_transcript("model", part.text)
                                            if web_server:
                                                await web_server.broadcast("transcript", {"role": "model", "text": part.text})
                                        if part.inline_data and part.inline_data.data:
                                            audio_data = part.inline_data.data
                                            break

                                # If an interruption recently occurred, drop remaining stale audio from that turn
                                if time.time() - bridge.last_interrupted_at < 0.3:
                                    audio_data = None
                                    leftover_bytes.clear()

                                if audio_data is not None:
                                    pcm16_hfp = resample_for_hfp(audio_data, bridge)
                                    leftover_bytes.extend(pcm16_hfp)

                                    # Push complete 640-byte frames into playback queue
                                    while len(leftover_bytes) >= chunk_size:
                                        frame = bytes(leftover_bytes[:chunk_size])
                                        del leftover_bytes[:chunk_size]
                                        try:
                                            bridge.to_hfp.put_nowait(frame)
                                        except queue.Full:
                                            pass

                                # If turn completes and leftover audio exists, flush it
                                if server_content.turn_complete:
                                    if len(leftover_bytes) > 0:
                                        padding = chunk_size - len(leftover_bytes)
                                        leftover_bytes.extend(b"\x00" * padding)
                                        try:
                                            bridge.to_hfp.put_nowait(bytes(leftover_bytes))
                                        except queue.Full:
                                            pass
                                        leftover_bytes.clear()

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
            threshold = 10 if bridge.is_healthy() else 6
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

        await asyncio.sleep(0.5)


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

    # Update initial hardware detection in web_server state
    in_dev, out_dev = find_hfp_devices(quiet=True)
    if in_dev is not None and out_dev is not None:
        try:
            from web_server import clean_device_name
            devs = sd.query_devices()
            clean_in = clean_device_name(devs[in_dev]["name"])
            clean_out = clean_device_name(devs[out_dev]["name"])
            web_server.state["hfp_input_name"] = clean_in
            web_server.state["hfp_output_name"] = clean_out
            web_server.state["hfp_connected"] = True
            if "(" in clean_in and ")" in clean_in:
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
    print(" Browser Studio   : Available on Web Platform")
    print("============================================================\n")

    # 3. Main continuous standby loop for GSM cellular calls
    while True:
        print(f"[{time.strftime('%H:%M:%S')}] Standby: Monitoring for incoming cellular calls...")

        # Wait until a cellular call is detected
        while not PhoneCallManager.get_is_call_active():
            await asyncio.sleep(0.5)

        print("\n" + "="*50)
        print(" >>> CELLULAR GSM CALL ACTIVE - STARTING AI VOICE GATEWAY <<<")
        print("="*50)

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

        bridge = HFPBridge()

        try:
            bridge.start()
            stop_event = asyncio.Event()
            await asyncio.gather(
                run_gemini_bridge(bridge, stop_event, recorder, web_server),
                watch_call_and_hfp(bridge, stop_event),
            )
        except Exception as e:
            print(f"\n[Gateway Session Error]: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)
        finally:
            try:
                bridge.stop()
            except Exception:
                pass

            # Finalize call audio recording and broadcast to web platform
            metadata = recorder.finish()
            web_server.state["status"] = "standby"
            web_server.state["call_type"] = None
            web_server.state["call_id"] = None
            web_server.state["call_start_time"] = None
            web_server.state["caller_rms"] = 0
            web_server.state["ai_rms"] = 0
            await web_server.broadcast("call_ended", {"metadata": metadata})

            print("="*50)
            print(f" >>> CELLULAR CALL ENDED: Saved recording {metadata.get('wav_file')} ({metadata.get('duration_seconds')}s) <<<")
            print("="*50 + "\n")

        # Wait until cellular call is fully cleared before returning to standby
        while PhoneCallManager.get_is_call_active():
            await asyncio.sleep(0.5)


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
