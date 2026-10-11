"""
Standalone test script for Gemini Live API audio pipeline.
Sends a sample WAV file to Gemini Live and plays/saves the response.
"""

import asyncio
import audioop
import os
import sys
import time
import wave
import numpy as np

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from google import genai
from google.genai import types
import sounddevice as sd

GEMINI_MODEL = "gemini-3.1-flash-live-preview"
GEMINI_IN_RATE = 16000
GEMINI_OUT_RATE = 24000

async def run_standalone_test(wav_path="sample.wav", play_audio=True):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY environment variable is not set!")
        sys.exit(1)

    if not os.path.exists(wav_path):
        print(f"ERROR: Audio file '{wav_path}' not found!")
        sys.exit(1)

    print(f"Reading input audio from '{wav_path}'...")
    with wave.open(wav_path, "rb") as wf:
        in_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        in_rate = wf.getframerate()
        raw_audio = wf.readframes(wf.getnframes())

    print(f"Input file format: {in_rate} Hz, {in_channels} channel(s), {sampwidth * 8}-bit")

    # Convert to mono if needed
    if in_channels > 1:
        print("Converting stereo to mono...")
        raw_audio = audioop.tomono(raw_audio, sampwidth, 0.5, 0.5)

    # Convert sample width to 16-bit if needed
    if sampwidth != 2:
        print("Converting to 16-bit...")
        raw_audio = audioop.lin2lin(raw_audio, sampwidth, 2)

    # Resample to 16kHz if needed
    if in_rate != GEMINI_IN_RATE:
        print(f"Resampling from {in_rate} Hz to {GEMINI_IN_RATE} Hz...")
        audio_16k, _ = audioop.ratecv(raw_audio, 2, 1, in_rate, GEMINI_IN_RATE, None)
    else:
        audio_16k = raw_audio

    duration_sec = len(audio_16k) / (GEMINI_IN_RATE * 2)
    print(f"Audio ready: {len(audio_16k)} bytes (~{duration_sec:.2f} seconds)")

    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=(
            "You are a phone agent answering calls for the business. "
            "Keep responses short and conversational, as in a real phone call."
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
    )

    print(f"\nConnecting to Gemini Live with model '{GEMINI_MODEL}'...")
    async with client.aio.live.connect(model=GEMINI_MODEL, config=config) as session:
        print("Connected to Gemini Live session!")

        received_pcm_24k = bytearray()
        received_text = []
        turn_completed = asyncio.Event()
        first_audio_time = None
        send_start_time = None

        async def receiver():
            nonlocal first_audio_time
            try:
                async for response in session.receive():
                    server_content = response.server_content
                    if server_content is None:
                        continue

                    if server_content.interrupted:
                        print("[RECEIVER] Interrupted flag received from Gemini Live!")

                    if server_content.model_turn:
                        for part in server_content.model_turn.parts:
                            if part.text:
                                print(f"[GEMINI TEXT]: {part.text}")
                                received_text.append(part.text)
                            if part.inline_data and part.inline_data.data:
                                if first_audio_time is None and send_start_time is not None:
                                    first_audio_time = time.time()
                                    latency = first_audio_time - send_start_time
                                    print(f"--> First audio packet received in {latency:.2f}s latency!")
                                received_pcm_24k.extend(part.inline_data.data)

                    if server_content.turn_complete:
                        print("[RECEIVER] Turn complete signaled by server.")
                        turn_completed.set()
                        break

            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[RECEIVER ERROR]: {e}")

        recv_task = asyncio.create_task(receiver())

        print("Streaming audio to Gemini Live in 20ms chunks...")
        send_start_time = time.time()
        chunk_size = 640  # 20ms @ 16kHz 16-bit mono
        for i in range(0, len(audio_16k), chunk_size):
            chunk = audio_16k[i : i + chunk_size]
            await session.send_realtime_input(
                audio=types.Blob(
                    data=chunk,
                    mime_type=f"audio/pcm;rate={GEMINI_IN_RATE}",
                )
            )
            await asyncio.sleep(0.02)  # real-time pace

        print("Finished sending all audio chunks.")
        print("Waiting for model response to complete...")

        try:
            await asyncio.wait_for(turn_completed.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            print("Warning: Timed out waiting for turn_complete.")

        recv_task.cancel()
        await asyncio.gather(recv_task, return_exceptions=True)

        print(f"\nReceived {len(received_pcm_24k)} bytes of 24kHz audio from Gemini Live.")

        if received_pcm_24k:
            out_filename = "test_output_24k.wav"
            with wave.open(out_filename, "wb") as out_wf:
                out_wf.setnchannels(1)
                out_wf.setsampwidth(2)
                out_wf.setframerate(GEMINI_OUT_RATE)
                out_wf.writeframes(received_pcm_24k)
            print(f"Saved response audio to '{out_filename}'.")

            if play_audio:
                print("Playing back response through default audio output...")
                data = np.frombuffer(received_pcm_24k, dtype=np.int16)
                sd.play(data, samplerate=GEMINI_OUT_RATE)
                sd.wait()
                print("Playback finished.")
            return True
        else:
            print("No audio received from Gemini Live.")
            return False

if __name__ == "__main__":
    test_wav = sys.argv[1] if len(sys.argv) > 1 else "sample.wav"
    asyncio.run(run_standalone_test(test_wav, play_audio=True))
