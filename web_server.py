# Initialize WinRT COM MTA apartment before any other libraries initialize COM
try:
    import winrt
    import winrt.windows.devices.bluetooth as bt
    import winrt.windows.devices.radios as radios
    import winrt.windows.devices.enumeration as enum
except Exception:
    bt = None
    radios = None
    enum = None

import os
import sys

# Support windowed / no-console execution without crashes
if sys.stdout is None:
    class DummyWriter:
        def write(self, *args, **kwargs): pass
        def flush(self): pass
        def reconfigure(self, **kwargs): pass
    sys.stdout = DummyWriter()
if sys.stderr is None:
    sys.stderr = sys.stdout

import json
import time
import threading
import asyncio
import collections
import audioop
import numpy as np
from datetime import datetime
from aiohttp import web, WSMsgType

# Terminal log ring buffer for web console streaming
MAX_TERMINAL_LOGS = 2000
terminal_logs = collections.deque(maxlen=MAX_TERMINAL_LOGS)
terminal_lock = threading.Lock()
_global_web_server = None

def add_terminal_log(text: str):
    """Appends live console logs to the ring buffer and pushes them via WebSocket to active browser tabs."""
    global _global_web_server
    if not text:
        return
    lines = text.splitlines()
    now_ts = time.time()
    time_str = time.strftime("%H:%M:%S", time.localtime(now_ts))
    formatted_entries = []
    with terminal_lock:
        for line in lines:
            if not line:
                continue
            entry = {
                "text": line,
                "timestamp": now_ts,
                "time_str": time_str
            }
            terminal_logs.append(entry)
            formatted_entries.append(entry)

    if _global_web_server and _global_web_server.loop and _global_web_server.ws_clients and formatted_entries:
        try:
            asyncio.run_coroutine_threadsafe(
                _global_web_server.broadcast_terminal_logs(formatted_entries),
                _global_web_server.loop
            )
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

from call_recorder import (
    CallRecorder,
    get_all_recordings,
    delete_recording,
    RECORDINGS_DIR,
)
import knowledge_manager as km
import outbound_dialer as dialer
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

_hw_cache = {
    "timestamp": 0.0,
    "connected": False,
    "in_name": None,
    "out_name": None,
    "alias": "",
    "in_call": False
}

def set_in_call_state(in_call: bool):
    """Mutes hardware scanning during active calls to protect real-time audio threads."""
    _hw_cache["in_call"] = in_call

def probe_hardware_sync():
    """Lightweight, non-blocking check of Bluetooth radio and HFP audio endpoints."""
    try:
        radio_on = True
        try:
            import winrt.windows.devices.bluetooth as bt
            import winrt.windows.devices.radios as radios

            async def _check_radio():
                adapter = await bt.BluetoothAdapter.get_default_async()
                if adapter:
                    radio = await adapter.get_radio_async()
                    return radio and radio.state == radios.RadioState.ON
                return False

            loop = asyncio.new_event_loop()
            radio_on = loop.run_until_complete(_check_radio())
            loop.close()
        except Exception:
            radio_on = True

        if not radio_on:
            _hw_cache["connected"] = False
            _hw_cache["in_name"] = None
            _hw_cache["out_name"] = None
            _hw_cache["alias"] = ""
            return

        devices = sd.query_devices()
        in_name = None
        out_name = None

        # 1. Primary: Hands-Free HF Audio (Phone Link cellular bridge)
        for d in devices:
            name = d.get("name", "")
            if "hands-free hf audio" in name.lower():
                if d.get("max_input_channels", 0) > 0 and in_name is None:
                    in_name = clean_device_name(name)
                if d.get("max_output_channels", 0) > 0 and out_name is None:
                    out_name = clean_device_name(name)

        # 2. Secondary fallback: any other hands-free device
        if not (in_name and out_name):
            for d in devices:
                name = d.get("name", "")
                if "hands-free" in name.lower():
                    if d.get("max_input_channels", 0) > 0 and in_name is None:
                        in_name = clean_device_name(name)
                    if d.get("max_output_channels", 0) > 0 and out_name is None:
                        out_name = clean_device_name(name)

        connected = (in_name is not None and out_name is not None)
        _hw_cache["connected"] = connected
        _hw_cache["in_name"] = in_name
        _hw_cache["out_name"] = out_name

        alias = ""
        if in_name and "(" in in_name and ")" in in_name:
            m = re.search(r'\(([^)]+)\)', in_name)
            if m:
                alias = m.group(1).strip()
        _hw_cache["alias"] = alias
    except Exception:
        pass

def _hardware_monitor_worker():
    while True:
        time.sleep(4.0)
        if _hw_cache.get("in_call", False):
            continue
        probe_hardware_sync()

# Initial probe and background monitoring daemon
probe_hardware_sync()
_monitor_thread = threading.Thread(target=_hardware_monitor_worker, daemon=True, name="HfpHardwareMonitor")
_monitor_thread.start()

def check_hfp_hardware():
    return _hw_cache["connected"], _hw_cache["in_name"], _hw_cache["out_name"]

# pip install google-genai
from google import genai
from google.genai import types
import knowledge_manager as km

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
    "language": "en",
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
        self.loop = None

        global _global_web_server
        _global_web_server = self

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
            if is_pc_voip_call_active():
                phone_active = False
                phone_incoming = False
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
        self.state["language"] = settings.get("language", "en")
        self.state["vad_threshold"] = settings.get("vad_threshold", 3600)
        self.state["audio_sample_rate"] = 16000
        self.state["audio_format"] = "16 kHz 16-Bit PCM Mono SCO"
        self.state["recording_mode"] = "Synchronized Stereo WAV (L: Caller | R: AI)"
        self.state["stay_awake_active"] = True
        self.state["recordings_count"] = len(get_all_recordings())
        self.state["knowledge_summary"] = km.get_knowledge_summary()
        self.state["server_uptime"] = int(time.time() - self.start_time)
        self.state["outbound_state"] = dialer.get_active_outbound()

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
        self.app.router.add_post("/api/shutdown", self.handle_post_shutdown)
        self.app.router.add_get("/api/logs", self.handle_get_logs)
        self.app.router.add_post("/api/logs/clear", self.handle_clear_logs)

        # Outbound Cellular Dialing API Routes
        self.app.router.add_post("/api/dial", self.handle_post_dial)
        self.app.router.add_post("/api/dial/cancel", self.handle_post_dial_cancel)
        self.app.router.add_get("/api/dial/status", self.handle_get_dial_status)

        # Knowledge Base Document API Routes
        self.app.router.add_get("/api/knowledge/documents", self.handle_get_knowledge_docs)
        self.app.router.add_post("/api/knowledge/upload", self.handle_post_knowledge_upload)
        self.app.router.add_delete("/api/knowledge/documents/{doc_id}", self.handle_delete_knowledge_doc)
        self.app.router.add_post("/api/knowledge/documents/{doc_id}/toggle", self.handle_toggle_knowledge_doc)
        self.app.router.add_get("/api/knowledge/documents/{doc_id}/preview", self.handle_get_knowledge_preview)
        self.app.router.add_get("/api/knowledge/summary", self.handle_get_knowledge_summary)

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

    # =========================================================================
    # Outbound Cellular Dialing API Handlers
    # =========================================================================

    async def handle_post_dial(self, request):
        try:
            data = await request.json()
            phone = data.get("phone_number", "").strip()
            contact_name = data.get("contact_name", "").strip()
            call_goal = data.get("call_goal", "").strip()
            lead_notes = data.get("lead_notes", "").strip()

            if not phone:
                return web.json_response({"error": "Phone number is required."}, status=400)

            outbound_info = dialer.start_outbound_dial(
                phone_number=phone,
                contact_name=contact_name,
                call_goal=call_goal,
                lead_notes=lead_notes
            )
            self.state["outbound_state"] = outbound_info
            await self.broadcast("outbound_dialing", {"outbound": outbound_info})
            return web.json_response({"success": True, "outbound": outbound_info})
        except ValueError as ve:
            return web.json_response({"error": str(ve)}, status=400)
        except Exception as e:
            return web.json_response({"error": f"Dialing failed: {e}"}, status=500)

    async def handle_post_dial_cancel(self, request):
        cancelled = dialer.cancel_outbound_dial()
        self.state["outbound_state"] = None
        self.state["status"] = "standby"
        self.state["call_type"] = None
        self.state["call_id"] = None
        self.state["phone_call_active"] = False
        await self.broadcast("outbound_cancelled", {})
        await self.broadcast("call_cancelled", {})
        await self.broadcast("state_update", self.get_current_state_dict())
        return web.json_response({"cancelled": cancelled})

    async def handle_get_dial_status(self, request):
        outbound = dialer.get_active_outbound()
        return web.json_response({"outbound": outbound})

    # =========================================================================
    # Knowledge Base API Handlers
    # =========================================================================

    async def handle_get_knowledge_docs(self, request):
        docs = km.get_all_documents()
        summary = km.get_knowledge_summary()
        return web.json_response({"documents": docs, "summary": summary})

    async def handle_post_knowledge_upload(self, request):
        try:
            reader = await request.multipart()
            uploaded_docs = []
            while True:
                part = await reader.next()
                if part is None:
                    break
                if part.filename:
                    filename = part.filename
                    content = await part.read()
                    if content:
                        doc = km.add_document(content, filename)
                        uploaded_docs.append(doc)
            summary = km.get_knowledge_summary()
            await self.broadcast("knowledge_updated", {"summary": summary})
            return web.json_response({
                "success": True,
                "uploaded": uploaded_docs,
                "summary": summary
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_delete_knowledge_doc(self, request):
        doc_id = request.match_info.get("doc_id")
        success = km.delete_document(doc_id)
        summary = km.get_knowledge_summary()
        await self.broadcast("knowledge_updated", {"summary": summary})
        return web.json_response({
            "success": success,
            "doc_id": doc_id,
            "summary": summary
        })

    async def handle_toggle_knowledge_doc(self, request):
        doc_id = request.match_info.get("doc_id")
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        enabled = body.get("enabled") if "enabled" in body else None
        new_state = km.toggle_document(doc_id, enabled)
        summary = km.get_knowledge_summary()
        await self.broadcast("knowledge_updated", {"summary": summary})
        return web.json_response({
            "success": new_state is not None,
            "doc_id": doc_id,
            "enabled": new_state,
            "summary": summary
        })

    async def handle_get_knowledge_preview(self, request):
        doc_id = request.match_info.get("doc_id")
        doc = km.get_document_full(doc_id)
        if not doc:
            return web.Response(status=404, text="Document not found")
        response_data = dict(doc)
        response_data["document"] = doc
        response_data["success"] = True
        return web.json_response(response_data)

    async def handle_get_knowledge_summary(self, request):
        summary = km.get_knowledge_summary()
        settings = load_settings()
        base_inst = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
        lang = settings.get("language", "en")
        full_inst = km.get_effective_system_instruction(base_inst, language=lang)
        prompt = km.build_knowledge_prompt()
        return web.json_response({
            "summary": summary,
            "prompt": prompt,
            "effective_instruction": full_inst,
            "language": lang
        })

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
            for k in ["voice_name", "language", "system_instruction", "vad_threshold", "barge_in_grace_period", "auto_launch_browser"]:
                if k in body:
                    update_data[k] = body[k]
            save_settings(update_data)
            return web.json_response({"success": True, "settings": load_settings()})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_post_shutdown(self, request):
        async def _delayed_kill():
            await asyncio.sleep(0.5)
            os._exit(0)
        asyncio.create_task(_delayed_kill())
        return web.json_response({"success": True, "message": "Callink Gateway is stopping..."})

    async def handle_get_logs(self, request):
        with terminal_lock:
            logs = list(terminal_logs)
        return web.json_response({"logs": logs})

    async def handle_clear_logs(self, request):
        with terminal_lock:
            terminal_logs.clear()
        await self.broadcast("terminal_logs_cleared", {})
        return web.json_response({"success": True})

    async def broadcast_terminal_logs(self, entries: list):
        if not self.ws_clients:
            return
        payload = json.dumps({"type": "terminal_logs", "entries": entries})
        stale_clients = []
        for client in list(self.ws_clients):
            try:
                await client.send_str(payload)
            except Exception:
                stale_clients.append(client)
        for client in stale_clients:
            self.ws_clients.discard(client)

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

        # Send initial terminal logs to new connection
        try:
            with terminal_lock:
                initial_logs = list(terminal_logs)
            if initial_logs:
                await ws.send_json({
                    "type": "init_terminal_logs",
                    "entries": initial_logs
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
        if message_type in ("transcript", "transcript_turn_complete") and data.get("text"):
            self.state.setdefault("live_transcripts", []).append({
                "role": data.get("role", "model"),
                "text": data.get("text", ""),
                "timestamp": time.time()
            })
        elif message_type == "call_started":
            self.state["live_transcripts"] = []

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
        now = time.time()
        if self.ws_clients and (now - getattr(self, "_last_levels_broadcast", 0.0) >= 0.08):
            self._last_levels_broadcast = now
            msg = json.dumps({
                "type": "audio_levels",
                "data": {"caller_rms": int(caller_rms), "ai_rms": int(ai_rms)},
                "timestamp": now
            })
            for client in list(self.ws_clients):
                try:
                    asyncio.create_task(client.send_str(msg))
                except Exception:
                    pass

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
        lang = settings.get("language", "en")
        base_inst = settings.get("system_instruction", DEFAULT_SETTINGS["system_instruction"])
        system_instruction = km.get_effective_system_instruction(base_inst, language=lang)

        # Check if running in outbound simulation mode
        is_outbound = request.query.get("outbound") == "1"
        outbound_info = None
        if is_outbound:
            contact_name = request.query.get("name", "").strip() or "the customer"
            call_goal = request.query.get("goal", "").strip() or "follow up with the customer and answer any questions"
            lead_notes = request.query.get("notes", "").strip()
            phone_num = request.query.get("phone", "").strip()
            outbound_info = {
                "contact_name": contact_name,
                "call_goal": call_goal,
                "lead_notes": lead_notes,
                "phone_number": phone_num,
                "display_number": phone_num,
            }

        call_type = "outbound_browser" if is_outbound else "browser"

        # Create browser recorder
        recorder = CallRecorder(call_type=call_type)
        if is_outbound and outbound_info:
            recorder.extra_metadata = {
                "direction": "outbound",
                "outbound_info": outbound_info,
            }
        recorder.start()

        # Update server state
        self.state["status"] = "in_call"
        self.state["call_type"] = call_type
        self.state["call_id"] = recorder.call_id
        self.state["call_start_time"] = time.time()
        self.state["live_transcripts"] = []
        await self.broadcast("call_started", {"call_id": recorder.call_id, "call_type": call_type, "outbound": outbound_info})

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

        stop_call_event = asyncio.Event()

        try:
            async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
                await ws.send_json({"type": "connected", "call_id": recorder.call_id})

                # Initial warm greeting or proactive outbound dispatch
                try:
                    if is_outbound and outbound_info:
                        greeting_text = dialer.build_outbound_proactive_prompt(outbound_info)
                    else:
                        greeting_text = (
                            "[A user has initiated an audio call with you in the browser. "
                            "Greet them warmly and concisely in one short sentence.]"
                        )
                    await session.send_client_content(
                        turns=types.Content(
                            role="user",
                            parts=[
                                types.Part(text=greeting_text)
                            ],
                        ),
                        turn_complete=True,
                    )
                except Exception as e:
                    print(f"[Browser Call Greeting Error]: {e}")

                current_caller_text = ""
                current_ai_text = ""

                async def browser_receiver():
                    """Receives Gemini Live audio and streams back to browser WebSocket."""
                    nonlocal current_caller_text, current_ai_text
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
                                    if current_ai_text:
                                        ai_interrupted = current_ai_text.strip()
                                        if ai_interrupted:
                                            recorder.add_transcript("model", ai_interrupted + " [interrupted]")
                                            await ws.send_json({"type": "transcript_turn_complete", "role": "model", "text": ai_interrupted + " [interrupted]"})
                                            await self.broadcast("transcript_turn_complete", {"role": "model", "text": ai_interrupted + " [interrupted]"})
                                        current_ai_text = ""
                                    await ws.send_json({"type": "interrupted"})
                                    await self.broadcast("barge_in", {"call_id": recorder.call_id})
                                    continue

                                # 1. Caller real-time speech transcription
                                if server_content.input_transcription and server_content.input_transcription.text:
                                    in_text = server_content.input_transcription.text
                                    current_caller_text += in_text
                                    await ws.send_json({"type": "transcript_chunk", "role": "caller", "text": in_text})
                                    await self.broadcast("transcript_chunk", {"role": "caller", "text": in_text})

                                # 2. Gemini AI speech synthesis transcription
                                if server_content.output_transcription and server_content.output_transcription.text:
                                    out_text = server_content.output_transcription.text
                                    if current_caller_text:
                                        final_caller = current_caller_text.strip()
                                        if final_caller:
                                            recorder.add_transcript("caller", final_caller)
                                            await ws.send_json({"type": "transcript_turn_complete", "role": "caller", "text": final_caller})
                                            await self.broadcast("transcript_turn_complete", {"role": "caller", "text": final_caller})
                                        current_caller_text = ""

                                    current_ai_text += out_text
                                    await ws.send_json({"type": "transcript_chunk", "role": "model", "text": out_text})
                                    await self.broadcast("transcript_chunk", {"role": "model", "text": out_text})

                                if server_content.model_turn:
                                    for part in server_content.model_turn.parts:
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
                                    if current_caller_text:
                                        final_caller = current_caller_text.strip()
                                        if final_caller:
                                            recorder.add_transcript("caller", final_caller)
                                            await ws.send_json({"type": "transcript_turn_complete", "role": "caller", "text": final_caller})
                                            await self.broadcast("transcript_turn_complete", {"role": "caller", "text": final_caller})
                                        current_caller_text = ""
                                    if current_ai_text:
                                        final_ai = current_ai_text.strip()
                                        if final_ai:
                                            recorder.add_transcript("model", final_ai)
                                            await ws.send_json({"type": "transcript_turn_complete", "role": "model", "text": final_ai})
                                            await self.broadcast("transcript_turn_complete", {"role": "model", "text": final_ai})
                                        current_ai_text = ""
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
        self.loop = asyncio.get_running_loop()
        global _global_web_server
        _global_web_server = self

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
