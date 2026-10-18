import asyncio
import audioop
import os
import queue
import time
import traceback
from google import genai
from google.genai import types

try:
    from winrt.windows.applicationmodel.calls import PhoneCallManager
except Exception:
    PhoneCallManager = None

from audio_bridge import (
    HFPBridge,
    HFP_RATE,
    HFP_BLOCK,
    GEMINI_IN_RATE,
    GEMINI_OUT_RATE,
    GEMINI_MODEL,
    VAD_RMS_THRESHOLD,
    VAD_TRIGGER_CONSECUTIVE,
    VAD_INITIAL_GRACE_PERIOD,
)
from audio_resampler import HFPResampler, resample_for_hfp
import outbound_dialer as dialer
import knowledge_manager as km
from call_recorder import CallRecorder
from web_server import WebGatewayServer, load_settings, DEFAULT_SETTINGS

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
                            greeting_turn = "[इनकमिंग कॉल कनेक्ट हो गया है। तुरंत बोलें: 'नमस्ते! बताइए, मैं आपकी क्या सहायता कर सकता हूँ?' — कभी भी 'कॉल करने के लिए धन्यवाद' न कहें।]"
                        elif lang == "mai":
                            greeting_turn = "[इनकमिंग कॉल कनेक्ट भऽ गेल। तुरंत बोलू: 'प्रणाम! कहू, हम अहाँक की मद्द कऽ सकैत छी?' — कखनो 'कॉल करय लेल धन्यवाद' नहि कहब।]"
                        else:
                            greeting_turn = "[Incoming call connected. Immediately say: 'Hello! How can I help you today?' — NEVER say 'thanks for calling'.]" 
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

                try:
                    while not stop_event.is_set() and not session_stop_event.is_set():
                        now = time.time()
                        if now - last_report >= 2.0:
                            last_report = now
                            ai_status = "AI Speaking (echo muted)" if bridge.has_pending_playback() else "Line Open"
                            print(f"[Audio Link] 🎙️ Caller Audio: RMS={latest_rms} | Streamed: {sent_chunks} chunks to Gemini Live ({ai_status})")

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
                            active_threshold = (
                                web_server.state.get("vad_threshold", vad_threshold)
                                if web_server and web_server.state.get("vad_threshold") is not None
                                else vad_threshold
                            )
                            active_grace = (
                                float(web_server.state.get("barge_in_grace_period", VAD_INITIAL_GRACE_PERIOD))
                                if web_server and web_server.state.get("barge_in_grace_period") is not None
                                else VAD_INITIAL_GRACE_PERIOD
                            )
                            if ai_speaking_sec >= active_grace:
                                if latest_rms > active_threshold:
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
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    print(f"[Sender Fatal Error]: {type(e).__name__}: {e}")
                    traceback.print_exc()
                    session_stop_event.set()

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

            stop_task = asyncio.create_task(stop_event.wait())
            session_stop_task = asyncio.create_task(session_stop_event.wait())

            done, pending = await asyncio.wait(
                [stop_task, session_stop_task, send_task, recv_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()

            for t in done:
                if not t.cancelled():
                    exc = t.exception()
                    if exc:
                        print(f"[Session Task Error]: {type(exc).__name__}: {exc}")

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

                is_outbound_reconnect = bool(outbound_info)
                outbound_ban = (
                    "5. CRITICAL OUTBOUND RULE: This is an OUTBOUND call — YOU placed this call. "
                    "NEVER say 'कॉल करने के लिए धन्यवाद', 'कॉल करय लेल धन्यवाद', 'thank you for calling', "
                    "or any similar phrase implying the customer called you. You called them.\n"
                ) if is_outbound_reconnect else ""
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
                    f"4. Seamlessly continue the conversation right where it left off. Respond directly, concisely (1-2 sentences), and naturally to whatever the caller says next.\n"
                    f"{outbound_ban}"
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

