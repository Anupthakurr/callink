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



# ==============================================================================
# MODULAR CALLINK ENGINE SUBSYSTEMS (100% Preserved Logic & Symbols)
# ==============================================================================
from audio_resampler import HFPResampler, resample_for_hfp
from power_guard import prevent_sleep, allow_sleep, start_stay_awake_heartbeat
from phone_audio_silencer import (
    is_pc_voip_call_active,
    set_phone_link_audio_silenced,
    PhoneLinkAudioSilencer,
)
from audio_bridge import (
    HFPBridge,
    find_active_bthhfenum_endpoints,
    find_hfp_devices,
    HFP_RATE,
    HFP_BLOCK,
    GEMINI_IN_RATE,
    GEMINI_OUT_RATE,
    GEMINI_MODEL,
    VAD_RMS_THRESHOLD,
    VAD_TRIGGER_CONSECUTIVE,
    VAD_INITIAL_GRACE_PERIOD,
    CLSID_MMDeviceEnumerator,
    IID_IMMDeviceEnumerator,
    IID_IMMDevice,
    IID_IAudioClient,
    IID_IAudioCaptureClient,
    IID_IAudioRenderClient,
    WAVEFORMATEX,
    IAudioCaptureClient,
    IAudioRenderClient,
    IAudioClient,
    IMMDevice,
    IMMDeviceEnumerator,
)
from gemini_live_bridge import run_gemini_bridge, GeminiPrewarmer


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
