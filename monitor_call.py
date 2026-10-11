import sys
sys.coinit_flags = 0

import time
from winrt.windows.applicationmodel.calls import PhoneCallManager

print("Monitoring PhoneCallManager states for 30 seconds...")
print("Make or receive a call now to see exact state transitions.\n")

prev_active = None
prev_incoming = None

for i in range(60):
    try:
        active = PhoneCallManager.get_is_call_active()
        incoming = PhoneCallManager.get_is_call_incoming()
        if active != prev_active or incoming != prev_incoming:
            print(f"[{time.strftime('%H:%M:%S')}] is_call_active={active}, is_call_incoming={incoming}")
            prev_active = active
            prev_incoming = incoming
    except Exception as e:
        print(f"[{time.strftime('%H:%M:%S')}] Error checking state: {e}")
    time.sleep(0.5)

print("Monitoring ended.")
