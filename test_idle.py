import asyncio
import os
import time
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

async def test_idle():
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction="You are a phone agent."
    )
    print("Connecting...")
    async with client.aio.live.connect(model="gemini-3.1-flash-live-preview", config=config) as session:
        print(f"Connected at {time.strftime('%H:%M:%S')}")
        
        async def receiver():
            try:
                async for resp in session.receive():
                    print(f"[{time.strftime('%H:%M:%S')}] Server msg: {resp}")
            except Exception as e:
                print(f"[{time.strftime('%H:%M:%S')}] Receiver exited: {type(e)} {e}")

        recv_task = asyncio.create_task(receiver())
        for i in range(25):
            await asyncio.sleep(1)
            print(f"[{time.strftime('%H:%M:%S')}] Elapsed {i+1}s...")
        recv_task.cancel()

if __name__ == "__main__":
    asyncio.run(test_idle())
