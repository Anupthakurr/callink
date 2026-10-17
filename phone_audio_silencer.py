import threading
import time

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


def set_phone_link_audio_silenced(silenced: bool = True):
    """Silences Microsoft Phone Link (PhoneExperienceHost) on BOTH PC speakers and PC microphone
    without spamming COM calls, completely eliminating audio leaks, clicking, and beep artifacts."""
    try:
        import comtypes
        com_initialized = False
        try:
            comtypes.CoInitialize()
            com_initialized = True
        except Exception:
            pass

        try:
            from pycaw.pycaw import AudioUtilities, IAudioSessionManager2, ISimpleAudioVolume, IAudioSessionControl2
            from comtypes import CLSCTX_ALL
            import psutil

            # 1. Silence Speaker Output (Render)
            for s in AudioUtilities.GetAllSessions():
                p = s.Process
                if p and "phoneexperiencehost" in p.name().lower():
                    vol = s.SimpleAudioVolume
                    target_mute = 1 if silenced else 0
                    if vol.GetMute() != target_mute:
                        vol.SetMute(target_mute, None)
                    if silenced and vol.GetMasterVolume() > 0.0:
                        vol.SetMasterVolume(0.0, None)
                    elif not silenced and vol.GetMasterVolume() == 0.0:
                        vol.SetMasterVolume(1.0, None)

            # 2. Silence Microphone Input (Capture)
            try:
                mic = AudioUtilities.GetMicrophone()
                if mic:
                    mgr = mic.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None)
                    enumerator = mgr.QueryInterface(IAudioSessionManager2).GetSessionEnumerator()
                    for i in range(enumerator.GetCount()):
                        ctl = enumerator.GetSession(i)
                        ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                        pid = ctl2.GetProcessId()
                        if pid > 0:
                            try:
                                proc_name = psutil.Process(pid).name().lower()
                            except Exception:
                                proc_name = ""
                            if "phoneexperiencehost" in proc_name:
                                vol = ctl.QueryInterface(ISimpleAudioVolume)
                                target_mute = 1 if silenced else 0
                                if vol.GetMute() != target_mute:
                                    vol.SetMute(target_mute, None)
                                if silenced and vol.GetMasterVolume() > 0.0:
                                    vol.SetMasterVolume(0.0, None)
                                elif not silenced and vol.GetMasterVolume() == 0.0:
                                    vol.SetMasterVolume(1.0, None)
            except Exception:
                pass
        finally:
            if com_initialized:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
    except Exception:
        pass


class PhoneLinkAudioSilencer:
    """Watchdog thread that ensures Phone Link's speaker & mic stay muted without redundant calls."""
    def __init__(self):
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="PhoneLinkSilencer")
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            set_phone_link_audio_silenced(True)
            time.sleep(1.0)

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        set_phone_link_audio_silenced(False)
