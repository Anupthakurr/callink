import ctypes
import threading
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

def prevent_sleep():
    """Instructs Windows to prevent entering sleep/standby while the gateway is active."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(
            ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
        )
    except Exception:
        pass


def start_stay_awake_heartbeat():
    """Starts a continuous daemon thread that refreshes Windows stay-awake state every 15s."""
    def heartbeat_worker():
        while True:
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
                )
            except Exception:
                pass
            time.sleep(15)

    thread = threading.Thread(target=heartbeat_worker, daemon=True, name="StayAwakeHeartbeat")
    thread.start()


def allow_sleep():
    """Restores default Windows sleep/standby behavior."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass
