import os
import sys
import time
import json
import wave
import audioop
import numpy as np
from datetime import datetime
from typing import List, Dict, Optional, Any

if getattr(sys, "frozen", False):
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")
os.makedirs(RECORDINGS_DIR, exist_ok=True)


class CallRecorder:
    """
    Synchronized Dual-Track Audio Recorder for Phone & AI Conversations.
    Records Left Channel (Caller) and Right Channel (AI Assistant) at 16kHz 16-bit PCM.
    Also aggregates conversation metadata, transcripts, and event telemetry.
    """

    def __init__(self, call_type: str = "gsm", call_id: Optional[str] = None):
        self.call_type = call_type
        now = datetime.now()
        timestamp_str = now.strftime("%Y%m%d_%H%M%S")
        self.call_id = call_id or f"{call_type}_{timestamp_str}"
        self.wav_path = os.path.join(RECORDINGS_DIR, f"{self.call_id}.wav")
        self.json_path = os.path.join(RECORDINGS_DIR, f"{self.call_id}.json")

        self.start_time = time.time()
        self.end_time = None
        self.duration = 0.0

        self.sample_rate = 16000  # 16 kHz
        self.sample_width = 2     # 16-bit
        self.channels = 2         # Stereo: Left = Caller, Right = AI

        self.total_caller_frames = 0
        self.total_ai_frames = 0
        self.total_stereo_samples = 0

        self.wave_file: Optional[wave.Wave_write] = None
        self.transcripts: List[Dict[str, Any]] = []
        self.events: List[Dict[str, Any]] = []
        self.is_recording = False

        # Audio chunk buffer for interleaving
        self._pending_caller = bytearray()
        self._pending_ai = bytearray()

    def start(self):
        """Initializes and opens the WAV file for low-latency streaming writes."""
        try:
            self.wave_file = wave.open(self.wav_path, "wb")
            self.wave_file.setnchannels(self.channels)
            self.wave_file.setsampwidth(self.sample_width)
            self.wave_file.setframerate(self.sample_rate)
            self.is_recording = True
            self.add_event("call_started", {"call_type": self.call_type, "timestamp": self.start_time})
        except Exception as e:
            print(f"[Recorder Error] Failed to initialize wave file: {e}")
            self.is_recording = False

    def add_transcript(self, role: str, text: str):
        """Records a spoken transcript line with relative call offset."""
        if not text or not text.strip():
            return
        offset = round(time.time() - self.start_time, 2)
        entry = {
            "role": role,  # 'user' (caller) or 'model' (Gemini AI)
            "text": text.strip(),
            "offset_seconds": offset,
            "timestamp": datetime.now().isoformat()
        }
        self.transcripts.append(entry)

    def add_event(self, event_name: str, data: Optional[Dict[str, Any]] = None):
        """Records system events like barge-in interruptions or reconnection."""
        entry = {
            "event": event_name,
            "offset_seconds": round(time.time() - self.start_time, 2),
            "data": data or {}
        }
        self.events.append(entry)

    def write_stereo_block(self, caller_pcm_16k: bytes, ai_pcm_16k: Optional[bytes] = None):
        """
        Interleaves 16-bit mono caller audio with 16-bit mono AI audio into stereo frames
        and writes directly to disk.
        - Channel 0 (Left): Caller
        - Channel 1 (Right): AI (silence if None or empty)
        """
        if not self.is_recording or not self.wave_file:
            return

        sample_bytes = 2  # 16-bit PCM

        # Caller mono chunk
        num_caller_samples = len(caller_pcm_16k) // sample_bytes
        if num_caller_samples == 0:
            return

        caller_arr = np.frombuffer(caller_pcm_16k, dtype=np.int16)

        # AI mono chunk: match number of samples exactly
        if ai_pcm_16k and len(ai_pcm_16k) >= len(caller_pcm_16k):
            ai_arr = np.frombuffer(ai_pcm_16k[:len(caller_pcm_16k)], dtype=np.int16)
        elif ai_pcm_16k and len(ai_pcm_16k) > 0:
            ai_part = np.frombuffer(ai_pcm_16k, dtype=np.int16)
            ai_arr = np.zeros(num_caller_samples, dtype=np.int16)
            ai_arr[:len(ai_part)] = ai_part
        else:
            # Silence for AI channel during caller speech or idle turn
            ai_arr = np.zeros(num_caller_samples, dtype=np.int16)

        # Interleave: [caller0, ai0, caller1, ai1, ...]
        stereo_arr = np.empty((num_caller_samples * 2,), dtype=np.int16)
        stereo_arr[0::2] = caller_arr
        stereo_arr[1::2] = ai_arr

        try:
            self.wave_file.writeframes(stereo_arr.tobytes())
            self.total_stereo_samples += num_caller_samples
        except Exception as e:
            print(f"[Recorder Error] Failed writing frames: {e}")

    def finish(self) -> Dict[str, Any]:
        """Closes the WAV file, computes final metrics, and writes JSON metadata."""
        if not self.is_recording:
            return self.get_summary()

        self.is_recording = False
        self.end_time = time.time()
        self.duration = round(self.end_time - self.start_time, 2)

        if self.wave_file:
            try:
                self.wave_file.close()
            except Exception as e:
                print(f"[Recorder Error] Failed to close wave file: {e}")
            self.wave_file = None

        file_size_bytes = os.path.getsize(self.wav_path) if os.path.exists(self.wav_path) else 0

        metadata = {
            "call_id": self.call_id,
            "call_type": self.call_type,
            "wav_file": os.path.basename(self.wav_path),
            "start_time": datetime.fromtimestamp(self.start_time).isoformat(),
            "end_time": datetime.fromtimestamp(self.end_time).isoformat() if self.end_time else None,
            "duration_seconds": self.duration,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_mapping": {"channel_0": "Caller (Left)", "channel_1": "Gemini AI (Right)"},
            "file_size_bytes": file_size_bytes,
            "transcripts": self.transcripts,
            "events": self.events,
            "total_samples": self.total_stereo_samples,
        }

        try:
            with open(self.json_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            print(f"[Recorder] Call recorded successfully: {self.wav_path} ({self.duration}s)")
        except Exception as e:
            print(f"[Recorder Error] Failed writing metadata JSON: {e}")

        return metadata

    def get_summary(self) -> Dict[str, Any]:
        """Returns in-progress or completed summary."""
        dur = round((time.time() - self.start_time) if not self.end_time else self.duration, 2)
        return {
            "call_id": self.call_id,
            "call_type": self.call_type,
            "is_recording": self.is_recording,
            "duration_seconds": dur,
            "transcripts_count": len(self.transcripts),
            "wav_file": os.path.basename(self.wav_path)
        }


def get_all_recordings() -> List[Dict[str, Any]]:
    """Scans the recordings directory and returns a sorted list of all calls."""
    recordings = []
    if not os.path.exists(RECORDINGS_DIR):
        return recordings

    for filename in os.listdir(RECORDINGS_DIR):
        if filename.endswith(".json"):
            json_file = os.path.join(RECORDINGS_DIR, filename)
            try:
                with open(json_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                wav_file = os.path.join(RECORDINGS_DIR, data.get("wav_file", ""))
                if os.path.exists(wav_file):
                    data["file_size_bytes"] = os.path.getsize(wav_file)
                recordings.append(data)
            except Exception:
                continue

    # Sort descending by start_time
    recordings.sort(key=lambda x: x.get("start_time", ""), reverse=True)
    return recordings


def delete_recording(call_id: str) -> bool:
    """Removes the WAV and JSON files for a given call ID."""
    wav_path = os.path.join(RECORDINGS_DIR, f"{call_id}.wav")
    json_path = os.path.join(RECORDINGS_DIR, f"{call_id}.json")
    deleted = False

    if os.path.exists(wav_path):
        try:
            os.remove(wav_path)
            deleted = True
        except Exception:
            pass

    if os.path.exists(json_path):
        try:
            os.remove(json_path)
            deleted = True
        except Exception:
            pass

    return deleted
