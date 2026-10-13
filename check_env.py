import sys
sys.coinit_flags = 0

import sounddevice as sd

apis = sd.query_hostapis()
print("Host APIs:")
for idx, a in enumerate(apis):
    print(f"  [{idx}] {a['name']}")

print("\nAll devices:")
for i, d in enumerate(sd.query_devices()):
    api_name = apis[d["hostapi"]]["name"]
    print(f"[{i:2d}] api={api_name:<18} in={d['max_input_channels']} out={d['max_output_channels']} : {d['name']}")
