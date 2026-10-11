import sys
sys.coinit_flags = 0

import asyncio
import audioop
import os
import queue
import time
from dotenv import load_dotenv

from winrt.windows.applicationmodel.calls import PhoneCallManager
import sounddevice as sd
from google import genai
from google.genai import types

load_dotenv()

def find_hfp():
    inp, outp = None, None
    for i, d in enumerate(sd.query_devices()):
        if "hands-free hf audio" in d["name"].lower():
            if d["max_input_channels"] > 0 and inp is None:
                inp = i
            if d["max_output_channels"] > 0 and outp is None:
                outp = i
    return inp, outp

async def main():
    inp, outp = find_hfp()
    print(f"HFP Input: {inp}, Output: {outp}")
    print(f"PhoneCallManager.get_is_call_active(): {PhoneCallManager.get_is_call_active()}")

    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="You are a helpful phone assistant. Say hello immediately when the user speaks.",
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
    )

    audio_q = queue.Queue()

    def in_cb(indata, frames, time_info, status):
        raw = indata[:, 0].tobytes()
        audio_q.put_nowait(raw)

    print("Connecting to Gemini Live...")
    async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
        print("Connected to Gemini Live!")

        # Try opening HFP mic if call is active
        stream = None
        if PhoneCallManager.get_is_call_active():
            print("Opening HFP mic stream...")
            stream = sd.InputStream(device=inp, samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=in_cb)
            stream.start()
            print("HFP mic stream started!")
        else:
            print("No cellular call active right now. Testing Gemini Live receiver for 10s...")

        stop = asyncio.Event()

        async def recv():
            try:
                print("[RECV] Listening for messages from Gemini...")
                async for resp in session.receive():
                    print(f"[RECV MSG]: {resp}")
                    if resp.server_content and resp.server_content.model_turn:
                        for p in resp.server_content.model_turn.parts:
                            if p.text:
                                print(f"[AI TEXT]: {p.text}")
                            if p.inline_data:
                                print(f"[AI AUDIO]: {len(p.inline_data.data)} bytes")
            except Exception as e:
                print(f"[RECV EXCEPTION]: {type(e).__name__}: {e}")
                stop.set()

        async def send():
            chunks_sent = 0
            while not stop.is_set():
                try:
                    raw = audio_q.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue

                rms = audioop.rms(raw, 2)
                if not any(raw):
                    continue

                chunks_sent += 1
                if chunks_sent % 50 == 0:
                    print(f"[SEND] Sent {chunks_sent} chunks to Gemini (latest RMS={rms})")

                try:
                    await session.send_realtime_input(audio=types.Blob(data=raw, mime_type="audio/pcm;rate=16000"))
                except Exception as e:
                    print(f"[SEND EXCEPTION]: {type(e).__name__}: {e}")
                    stop.set()
                    break

        recv_task = asyncio.create_task(recv())
        send_task = asyncio.create_task(send())

        # Run for up to 15 seconds
        for _ in range(15):
            if stop.is_set():
                break
            await asyncio.sleep(1)

        stop.set()
        if stream:
            stream.stop()
            stream.close()
        recv_task.cancel()
        send_task.cancel()

    print("Test finished.")

if __name__ == "__main__":
    asyncio.run(main())
