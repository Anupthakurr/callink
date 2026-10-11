import sys
sys.coinit_flags = 0

from winrt.windows.applicationmodel.calls import PhoneCallManager
import sounddevice as sd

apis = sd.query_hostapis()
print("Host APIs:")
for idx, a in enumerate(apis):
    print(f"  [{idx}] {a['name']}")

print("\nMatching devices:")
for i, d in enumerate(sd.query_devices()):
    if "hands-free" in d["name"].lower():
        api_name = apis[d["hostapi"]]["name"]
        print(f"[{i}] api={api_name} in={d['max_input_channels']} out={d['max_output_channels']} : {d['name']}")
