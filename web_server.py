import os
import sys
import json
import time
import asyncio
import audioop
import numpy as np
from datetime import datetime
from aiohttp import web, WSMsgType

from call_recorder import (
    CallRecorder,
    get_all_recordings,
    delete_recording,
    RECORDINGS_DIR,
)
import sounddevice as sd
import re

def clean_device_name(name: str) -> str:
    if not name:
        return ""
    # Match alias in ;(alias)) or (alias)
    alias_match = re.search(r';\(([^)]+)\)\s*\)?$', name)
    alias = alias_match.group(1).strip() if alias_match else ''
    
    if "hands-free hf audio" in name.lower():
        base = "Hands-Free HF Audio"
    elif "hands-free" in name.lower():
        base = "Bluetooth Hands-Free"
    else:
        base = re.sub(r'\(?@System32[^;]+;?', '', name, flags=re.IGNORECASE)
        base = re.sub(r'%[0-9]', '', base).replace(';', ' ')
        base = re.sub(r'\s+', ' ', base).strip(' ()')
    
    if alias:
        return f"{base} ({alias})"
    return base

import ctypes
import uuid
from ctypes import wintypes

cfgmgr32 = ctypes.windll.cfgmgr32

class DEVPROPKEY(ctypes.Structure):
    _fields_ = [
        ('fmtid', ctypes.c_byte * 16),
        ('pid', wintypes.ULONG)
    ]

# DEVPKEY_Device_IsConnected: {83DA6326-97A6-4088-9453-A1923F573B29}, 15
u_conn = uuid.UUID('{83DA6326-97A6-4088-9453-A1923F573B29}')
PKEY_IS_CONNECTED = DEVPROPKEY()
PKEY_IS_CONNECTED.fmtid = (ctypes.c_byte * 16)(*u_conn.bytes_le)
PKEY_IS_CONNECTED.pid = 15

# DEVPKEY_NAME: {b725f130-4704-43b4-a0be-2e0c4598340c}, 10
u_name = uuid.UUID('{b725f130-4704-43b4-a0be-2e0c4598340c}')
PKEY_NAME = DEVPROPKEY()
PKEY_NAME.fmtid = (ctypes.c_byte * 16)(*u_name.bytes_le)
PKEY_NAME.pid = 10

def get_connected_hfp_device_aliases() -> set:
    """
    Directly queries Windows PnP hardware Configuration Manager (cfgmgr32).
    Returns a set of lowercase aliases/names of Bluetooth Hands-Free devices
    that are TRULY connected over the air right now.
    """
    try:
        buf_len = wintypes.ULONG()
        ret = cfgmgr32.CM_Get_Device_ID_List_SizeW(ctypes.byref(buf_len), 'BTHHFENUM', 0)
        if ret != 0 or buf_len.value <= 1:
            return set()
        buf = ctypes.create_unicode_buffer(buf_len.value)
        if cfgmgr32.CM_Get_Device_ID_ListW('BTHHFENUM', buf, buf_len.value, 0) != 0:
            return set()
        device_ids = [s for s in buf[:].split('\0') if s and 'BTHHFPAudio' in s]
        connected_aliases = set()
        for dev_id in device_ids:
            devnode = wintypes.DWORD()
            if cfgmgr32.CM_Locate_DevNodeW(ctypes.byref(devnode), dev_id, 0) == 0:
                prop_type = wintypes.ULONG()
                conn_buf = (ctypes.c_byte * 1)()
                conn_size = wintypes.ULONG(1)
                if cfgmgr32.CM_Get_DevNode_PropertyW(
                    devnode, ctypes.byref(PKEY_IS_CONNECTED), ctypes.byref(prop_type),
                    ctypes.byref(conn_buf), ctypes.byref(conn_size), 0
                ) == 0:
                    if bool(conn_buf[0]):
                        name_buf = ctypes.create_unicode_buffer(256)
                        name_size = wintypes.ULONG(512)
                        if cfgmgr32.CM_Get_DevNode_PropertyW(
                            devnode, ctypes.byref(PKEY_NAME), ctypes.byref(prop_type),
                            ctypes.byref(name_buf), ctypes.byref(name_size), 0
                        ) == 0:
                            connected_aliases.add(name_buf.value.lower().strip())
                        connected_aliases.add(dev_id.lower().strip())
        return connected_aliases
    except Exception:
        return set()

def check_hfp_hardware():
    try:
        connected_aliases = get_connected_hfp_device_aliases()
        # If Windows kernel reports no Bluetooth HFP hardware is actively connected:
        if not connected_aliases:
            return False, None, None

        devices = sd.query_devices()
        in_name = None
        out_name = None

        def extract_alias(cleaned_name):
            m = re.search(r'\(([^)]+)\)', cleaned_name)
            return m.group(1).lower().strip() if m else ""

        # 1. First priority: Hands-Free HF Audio (Phone Link cellular bridge)
        for d in devices:
            name = d.get("name", "")
            if "hands-free hf audio" in name.lower():
                cleaned = clean_device_name(name)
                alias = extract_alias(cleaned)
                # Ensure device alias matches one of the truly connected hardware aliases
                if not any(alias in ca or ca in alias for ca in connected_aliases):
                    continue
                if d.get("max_input_channels", 0) > 0 and in_name is None:
                    in_name = cleaned
                if d.get("max_output_channels", 0) > 0 and out_name is None:
                    out_name = cleaned

        # 2. Second priority: any other hands-free headset that is actively connected
        if not (in_name and out_name):
            for d in devices:
                name = d.get("name", "")
                if "hands-free" in name.lower():
                    cleaned = clean_device_name(name)
                    alias = extract_alias(cleaned)
                    if not any(alias in ca or ca in alias for ca in connected_aliases):
                        continue
                    if d.get("max_input_channels", 0) > 0 and in_name is None:
                        in_name = cleaned
                    if d.get("max_output_channels", 0) > 0 and out_name is None:
                        out_name = cleaned

        connected = (in_name is not None and out_name is not None)
        return connected, in_name, out_name
    except Exception:
        return False, None, None

# pip install google-genai
from google import genai
from google.genai import types

if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BASE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BASE_DIR

WEB_DIR = os.path.join(BASE_DIR, "web")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")

# Default gateway settings
DEFAULT_SETTINGS = {
    "voice_name": "Puck",
    "system_instruction": (
        "You are a helpful, natural, and polite AI phone assistant answering calls for the business. "
        "Speak conversationally in short, crisp sentences (1-2 sentences at a time) as in a real phone call. "
        "Respond immediately and concisely without long preambles."
    ),
    "vad_threshold": 3600,
    "barge_in_grace_period": 0.35,
    "auto_launch_browser": True
}


def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                merged = dict(DEFAULT_SETTINGS)
                merged.update(data)
                return merged
        except Exception:
            pass
    return dict(DEFAULT_SETTINGS)


def save_settings(new_settings: dict):
    current = load_settings()
    current.update(new_settings)
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=2)
    except Exception as e:
        print(f"[Settings Error] Could not save settings: {e}")


class WebGatewayServer:
    """
    Asynchronous Web Platform Server for Callink.
    Hosts the web dashboard, manages real-time WebSockets,
    serves call audio recordings, and provides live browser-call testing.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5000):
        self.host = host
        self.port = port
        self.start_time = time.time()
        self.app = web.Application()
        self.runner = None
        self.site = None

        self.ws_clients = set()
        self.browser_sessions = {}

        # Gateway state snapshot
        self.state = {
            "status": "standby",      # 'stopped', 'standby', 'ringing', 'in_call'
            "call_type": None,         # 'gsm' or 'browser'
            "call_id": None,
            "call_start_time": None,
            "caller_rms": 0,
            "ai_rms": 0,
            "hfp_input_name": "Scanning Bluetooth endpoints...",
            "hfp_output_name": "Scanning Bluetooth endpoints...",
            "hfp_connected": False,
            "hfp_alias": "",
            "api_key_configured": bool(os.environ.get("GEMINI_API_KEY")),
            "total_calls_today": 0,
            "live_transcripts": []
        }

        self.setup_routes()

    def get_current_state_dict(self) -> dict:
        import platform
        connected, in_name, out_name = check_hfp_hardware()
        self.state["hfp_connected"] = connected
        self.state["hfp_input_name"] = in_name or "Not connected"
        self.state["hfp_output_name"] = out_name or "Not connected"

        # Extract alias like 'myvault'
        alias = ""
        if in_name and "(" in in_name and ")" in in_name:
            m = re.search(r'\(([^)]+)\)', in_name)
            if m:
                alias = m.group(1).strip()
        self.state["hfp_alias"] = alias

        # Real OS Details
        os_name = platform.system()
        os_rel = platform.release()
        os_ver = platform.version()
        build_match = re.search(r'\.(\d+)$', os_ver)
        build_str = f" Build {build_match.group(1)}" if build_match else ""
        self.state["os_version"] = f"{os_name} {os_rel}{build_str} (WinRT)"

        phone_active = False
        phone_incoming = False
        winrt_available = False
        try:
            from winrt.windows.applicationmodel.calls import PhoneCallManager
            winrt_available = True
            phone_active = PhoneCallManager.get_is_call_active()
            phone_incoming = PhoneCallManager.get_is_call_incoming()
        except Exception:
            pass

        self.state["winrt_ready"] = winrt_available
        self.state["phone_call_active"] = phone_active
        self.state["phone_call_incoming"] = phone_incoming

        settings = load_settings()
        api_key = os.environ.get("GEMINI_API_KEY", "")
        self.state["api_key_configured"] = bool(api_key)
        self.state["gemini_model"] = "gemini-3.1-flash-live-preview"
        self.state["voice_name"] = settings.get("voice_name", "Puck")
        self.state["vad_threshold"] = settings.get("vad_threshold", 3600)
        self.state["audio_sample_rate"] = 16000
        self.state["audio_format"] = "16 kHz 16-Bit PCM Mono SCO"
        self.state["recording_mode"] = "Synchronized Stereo WAV (L: Caller | R: AI)"
        self.state["stay_awake_active"] = True
        self.state["recordings_count"] = len(get_all_recordings())
        self.state["server_uptime"] = int(time.time() - self.start_time)

        return dict(self.state)

    def setup_routes(self):
        # API Routes
        self.app.router.add_get("/api/status", self.handle_get_status)
        self.app.router.add_get("/api/recordings", self.handle_get_recordings)
        self.app.router.add_get("/api/recordings/{call_id}/audio", self.handle_get_recording_audio)
        self.app.router.add_get("/api/recordings/{call_id}/json", self.handle_get_recording_json)
        self.app.router.add_delete("/api/recordings/{call_id}", self.handle_delete_recording)
        self.app.router.add_get("/api/settings", self.handle_get_settings)
        self.app.router.add_post("/api/settings", self.handle_post_settings)

        # WebSocket Routes
        self.app.router.add_get("/ws/telemetry", self.handle_ws_telemetry)
        self.app.router.add_get("/ws/browser_call", self.handle_ws_browser_call)

        # Static Web Files
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_static("/static", WEB_DIR)

    async def handle_index(self, request):
        index_path = os.path.join(WEB_DIR, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text="Callink Web Platform is initializing...", content_type="text/html")

    async def handle_get_status(self, request):
        data = self.get_current_state_dict()
        return web.json_response(data)

    async def handle_get_recordings(self, request):
        recordings = get_all_recordings()
        return web.json_response({"recordings": recordings})

    async def handle_get_recording_audio(self, request):
        call_id = request.match_info.get("call_id")
        wav_path = os.path.join(RECORDINGS_DIR, f"{call_id}.wav")
        if not os.path.exists(wav_path):
            return web.Response(status=404, text="Recording audio file not found")
        return web.FileResponse(
            wav_path,
            headers={
                "Content-Type": "audio/wav",
                "Content-Disposition": f'inline; filename="{call_id}.wav"',
                "Accept-Ranges": "bytes",
            }
        )

    async def handle_get_recording_json(self, request):
        call_id = request.match_info.get("call_id")
        json_path = os.path.join(RECORDINGS_DIR, f"{call_id}.json")
        if not os.path.exists(json_path):
            return web.Response(status=404, text="Recording metadata not found")
        return web.FileResponse(json_path)

    async def handle_delete_recording(self, request):
        call_id = request.match_info.get("call_id")
        success = delete_recording(call_id)
        return web.json_response({"deleted": success, "call_id": call_id})

    async def handle_get_settings(self, request):
        settings = load_settings()
        api_key = os.environ.get("GEMINI_API_KEY", "")
        settings["api_key_masked"] = (api_key[:4] + "..." + api_key[-4:]) if len(api_key) > 8 else ("Set" if api_key else "Missing")
        return web.json_response(settings)

    async def handle_post_settings(self, request):
        try:
            body = await request.json()
            if "gemini_api_key" in body and body["gemini_api_key"].strip():
                new_key = body["gemini_api_key"].strip()
                os.environ["GEMINI_API_KEY"] = new_key
                # Save to .env as well
                env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
                try:
                    with open(env_file, "w") as f:
                        f.write(f"GEMINI_API_KEY={new_key}\n")
                except Exception:
                    pass

            # Save other settings
            update_data = {}
            for k in ["voice_name", "system_instruction", "vad_threshold", "barge_in_grace_period", "auto_launch_browser"]:
                if k in body:
                    update_data[k] = body[k]
            save_settings(update_data)
            return web.json_response({"success": True, "settings": load_settings()})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_ws_telemetry(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws_clients.add(ws)

        # Send immediate initial live state
        try:
            current_state = self.get_current_state_dict()
            await ws.send_json({
                "type": "state_update",
                "state": current_state
            })
        except Exception:
            pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("type") == "ping":
                            await ws.send_json({
                                "type": "pong",
                                "t": data.get("t"),
                                "server_time": time.time()
                            })
                    except Exception:
                        pass
        finally:
            self.ws_clients.discard(ws)
        return ws

    async def broadcast(self, message_type: str, data: dict):
        """Broadcasts real-time events to all active dashboard browser tabs."""
        if not self.ws_clients:
            return
        payload = json.dumps({"type": message_type, "data": data, "timestamp": time.time()})
        stale_clients = []
        for client in self.ws_clients:
            try:
                await client.send_str(payload)
            except Exception:
                stale_clients.append(client)
        for client in stale_clients:
            self.ws_clients.discard(client)

    def update_audio_levels(self, caller_rms: int, ai_rms: int):
        self.state["caller_rms"] = int(caller_rms)
        self.state["ai_rms"] = int(ai_rms)

    async def handle_ws_browser_call(self, request):
        """
        In-Browser Voice Call Studio:
        Allows users to have a full voice conversation with Gemini Live
        directly from their web browser mic & speakers, while recording the call!
        """
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            await ws.send_json({"type": "error", "message": "Gemini API Key is not configured."})
            await ws.close()
            return ws

        settings = load_settings()
        voice_name = settings.get("voice_name", "Puck")
        system_instruction = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])

        # Create browser recorder
        recorder = CallRecorder(call_type="browser")
        recorder.start()

        # Update server state
        self.state["status"] = "in_call"
        self.state["call_type"] = "browser"
        self.state["call_id"] = recorder.call_id
        self.state["call_start_time"] = time.time()
        self.state["live_transcripts"] = []
        await self.broadcast("call_started", {"call_id": recorder.call_id, "call_type": "browser"})

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

        stop_call_event = asyncio.Event()

        try:
            async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
                await ws.send_json({"type": "connected", "call_id": recorder.call_id})

                # Initial warm greeting
                try:
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[
                                types.Part(
                                    text=(
                                        "[A user has initiated an audio call with you in the browser. "
                                        "Greet them warmly and concisely in one short sentence.]"
                                    )
                                )
                            ],
                        ),
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Browser Call Greeting Error]: {e}")

                async def browser_receiver():
                    """Receives Gemini Live audio and streams back to browser WebSocket."""
                    while not stop_call_event.is_set():
                        try:
                            async for response in session.receive():
                                if stop_call_event.is_set():
                                    break
                                server_content = response.server_content
                                if not server_content:
                                    continue

                                if server_content.interrupted:
                                    recorder.add_event("barge_in")
                                    await ws.send_json({"type": "interrupted"})
                                    await self.broadcast("barge_in", {"call_id": recorder.call_id})
                                    continue

                                if server_content.model_turn:
                                    for part in server_content.model_turn.parts:
                                        if part.text:
                                            recorder.add_transcript("model", part.text)
                                            await ws.send_json({"type": "transcript", "role": "model", "text": part.text})
                                            await self.broadcast("transcript", {"role": "model", "text": part.text})
                                        if part.inline_data and part.inline_data.data:
                                            raw_audio_24k = part.inline_data.data
                                            ai_rms = audioop.rms(raw_audio_24k, 2)
                                            self.update_audio_levels(self.state["caller_rms"], ai_rms)

                                            # Resample 24k -> 16k for recorder
                                            pcm_16k, _ = audioop.ratecv(raw_audio_24k, 2, 1, 24000, 16000, None)
                                            # Write to recorder: silence for caller, pcm_16k for AI
                                            silence_caller = b"\x00" * len(pcm_16k)
                                            recorder.write_stereo_block(silence_caller, pcm_16k)

                                            # Send audio to browser (browser plays 24kHz raw PCM or sends as binary)
                                            await ws.send_bytes(raw_audio_24k)

                                if server_content.turn_complete:
                                    await ws.send_json({"type": "turn_complete"})

                        except asyncio.CancelledError:
                            break
                        except Exception as e:
                            print(f"[Browser Receiver Error]: {e}")
                            break

                recv_task = asyncio.create_task(browser_receiver())

                try:
                    async for msg in ws:
                        if msg.type == WSMsgType.BINARY:
                            # 16kHz 16-bit mono PCM from browser mic
                            mic_data = msg.data
                            caller_rms = audioop.rms(mic_data, 2)
                            self.update_audio_levels(caller_rms, self.state["ai_rms"])

                            # Record caller audio
                            silence_ai = b"\x00" * len(mic_data)
                            recorder.write_stereo_block(mic_data, silence_ai)

                            # Stream to Gemini
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=mic_data,
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )
                        elif msg.type == WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("type") == "end_call":
                                break
                finally:
                    stop_call_event.set()
                    recv_task.cancel()
                    await asyncio.gather(recv_task, return_exceptions=True)

        except Exception as e:
            print(f"[Browser Call Error]: {e}")
            await ws.send_json({"type": "error", "message": str(e)})

        finally:
            meta = recorder.finish()
            self.state["status"] = "standby"
            self.state["call_type"] = None
            self.state["call_id"] = None
            self.state["call_start_time"] = None
            self.state["caller_rms"] = 0
            self.state["ai_rms"] = 0
            await self.broadcast("call_ended", {"metadata": meta})
            if not ws.closed:
                await ws.send_json({"type": "call_ended", "metadata": meta})
                await ws.close()

        return ws

    async def start(self):
        """Starts the AIOHTTP server asynchronously."""
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()

        # Try designated port, or search next available if occupied
        port = self.port
        for p in range(port, port + 10):
            try:
                self.site = web.TCPSite(self.runner, self.host, p)
                await self.site.start()
                self.port = p
                print(f"[Web Platform] Dashboard active and listening at: http://{self.host}:{self.port}")
                return
            except OSError:
                continue

        raise RuntimeError(f"Could not bind Web Platform to ports {port}-{port+9}")

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
            print("[Web Platform] Server stopped.")
