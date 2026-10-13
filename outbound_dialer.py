"""
Callink — Outbound Cellular Telephony & Proactive Voice Agent Manager
Handles outbound dialing via Windows Phone Link (tel: protocol) and ADB,
manages outbound session state, and crafts proactive AI opening prompts.
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

# Expiration window: if an outbound call was dialed more than 90s ago
# and hasn't connected, expire it so normal incoming logic resumes.
OUTBOUND_EXPIRY_SECONDS = 90.0


def clean_phone_number(raw_number: str) -> str:
    """Strips formatting characters while preserving valid phone number characters (+, digits)."""
    if not raw_number:
        return ""
    # Keep leading + if present, and all digits
    raw = raw_number.strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"[^\d]", "", raw)
    return ("+" + digits) if has_plus else digits


def format_e164_number(clean_number: str) -> str:
    """
    Ensures the number has an international country code (E.164 format)
    so Microsoft Phone Link can properly parse and dial it.
    Defaults to Indian mobile (+91) if a 10-digit number is provided.
    """
    if not clean_number:
        return ""
    if clean_number.startswith("+"):
        return clean_number

    digits = re.sub(r"[^\d]", "", clean_number)
    # 10 digits starting with 6-9 (standard Indian mobile number)
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    # 11 digits starting with 0 followed by 6-9
    if len(digits) == 11 and digits.startswith("0") and digits[1] in "6789":
        return f"+91{digits[1:]}"
    # 10-digit number fallback
    if len(digits) == 10:
        return f"+91{digits}"
    return clean_number


def _auto_press_dial(phone_number: str):
    """
    Background automation helper that brings Phone Link to focus,
    locates the on-screen green 'Call' / 'Dial' button at (84.5%, 83.5%),
    and automatically triggers the click and Enter key.
    Exits as soon as the call connects.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    # Attach thread to interactive desktop if running in non-standard window station
    try:
        hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x10000000)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

    def _is_call_active():
        try:
            import winrt.windows.applicationmodel.calls as calls_winrt
            return calls_winrt.PhoneCallManager.get_is_call_active()
        except Exception:
            return False

    def _find_phone_link():
        # Check WinUI 3 desktop class (Windows 11 Phone Link)
        h = user32.FindWindowW("WinUIDesktopWin32WindowClass", "Phone Link")
        if h:
            return h
        # Check title only
        h = user32.FindWindowW(None, "Phone Link")
        if h:
            return h
        # Check UWP class (Windows 10 Your Phone)
        h = user32.FindWindowW("ApplicationFrameWindow", "Phone Link")
        if h:
            return h
        # Fallback window title scan
        found = [None]
        def _enum_cb(hwnd, lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                val = buf.value.lower()
                if "phone link" in val or "your phone" in val:
                    found[0] = hwnd
                    return False
            return True
        cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum_cb)
        user32.EnumWindows(cb, 0)
        return found[0]

    # Initial brief pause for Phone Link UI to render the dial pad
    time.sleep(0.6)

    clicked_count = 0
    # Poll for up to 8 seconds (20 iterations x 0.4s)
    for _ in range(20):
        if _is_call_active():
            print("[Outbound Dialer] Cellular call active detected! Auto-dialer disengaging.")
            return

        hwnd = _find_phone_link()
        if hwnd:
            # Restore window if minimized
            SW_RESTORE = 9
            SW_SHOW = 5
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.ShowWindow(hwnd, SW_SHOW)

            # Bypass foreground lock via Alt key press
            VK_MENU = 0x12
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
            user32.SetFocus(hwnd)

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            # Only proceed if window is restored and has normal dimensions
            if w > 200 and h > 200:
                # Keypad green Call button center: (84.5% X, 83.5% Y)
                click_x = rect.left + int(w * 0.845)
                click_y = rect.top + int(h * 0.835)

                # Save original mouse position
                orig_pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(orig_pt))

                # Hardware cursor move & left click
                MOUSEEVENTF_LEFTDOWN = 0x0002
                MOUSEEVENTF_LEFTUP = 0x0004
                user32.SetCursorPos(click_x, click_y)
                time.sleep(0.04)
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                time.sleep(0.04)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

                # Send Enter key as secondary trigger in case keypad field is focused
                VK_RETURN = 0x0D
                user32.keybd_event(VK_RETURN, 0, 0, 0)
                time.sleep(0.03)
                user32.keybd_event(VK_RETURN, 0, 2, 0)

                # Restore original cursor position immediately
                time.sleep(0.03)
                user32.SetCursorPos(orig_pt.x, orig_pt.y)

                clicked_count += 1
                print(f"[Outbound Dialer] Auto-clicked Call button at ({click_x}, {click_y}) [attempt {clicked_count}].")

                # If clicked at least twice, pause longer to check if connected
                if clicked_count >= 3:
                    time.sleep(1.0)
                    if _is_call_active():
                        return

        time.sleep(0.4)


def trigger_system_dialer(clean_number: str) -> bool:
    """
    Triggers Microsoft Phone Link specifically (bypassing PC VoIP software like MicroSIP,
    Skype, or Zoom that hijack the tel: protocol) and automatically enters and dials the number.
    """
    if not clean_number:
        return False

    e164_number = format_e164_number(clean_number)

    # 1. Attempt Android ADB direct dial if adb device is connected
    try:
        adb_check = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=1.0
        )
        if adb_check.returncode == 0 and "device\n" in adb_check.stdout:
            # Android device connected via ADB! Execute zero-click cellular dial
            subprocess.Popen([
                "adb", "shell", "am", "start",
                "-a", "android.intent.action.CALL",
                "-d", f"tel:{e164_number}"
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"[Outbound Dialer] Placed automated cellular call to {e164_number} via Android ADB.")
            return True
    except Exception:
        pass

    # 2. Copy phone number to Windows Clipboard so user can also paste instantly if desired
    try:
        subprocess.run(["clip.exe"], input=clean_number.encode(), check=False)
    except Exception:
        pass

    # 3. Attempt direct WinRT cellular dial via PhoneLine API
    try:
        import winrt.windows.applicationmodel.calls as calls_winrt
        import asyncio
        import concurrent.futures

        def _direct_winrt_dial(num: str):
            async def _coro():
                try:
                    store = await calls_winrt.PhoneCallManager.request_store_async()
                    line_id = await store.get_default_line_async()
                    if line_id:
                        line = await calls_winrt.PhoneLine.from_id_async(line_id)
                        if line and line.can_dial:
                            line.dial(num, "Customer")
                            return True
                except Exception as ex:
                    print(f"[Outbound Dialer] WinRT direct line exception: {ex}")
                return False

            return asyncio.run(_coro())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            direct_dialed = pool.submit(_direct_winrt_dial, e164_number).result(timeout=2.0)
        if direct_dialed:
            print(f"[Outbound Dialer] Dispatched cellular call to {e164_number} directly via WinRT PhoneLine.")
    except Exception as e:
        print(f"[Outbound Dialer] Direct WinRT line check: {e}")

    # 4. Launch auto-dial helper in background thread to navigate Calls tab & press Enter/ButtonCall
    threading.Thread(target=_auto_press_dial, args=(clean_number,), daemon=True).start()

    # 5. Specifically target Microsoft Phone Link via Windows Runtime (WinRT) LauncherOptions
    # Pinning target_application_package_family_name tells Windows to route the call
    # exclusively to Microsoft Phone Link, completely bypassing MicroSIP, Skype, or Zoom.
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
                ok = await sys_winrt.Launcher.launch_uri_async(uri, options)
                return ok

            return asyncio.run(_coro())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            phone_link_ok = pool.submit(_launch_targeted, e164_number).result(timeout=3.0)

        if phone_link_ok:
            print(f"[Outbound Dialer] Dispatched {e164_number} directly to Microsoft Phone Link.")
            return True
    except Exception as e:
        print(f"[Outbound Dialer] WinRT Phone Link targeting: {e}")

    # 6. Dedicated Phone Link URI protocol (ms-phone: protocol unique to Phone Link)
    try:
        os.startfile(f"ms-phone:{e164_number}")
        print(f"[Outbound Dialer] Dispatched via ms-phone URI for {e164_number}.")
        return True
    except Exception:
        pass

    # 7. Fallback to generic system telephony protocol
    try:
        tel_url = f"tel:{e164_number}"
        try:
            os.startfile(tel_url)
        except Exception:
            webbrowser.open(tel_url)
        print(f"[Outbound Dialer] Triggered system telephony handler for {e164_number}.")
        return True
    except Exception as e:
        print(f"[Outbound Dialer Error] Failed to open telephony handler: {e}")
        return False



def start_outbound_dial(
    phone_number: str,
    contact_name: str = "",
    call_goal: str = "",
    lead_notes: str = ""
) -> Dict[str, Any]:
    """
    Registers an active outbound dialing intent and triggers the telephony dialer.
    """
    clean_num = clean_phone_number(phone_number)
    if not clean_num:
        raise ValueError("Invalid phone number provided.")

    name = contact_name.strip() or "the customer"
    goal = call_goal.strip() or "follow up with the customer and answer any questions"
    notes = lead_notes.strip()

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


def cancel_outbound_dial() -> bool:
    """Cancels any pending outbound call state."""
    global _active_outbound
    with _lock:
        if _active_outbound:
            _active_outbound = None
            print("[Outbound Dialer] Outbound dialing cancelled by user.")
            return True
    return False


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
    """
    Atomically retrieves and consumes the outbound call state when the call connects.
    Prevents subsequent calls from mistakenly inheriting outbound properties.
    """
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


def build_outbound_proactive_prompt(outbound_info: Dict[str, Any]) -> str:
    """
    Constructs the initial directive instructing Gemini Live to speak first
    immediately with a proactive greeting, purpose statement, and business knowledge context.
    """
    name = outbound_info.get("contact_name", "the customer")
    goal = outbound_info.get("call_goal", "follow up and assist with their inquiry")
    notes = outbound_info.get("lead_notes", "")
    phone = outbound_info.get("display_number") or outbound_info.get("phone_number", "")

    notes_section = f"\nLead Context / Prior Notes: {notes}" if notes else ""

    return (
        f"[OUTBOUND CALL ENGAGEMENT DIRECTIVE]\n"
        f"You are placing an OUTBOUND telephonic phone call to {name} (Number: {phone}).\n"
        f"Primary Call Objective: {goal}.{notes_section}\n\n"
        f"CRITICAL INSTRUCTION: The recipient has just answered the phone. YOU MUST SPEAK FIRST!\n"
        f"Deliver a warm, professional, and natural opening line (1-2 short sentences):\n"
        f"1. Greet them politely by name (e.g., 'Hello {name}' or 'Hi there').\n"
        f"2. Introduce yourself and your company clearly.\n"
        f"3. State the purpose of your call concisely and check if now is a convenient time to speak.\n"
        f"Speak with confidence and high energy suitable for an outbound representative."
    )
