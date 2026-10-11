import sys
sys.coinit_flags = 0

import audioop
import time
from winrt.windows.applicationmodel.calls import PhoneCallManager
import sounddevice as sd

def find_hfp_devices():
    devices = sd.query_devices()
    input_device, output_device = None, None
    for i, device in enumerate(devices):
        name = device["name"].lower()
        if "hands-free hf audio" not in name:
            continue
        if device["max_input_channels"] > 0 and input_device is None:
            input_device = i
        if device["max_output_channels"] > 0 and output_device is None:
            output_device = i
    return input_device, output_device

inp, outp = find_hfp_devices()
print(f"HFP Input: {inp}, Output: {outp}")
print("Waiting for cellular call to test microphone capture...")

while not PhoneCallManager.get_is_call_active():
    time.sleep(0.5)

print("\nCall active! Opening HFP input stream...")
chunk_count = 0
zero_count = 0
nonzero_count = 0
max_rms = 0

def in_cb(indata, frames, time_info, status):
    global chunk_count, zero_count, nonzero_count, max_rms
    raw = indata[:, 0].tobytes()
    chunk_count += 1
    rms = audioop.rms(raw, 2)
    if rms > max_rms:
        max_rms = rms
    if not any(raw):
        zero_count += 1
    else:
        nonzero_count += 1
    if chunk_count % 25 == 0:
        print(f"[{time.strftime('%H:%M:%S')}] Chunks: total={chunk_count}, zeroes={zero_count}, non-zero={nonzero_count}, latest_rms={rms}, max_rms={max_rms}")

stream = sd.InputStream(device=inp, samplerate=16000, channels=1, dtype="int16", blocksize=320, callback=in_cb)
stream.start()
print("Recording HFP mic for 10 seconds. Please speak into the phone!\n")

for _ in range(20):
    if not PhoneCallManager.get_is_call_active():
        print("Call ended early.")
        break
    time.sleep(0.5)

stream.stop()
stream.close()
print(f"\nFinal Stats: Chunks={chunk_count}, Zeroes={zero_count}, Non-zero={nonzero_count}, Max RMS={max_rms}")
