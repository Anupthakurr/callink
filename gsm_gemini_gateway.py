import sys
sys.coinit_flags = 0

# Ensure immediate line flushing for real-time console feedback
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

import warnings
warnings.filterwarnings("ignore", category=FutureWarning)

import asyncio
import audioop
import os
import queue
import time
import traceback

# MUST import winrt before sounddevice to ensure COM is initialized in MTA mode
from winrt.windows.applicationmodel.calls import PhoneCallManager
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


def find_hfp_devices():
    devices = sd.query_devices()

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

    if input_device is None:
        raise RuntimeError(
            "Bluetooth HFP input device not found. "
            "Make sure the smartphone is connected through Bluetooth Hands-Free."
        )

    if output_device is None:
        raise RuntimeError(
            "Bluetooth HFP output device not found. "
            "Make sure the smartphone is connected through Bluetooth Hands-Free."
        )

    print(f"HFP INPUT  : [{input_device}] {devices[input_device]['name']}")
    print(f"HFP OUTPUT : [{output_device}] {devices[output_device]['name']}")

    return input_device, output_device


HFP_INPUT, HFP_OUTPUT = find_hfp_devices()

HFP_RATE = 16000       # native rate of the Windows HFP endpoint
HFP_BLOCK = 320        # 20ms block @ 16kHz (320 samples = 640 bytes)

# Gemini Live API audio contract:
#   input  (you -> model):  16-bit PCM, mono, 16 kHz
#   output (model -> you):  16-bit PCM, mono, 24 kHz
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000

GEMINI_MODEL = "gemini-3.1-flash-live-preview"

# Barge-in VAD parameters (for client-side speech detection while AI is playing)
# Calibrated for smartphone cellular HFP:
# Ambient noise, breathing & earpiece acoustic leakage: RMS ~1000 - 2800
# Real intentional speech: RMS ~4000 - 9000
VAD_RMS_THRESHOLD = 3600
VAD_TRIGGER_CONSECUTIVE = 5       # ~100ms of sustained speech above threshold (prevents pops/breaths from clipping AI)
VAD_INITIAL_GRACE_PERIOD = 0.35   # 350ms grace period after AI speech starts before barge-in can trigger (prevents self-clipping)


class HFPBridge:
    """Reads/writes the HFP device and exposes two queues:
    to_ai (caller's voice, for Gemini) and
    to_hfp (AI's synthesized reply, played into the phone call)."""

    def __init__(self, in_dev=None, out_dev=None):
        self.to_ai = queue.Queue(maxsize=300)
        self.to_hfp = queue.Queue(maxsize=1000)

        self.input_device = in_dev if in_dev is not None else HFP_INPUT
        self.output_device = out_dev if out_dev is not None else HFP_OUTPUT

        self.input_stream = None
        self.output_stream = None

        self._out_resample_state = None
        self.total_input_frames = 0
        self.total_output_frames = 0

        self.playback_started_at = None
        self.last_interrupted_at = 0.0

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
            except queue.Empty:
                self.playback_started_at = None
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
        try:
            print("Opening HFP input...")
            self.input_stream = sd.InputStream(
                device=self.input_device,
                samplerate=HFP_RATE,
                channels=1,
                dtype="int16",
                blocksize=HFP_BLOCK,
                callback=self.input_callback,
            )
            print("HFP input opened.")

            print("Opening HFP output...")
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
            print("HFP input started.")

            self.output_stream.start()
            print("HFP output started.")

            print("HFP bridge audio running.")
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


async def run_gemini_bridge(bridge: HFPBridge, stop_event: asyncio.Event):
    """Owns the Gemini Live session for the duration of one call.
    Pulls caller audio from bridge.to_ai, streams it to Gemini,
    and pushes Gemini's spoken reply onto bridge.to_hfp."""

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY environment variable is not set!")

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=(
            "You are a helpful, natural, and polite AI phone assistant answering calls for the business. "
            "Speak conversationally in short, crisp sentences (1-2 sentences at a time) as in a real phone call. "
            "Respond immediately and concisely without long preambles."
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
    )

    print("Connecting to Gemini Live...")

    try:
        async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
            print("Gemini Live session connected.")
            print("Gateway active.\n")

            # Flush any stale audio buffered in to_ai during connection establishment
            flushed = 0
            while not bridge.to_ai.empty():
                try:
                    bridge.to_ai.get_nowait()
                    flushed += 1
                except queue.Empty:
                    break
            if flushed > 0:
                print(f"[Audio] Flushed {flushed} stale frames from pre-connection buffer.")

            # Proactively prompt Gemini to greet the caller immediately upon connection
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

            async def sender():
                """Drain caller audio from bridge.to_ai, stream to Gemini, and monitor for barge-in."""
                consecutive_speech_chunks = 0
                sent_chunks = 0
                zero_chunks = 0
                last_report = time.time()
                latest_rms = 0

                while not stop_event.is_set():
                    # Periodic diagnostic heartbeat every 2 seconds
                    now = time.time()
                    if now - last_report >= 2.0:
                        print(
                            f"[Audio Status] Queue: {bridge.to_ai.qsize()}, "
                            f"Mic frames: {bridge.total_input_frames}, "
                            f"Sent: {sent_chunks}, Zeroes: {zero_chunks}, "
                            f"Latest RMS: {latest_rms}"
                        )
                        last_report = now

                    try:
                        pcm16 = bridge.to_ai.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    except Exception:
                        continue

                    latest_rms = audioop.rms(pcm16, 2)

                    # Skip uninitialized flatline digital zeroes (muted driver buffers)
                    if not any(pcm16):
                        zero_chunks += 1
                        await asyncio.sleep(0.005)
                        continue

                    # Client-side barge-in detection:
                    if not bridge.to_hfp.empty():
                        ai_speaking_sec = (
                            time.time() - bridge.playback_started_at
                            if bridge.playback_started_at is not None
                            else 0.0
                        )
                        # Suppress false barge-in during the first 350ms of AI speech to prevent self-clipping from initial acoustic reflection
                        if ai_speaking_sec >= VAD_INITIAL_GRACE_PERIOD:
                            if latest_rms > VAD_RMS_THRESHOLD:
                                consecutive_speech_chunks += 1
                                if consecutive_speech_chunks >= VAD_TRIGGER_CONSECUTIVE:
                                    print(f"[BARGE-IN] Caller voice detected locally (RMS={latest_rms}) - interrupting AI playback.")
                                    bridge.clear_playback()
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
                        # If first chunk sent, log it
                        if sent_chunks == 1:
                            print(f"[Audio] First active audio chunk streamed to Gemini! (RMS={latest_rms})")
                    except Exception as e:
                        if not stop_event.is_set():
                            print(f"[Sender] Error sending audio: {type(e).__name__}: {e}")
                            stop_event.set()
                        break

            async def receiver():
                """Pull Gemini's spoken response, resample, and push to bridge.to_hfp."""
                leftover_bytes = bytearray()
                chunk_size = 640  # 320 samples @ 16kHz 16-bit mono = exactly HFP_BLOCK

                while not stop_event.is_set():
                    try:
                        async for response in session.receive():
                            if stop_event.is_set():
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
                                continue

                            # Extract synthesized audio
                            audio_data = None
                            if server_content.model_turn:
                                for part in server_content.model_turn.parts:
                                    if part.text:
                                        print(f"[AI Transcript]: {part.text}")
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
                                print("[Gemini] Turn complete.")
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
                        if not stop_event.is_set():
                            rcvd = getattr(e, 'rcvd', None)
                            sent = getattr(e, 'sent', None)
                            code = getattr(e, 'code', None)
                            reason = getattr(e, 'reason', '')
                            print(f"[Receiver] Connection closed: {type(e).__name__}: {e} (code={code}, reason='{reason}', rcvd={rcvd}, sent={sent})")
                            stop_event.set()
                        break

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())

            await stop_event.wait()

            send_task.cancel()
            recv_task.cancel()
            await asyncio.gather(send_task, recv_task, return_exceptions=True)

    except Exception as e:
        if not stop_event.is_set():
            print(f"Gemini Live error: {type(e).__name__}: {e}")
    finally:
        stop_event.set()
        print("Gemini Live session closed.")


async def watch_call_and_hfp(bridge: HFPBridge, stop_event: asyncio.Event):
    """Monitors cellular call state and checks Bluetooth HFP health."""
    loop = asyncio.get_running_loop()
    inactive_count = 0
    start_time = time.time()

    while not stop_event.is_set():
        # Check call state
        try:
            call_active = PhoneCallManager.get_is_call_active()
        except Exception:
            call_active = False

        if not call_active:
            inactive_count += 1
            # Require 3 consecutive checks (~1.5s) of inactivity to prevent transient glitches during call setup/handoff
            if inactive_count >= 3:
                print("\nCellular call became inactive (PhoneCallManager reported call ended).")
                stop_event.set()
                break
        else:
            inactive_count = 0

        # Stream health check with a 3.0s grace period for initial Bluetooth link stabilization
        if time.time() - start_time > 3.0:
            if not bridge.is_healthy():
                print("\n[WARNING] Bluetooth HFP link dropped mid-call! Attempting to reconnect...")
                bridge.stop()

                reconnected = False
                for _ in range(5):
                    if not PhoneCallManager.get_is_call_active() or stop_event.is_set():
                        break
                    try:
                        in_dev, out_dev = await loop.run_in_executor(None, find_hfp_devices)
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


def main():
    print("========================================")
    print("   GSM -> GEMINI LIVE AI VOICE GATEWAY")
    print("========================================")
    print()

    while True:
        print("Waiting for smartphone cellular call...")
        print()

        try:
            while not PhoneCallManager.get_is_call_active():
                time.sleep(0.5)

            print("CELLULAR CALL ACTIVE")
            print("Starting AI gateway...")
            print()

        except KeyboardInterrupt:
            print("Stopping...")
            return

        bridge = HFPBridge()

        try:
            bridge.start()

            async def runner():
                # Bound strictly to the running event loop of asyncio.run()
                stop_event = asyncio.Event()
                await asyncio.gather(
                    run_gemini_bridge(bridge, stop_event),
                    watch_call_and_hfp(bridge, stop_event),
                )

            asyncio.run(runner())

            print("CELLULAR CALL ENDED")
            print("Stopping gateway session...")

        except KeyboardInterrupt:
            print("Stopping...")
            break

        except Exception as e:
            print(f"\nFATAL ERROR: {type(e).__name__}: {e}")
            traceback.print_exc(file=sys.stdout)

        finally:
            try:
                bridge.stop()
            except Exception:
                pass
            print("Gateway session cleaned up.\n")

        # Wait until cellular call is fully cleared before listening for the next call
        while PhoneCallManager.get_is_call_active():
            time.sleep(0.5)

    print("Gateway stopped.")


if __name__ == "__main__":
    main()
