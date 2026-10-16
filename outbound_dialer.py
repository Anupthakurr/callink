"""
Callink — Outbound Cellular Telephony & Proactive Voice Agent Manager
Handles clean outbound dialing via standard Windows tel: protocol,
manages outbound session state, and crafts proactive AI opening prompts.
Zero invasive window manipulation, zero mouse hijacking, zero UIAutomation poking.
"""

import os
import re
import time
import threading
import subprocess
import webbrowser
from typing import Optional, Dict, Any

_lock = threading.Lock()
_active_outbound: Optional[Dict[str, Any]] = None
_user_cancelled: bool = False

OUTBOUND_EXPIRY_SECONDS = 90.0


def is_dial_cancelled() -> bool:
    """Checks if the user explicitly cancelled the outbound call from the dashboard."""
    global _user_cancelled
    with _lock:
        return _user_cancelled


def clear_outbound_cancelled():
    """Resets the cancellation flag when starting a new outbound dial."""
    global _user_cancelled
    with _lock:
        _user_cancelled = False


def clean_phone_number(raw_number: str) -> str:
    """Strips formatting characters while preserving valid phone number characters (+, digits)."""
    if not raw_number:
        return ""
    raw = raw_number.strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"[^\d]", "", raw)
    return ("+" + digits) if has_plus else digits


def format_e164_number(clean_number: str) -> str:
    """
    Ensures the number has an international country code (E.164 format).
    Defaults to Indian mobile (+91) if a 10-digit number is provided.
    """
    if not clean_number:
        return ""
    if clean_number.startswith("+"):
        return clean_number

    digits = re.sub(r"[^\d]", "", clean_number)
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return f"+91{digits[1:]}"
    if len(digits) == 10:
        return f"+91{digits}"
    return clean_number


def trigger_system_dialer(clean_number: str) -> bool:
    """
    Standard, clean Windows dialer.
    Directly opens the standard tel: URI protocol in Windows, naturally launching
    Microsoft Phone Link in the normal way without any window manipulation,
    mouse hijacking, or UI automation poking.
    """
    if not clean_number:
        return False

    e164_number = format_e164_number(clean_number)

    # 1. Copy number to Windows clipboard for convenience
    try:
        subprocess.run(["clip.exe"], input=clean_number.encode(), check=False)
    except Exception:
        pass

    # 2. Specifically target Microsoft Phone Link (bypasses MicroSIP, Skype, Zoom)
    try:
        import winrt.windows.system as sys_winrt
        import winrt.windows.foundation as fnd_winrt
        import asyncio
        import concurrent.futures

        def _launch_targeted(num: str):
            async def _coro():
                options = sys_winrt.LauncherOptions()
                options.target_application_package_family_name = "Microsoft.YourPhone_8wekyb3d8bbwe"
                uri = fnd_winrt.Uri(f"tel:{num}")
                return await sys_winrt.Launcher.launch_uri_async(uri, options)

            return asyncio.run(_coro())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            phone_link_ok = pool.submit(_launch_targeted, e164_number).result(timeout=3.0)

        if phone_link_ok:
            print(f"[Outbound Dialer] Dispatched {e164_number} directly to Microsoft Phone Link (bypassed MicroSIP).")
            return True
    except Exception as e:
        print(f"[Outbound Dialer] Targeted launch note: {e}")

    # 3. Fallback: Standard Windows tel: protocol
    try:
        os.startfile(f"tel:{e164_number}")
        print(f"[Outbound Dialer] Dispatched {e164_number} via standard Windows tel: protocol.")
        return True
    except Exception:
        pass

    try:
        webbrowser.open(f"tel:{e164_number}")
        print(f"[Outbound Dialer] Dispatched {e164_number} via browser tel: protocol.")
        return True
    except Exception as ex:
        print(f"[Outbound Dialer] Failed to launch tel: protocol: {ex}")
        return False


def start_outbound_dial(
    phone_number: str,
    contact_name: str = "",
    call_goal: str = "",
    lead_notes: str = ""
) -> Dict[str, Any]:
    """Registers an active outbound dialing intent and triggers the telephony dialer."""
    clean_num = clean_phone_number(phone_number)
    if not clean_num:
        raise ValueError("Invalid phone number provided.")

    name = contact_name.strip() or "the customer"
    goal = call_goal.strip() or "follow up with the customer and answer any questions"
    notes = lead_notes.strip()

    clear_outbound_cancelled()
    reset_transfer_state(force=True)

    global _active_outbound
    with _lock:
        _active_outbound = {
            "phone_number": clean_num,
            "display_number": phone_number.strip(),
            "contact_name": name,
            "call_goal": goal,
            "lead_notes": notes,
            "dialed_at": time.time(),
            "status": "dialing"
        }

    success = trigger_system_dialer(clean_num)
    with _lock:
        if _active_outbound:
            _active_outbound["dialer_triggered"] = success

    return dict(_active_outbound)


def cancel_outbound_dial(force_hangup: bool = False) -> bool:
    """Cancels any pending outbound call in state."""
    global _active_outbound, _user_cancelled
    with _lock:
        _user_cancelled = True
        had_outbound = _active_outbound is not None
        _active_outbound = None

    if not had_outbound and not force_hangup:
        return False

    print("[Outbound Dialer] Outbound dialing cancelled.")
    hangup_current_call()
    return True


def get_active_outbound() -> Optional[Dict[str, Any]]:
    """Returns the current active outbound state if still within expiry window."""
    global _active_outbound
    with _lock:
        if not _active_outbound:
            return None
        elapsed = time.time() - _active_outbound.get("dialed_at", 0)
        if elapsed > OUTBOUND_EXPIRY_SECONDS:
            _active_outbound = None
            return None
        return dict(_active_outbound)


def consume_active_outbound() -> Optional[Dict[str, Any]]:
    """Atomically retrieves and consumes the outbound call state when the call connects."""
    global _active_outbound
    with _lock:
        if not _active_outbound:
            return None
        elapsed = time.time() - _active_outbound.get("dialed_at", 0)
        if elapsed > OUTBOUND_EXPIRY_SECONDS:
            _active_outbound = None
            return None
        info = dict(_active_outbound)
        info["connected_at"] = time.time()
        _active_outbound = None
        return info


def build_outbound_context(outbound_info: Dict[str, Any], language: str = "auto") -> str:
    """Constructs comprehensive outbound context for system instruction priming."""
    name = outbound_info.get("contact_name", "the customer")
    goal = outbound_info.get("call_goal", "follow up and assist with their inquiry")
    notes = outbound_info.get("lead_notes", "")
    phone = outbound_info.get("display_number") or outbound_info.get("phone_number", "")
    notes_section = f"\n- Lead Context / Prior Notes: {notes}" if notes else ""
    return (
        f"\n\n[ACTIVE OUTBOUND CALL CONTEXT]\n"
        f"- Target Recipient: {name} (Number: {phone})\n"
        f"- Call Goal: {goal}{notes_section}\n"
        f"- Outbound Role Directive: You are placing an outbound call to {name}. When the call is answered, greet {name} warmly by name in 1-2 natural sentences, state why you are calling, and ask if now is a good time to speak."
    )


def build_outbound_proactive_prompt(outbound_info: Dict[str, Any], language: str = "auto") -> str:
    """Constructs an ultra-fast, minimal trigger instructing Gemini Live to speak first immediately."""
    name = outbound_info.get("contact_name", "the customer")
    lang_clean = (language or "auto").strip().lower()
    if lang_clean == "hi":
        return f"[कॉल कनेक्ट हो गया है। {name} को तुरंत नमस्ते कहें और 1-2 वाक्यों में बताएं कि आपने कॉल क्यों किया है।]"
    elif lang_clean == "mai":
        return f"[कॉल कनेक्ट भऽ गेल। {name} कें तुरंत प्रणाम करू आ 1-2 वाक्य मे कहू जे अहाँ फोन किएक कएने छी।]"
    else:
        return f"[The call is connected with {name}. Immediately speak your opening greeting now in 1-2 concise sentences.]"


# =============================================================================
# Clean Non-Manipulative Telephony Helpers (Using Windows WinRT Telephony)
# No Window Manipulation, No UIAutomation, No Mouse Cursor Hijacking
# =============================================================================

def is_phone_link_call_connected() -> bool:
    """Uses official Windows WinRT Telephony API instead of window scraping."""
    try:
        from winrt.windows.applicationmodel.calls import PhoneCallManager
        return PhoneCallManager.get_is_call_active()
    except Exception:
        return False


def is_phone_link_call_present() -> bool:
    """Uses official Windows WinRT Telephony API instead of window scraping."""
    try:
        from winrt.windows.applicationmodel.calls import PhoneCallManager
        return PhoneCallManager.get_is_call_active() or PhoneCallManager.get_is_call_incoming()
    except Exception:
        return False


def is_phone_link_call_ended() -> bool:
    """Uses official Windows WinRT Telephony API instead of window scraping."""
    try:
        from winrt.windows.applicationmodel.calls import PhoneCallManager
        return not (PhoneCallManager.get_is_call_active() or PhoneCallManager.get_is_call_incoming())
    except Exception:
        return False


def ensure_phone_link_visible():
    """No-op: Phone Link operates naturally as a standard Windows app without focus stealing."""
    pass


def hangup_current_call() -> bool:
    """Clean hangup: let user or Phone Link end call naturally."""
    return True


def is_call_transferred_to_pc() -> bool:
    return False


def is_transfer_clicked() -> bool:
    return False


def reset_transfer_state(force: bool = False):
    pass


def trigger_transfer_to_pc_async(timeout: float = 0.0):
    pass


def keep_phone_link_minimized(duration: float = 0.0, restore_hwnd: int = None):
    pass


def find_all_phone_link_windows() -> list:
    return []


def find_phone_link_window():
    return None
