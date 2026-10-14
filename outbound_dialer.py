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
import ctypes
from ctypes import wintypes
from typing import Optional, Dict, Any

_lock = threading.Lock()
_active_outbound: Optional[Dict[str, Any]] = None
_user_cancelled: bool = False

# Expiration window: if an outbound call was dialed more than 90s ago
# and hasn't connected, expire it so normal incoming logic resumes.
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


def _safe_coinit() -> bool:
    """Safely initializes COM apartment without throwing RPC_E_CHANGED_MODE on MTA threads."""
    try:
        import comtypes
        comtypes.CoInitialize()
        return True
    except Exception:
        return False


def _safe_couninit(did_init: bool):
    """Safely uninitializes COM apartment only if this scope initialized it."""
    if did_init:
        try:
            import comtypes
            comtypes.CoUninitialize()
        except Exception:
            pass


def find_all_phone_link_windows() -> list:
    """Locates all visible Microsoft Phone Link and active in-call popup window handles."""
    import ctypes
    from ctypes import wintypes
    import os
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    hdesk = None
    try:
        hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x10000000)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

    def _get_proc(h):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if not pid.value:
            return ""
        hProc = kernel32.OpenProcess(0x1000, False, pid.value)
        if not hProc:
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        kernel32.QueryFullProcessImageNameW(hProc, 0, buf, ctypes.byref(size))
        kernel32.CloseHandle(hProc)
        return os.path.basename(buf.value).lower()

    found_hwnds = []

    def _enum_cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pname = _get_proc(hwnd)
        if pname in ["phoneexperiencehost.exe", "yourphone.exe", "applicationframehost.exe"]:
            if hwnd not in found_hwnds:
                found_hwnds.append(hwnd)
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            val = buf.value.lower()
            if any(k in val for k in ["phone link", "your phone", "call on pc", "call on mobile", "calls"]):
                if hwnd not in found_hwnds:
                    found_hwnds.append(hwnd)
        return True

    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum_cb)
    if hdesk:
        user32.EnumDesktopWindows(hdesk, cb, 0)
    user32.EnumWindows(cb, 0)
    return found_hwnds


def find_phone_link_window():
    """Locates the primary active Microsoft Phone Link window handle, prioritizing in-call popups."""
    hwnds = find_all_phone_link_windows()
    if not hwnds:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32

    # Prioritize active in-call popup window
    # 1. By title substring if present
    for h in hwnds:
        length = user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            val = buf.value.lower()
            if "call on" in val or "calls" in val:
                return h

    # 2. In-call popup is a compact floating window (typically width < 650 and height < 500)
    rect = wintypes.RECT()
    for h in hwnds:
        if user32.GetWindowRect(h, ctypes.byref(rect)):
            w = rect.right - rect.left
            height = rect.bottom - rect.top
            if 200 < w < 650 and 120 < height < 500:
                return h

    return hwnds[0]


def ensure_phone_link_visible():
    """Ensures Microsoft Phone Link is opened and active on screen so in-call controls and popup appear."""
    import ctypes
    user32 = ctypes.windll.user32
    h = find_phone_link_window()
    if h and user32.IsWindowVisible(h):
        user32.ShowWindow(h, 9)  # SW_RESTORE
        try:
            user32.SetForegroundWindow(h)
            user32.SetWindowPos(h, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # HWND_TOPMOST, SWP_SHOWWINDOW
            user32.SetWindowPos(h, -2, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)  # HWND_NOTOPMOST
        except Exception:
            pass
        return

    import subprocess
    try:
        subprocess.Popen(["explorer.exe", r"shell:AppsFolder\Microsoft.YourPhone_8wekyb3d8bbwe!App"], shell=False)
    except Exception:
        pass


_transfer_lock = threading.Lock()
_last_transfer_action_time = 0.0
_audio_transferred_to_pc = False
_transfer_clicked = False


def is_call_transferred_to_pc() -> bool:
    """Returns True if call audio is confirmed to be routed to PC."""
    global _audio_transferred_to_pc
    return _audio_transferred_to_pc


def is_transfer_clicked() -> bool:
    """Returns True if an automated or manual transfer action was executed during this call."""
    global _transfer_clicked
    return _transfer_clicked


def reset_transfer_state(force: bool = False):
    """Resets audio transfer state tracking for a new cellular call."""
    global _last_transfer_action_time, _audio_transferred_to_pc, _transfer_clicked
    with _transfer_lock:
        now = time.time()
        # Cooldown guard: Never reset if a transfer occurred less than 15s ago unless forced
        if not force and _last_transfer_action_time > 0 and (now - _last_transfer_action_time < 15.0):
            print(f"[Audio Router] Guarding active transfer state ({now - _last_transfer_action_time:.1f}s since transfer). Reset skipped.")
            return
        _last_transfer_action_time = 0.0
        _audio_transferred_to_pc = False
        _transfer_clicked = False


def get_last_transfer_time() -> float:
    """Returns timestamp of the last transfer attempt or confirmation."""
    return _last_transfer_action_time


def ensure_call_transferred_to_pc(timeout: float = 60.0) -> bool:
    """
    Monitors Phone Link during an active or outgoing cellular call and ensures
    call audio is routed to PC (switching the button to 'Transfer to phone' or 'Send call to mobile device').
    If the window or button already indicates PC routing ('Call on PC', 'Transfer to phone'),
    it marks success and NEVER clicks.
    If 'Transfer to PC' is detected, it triggers the transfer at most ONCE per call to prevent
    any toggle loops or fighting with the user/system.
    """
    global _last_transfer_action_time, _audio_transferred_to_pc, _transfer_clicked

    # Prevent concurrent running transfer jobs
    if not _transfer_lock.acquire(blocking=False):
        return _audio_transferred_to_pc

    try:
        now = time.time()
        # Hard guards: If already confirmed on PC or already clicked, never click again during this call
        if _audio_transferred_to_pc:
            return True
        if _transfer_clicked:
            return True
        if _last_transfer_action_time > 0 and (now - _last_transfer_action_time < 20.0):
            return True

        import ctypes
        from ctypes import wintypes
        import os

        ensure_phone_link_visible()

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        def _attach_desktop():
            try:
                hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x10000000)
                if hwinsta:
                    user32.SetProcessWindowStation(hwinsta)
                hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
                if hdesk:
                    user32.SetThreadDesktop(hdesk)
                return hdesk
            except Exception:
                return None

        start_time = time.time()
        while time.time() - start_time < timeout:
            _attach_desktop()

            # Re-check guards on every loop iteration
            if _audio_transferred_to_pc or _transfer_clicked:
                return True
            if _last_transfer_action_time > 0 and (time.time() - _last_transfer_action_time < 20.0):
                return True

            # Find all visible Phone Link and active call windows
            candidate_hwnds = find_all_phone_link_windows()

            # 1. Immediate Window Title Check
            for hwnd in candidate_hwnds:
                if user32.IsWindowVisible(hwnd):
                    length = user32.GetWindowTextLengthW(hwnd)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(hwnd, buf, length + 1)
                        title_val = buf.value.lower()
                        if any(k in title_val for k in ["call on pc", "on pc", "call on this pc"]):
                            print(f"[Audio Router] Phone Link window title confirms call audio is already routed to PC ('{buf.value}').")
                            _audio_transferred_to_pc = True
                            _transfer_clicked = True
                            _last_transfer_action_time = time.time()
                            return True

            # 2. UI Automation Inspection
            try:
                import comtypes
                import comtypes.client
                from comtypes.gen.UIAutomationClient import (
                    IUIAutomation, TreeScope_Descendants,
                    UIA_ButtonControlTypeId, UIA_TextControlTypeId, UIA_ControlTypePropertyId,
                    UIA_InvokePatternId, IUIAutomationInvokePattern,
                    UIA_TogglePatternId, IUIAutomationTogglePattern
                )

                co_ok = _safe_coinit()
                try:
                    uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
                    btn_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
                    txt_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_TextControlTypeId)

                    for hwnd in candidate_hwnds:
                        try:
                            el = uia.ElementFromHandle(hwnd)
                            if not el:
                                continue

                            # Check window-level text controls for 'Call on PC'
                            try:
                                win_texts = el.FindAll(TreeScope_Descendants, txt_cond)
                                if win_texts:
                                    for wt_idx in range(win_texts.Length):
                                        wt = win_texts.GetElement(wt_idx)
                                        wt_name = (wt.CurrentName or "").strip().lower()
                                        if any(k in wt_name for k in ["call on pc", "on pc", "call on this pc", "pc audio"]):
                                            print(f"[Audio Router] Window text confirms call is already routed to PC ('{wt_name}').")
                                            _audio_transferred_to_pc = True
                                            _transfer_clicked = True
                                            _last_transfer_action_time = time.time()
                                            return True
                            except Exception:
                                pass

                            btns = el.FindAll(TreeScope_Descendants, btn_cond)
                            if not btns or btns.Length == 0:
                                continue

                            for i in range(btns.Length):
                                b = btns.GetElement(i)
                                name = (b.CurrentName or "").strip()
                                aid = (b.CurrentAutomationId or "").strip()
                                help_txt = (b.CurrentHelpText or "").strip()
                                name_l = name.lower()
                                aid_l = aid.lower()
                                help_l = help_txt.lower()

                                # Reject non-transfer buttons (end, mute, dialpad, bluetooth, device selection, etc.)
                                reject_keywords = [
                                    "end", "hang", "decline", "cancel", "mute", "mic",
                                    "keypad", "dialpad", "hold", "add", "swap", "dismiss",
                                    "close", "bluetooth", "device selection", "more options",
                                    "expand", "collapse", "pane", "menu"
                                ]
                                if any(f in name_l for f in reject_keywords):
                                    continue
                                if any(f in aid_l for f in reject_keywords):
                                    continue

                                # Collect text from any inner child elements
                                child_texts = []
                                try:
                                    child_els = b.FindAll(TreeScope_Descendants, txt_cond)
                                    if child_els:
                                        for c_i in range(child_els.Length):
                                            c_name = (child_els.GetElement(c_i).CurrentName or "").strip().lower()
                                            if c_name:
                                                child_texts.append(c_name)
                                except Exception:
                                    pass

                                all_text = " ".join([name_l, help_l] + child_texts).strip()

                                # (A) ALREADY ON PC check:
                                # When audio is routed to PC, Phone Link button offers to transfer back to mobile/phone!
                                is_on_pc = False
                                if any(k in all_text for k in [
                                    "transfer to phone", "transfer call to phone",
                                    "send call to mobile", "send to phone", "send to mobile",
                                    "transfer to mobile", "transfer call to mobile",
                                    "to phone", "to mobile", "use phone", "use mobile",
                                    "call on pc", "on pc"
                                ]):
                                    is_on_pc = True

                                # Check TogglePattern only if the button is specifically transfer/PC/phone related
                                if not is_on_pc and any(k in all_text for k in ["transfer", "pc", "phone", "mobile"]):
                                    try:
                                        tog_p = b.GetCurrentPattern(UIA_TogglePatternId)
                                        if tog_p:
                                            tog = tog_p.QueryInterface(IUIAutomationTogglePattern)
                                            if tog.CurrentToggleState == 1:
                                                is_on_pc = True
                                    except Exception:
                                        pass

                                if is_on_pc:
                                    print(f"[Audio Router] Call audio is already routed to PC (button indicates: '{all_text}').")
                                    _audio_transferred_to_pc = True
                                    _transfer_clicked = True
                                    _last_transfer_action_time = time.time()
                                    return True

                                # (B) TRANSFER TO PC check:
                                # Target button to transfer audio from phone to PC
                                is_transfer_btn = False
                                if any(p in all_text for p in [
                                    "transfer to pc", "transfer call to pc", "send call to pc",
                                    "send to pc", "to pc", "switch to pc"
                                ]) and not any(m in all_text for m in ["to phone", "to mobile", "on pc"]):
                                    is_transfer_btn = True

                                if is_transfer_btn:
                                    # Hard guard: If already confirmed on PC, never click again!
                                    if _audio_transferred_to_pc:
                                        return True

                                    # Check if the button is enabled (not mid-transition)
                                    try:
                                        if not b.CurrentIsEnabled:
                                            continue
                                    except Exception:
                                        pass

                                    print(f"[Audio Router] Detected 'Transfer to PC' button (Name='{name}', AutomationId='{aid}', Text='{all_text}'). Transferring to PC...")

                                    # 1. Hardware mouse click directly to button center
                                    clicked = False
                                    try:
                                        rect = b.CurrentBoundingRectangle
                                        bw = rect.right - rect.left
                                        bh = rect.bottom - rect.top
                                        if bw > 0 and bh > 0 and rect.left >= 0 and rect.top >= 0:
                                            cx = rect.left + bw // 2
                                            cy = rect.top + bh // 2
                                            orig_pt = wintypes.POINT()
                                            user32.GetCursorPos(ctypes.byref(orig_pt))
                                            user32.SetCursorPos(cx, cy)
                                            time.sleep(0.05)
                                            user32.mouse_event(0x0002, 0, 0, 0, 0)
                                            time.sleep(0.05)
                                            user32.mouse_event(0x0004, 0, 0, 0, 0)
                                            time.sleep(0.05)
                                            user32.SetCursorPos(orig_pt.x, orig_pt.y)
                                            clicked = True
                                            print(f"[Audio Router] Hardware mouse click dispatched to Transfer button at ({cx}, {cy}).")
                                    except Exception as ex:
                                        print(f"[Audio Router] Mouse click notice: {ex}")

                                    # 2. Also invoke UI Automation pattern as secondary trigger
                                    try:
                                        pattern = b.GetCurrentPattern(UIA_InvokePatternId)
                                        if pattern:
                                            invoker = pattern.QueryInterface(IUIAutomationInvokePattern)
                                            invoker.Invoke()
                                    except Exception:
                                        pass

                                    _transfer_clicked = True
                                    _last_transfer_action_time = time.time()
                                    time.sleep(1.2)
                        except Exception:
                            pass
                finally:
                    _safe_couninit(co_ok)
            except Exception:
                pass

            time.sleep(0.3)

        return _audio_transferred_to_pc
    finally:
        _transfer_lock.release()


def is_phone_link_call_connected() -> bool:
    """
    Checks if Microsoft Phone Link currently displays an in-call session with
    active duration timer (e.g. 00:01, 00:05), or in-call banner/controls without 'Calling...' text,
    or title 'Call on PC'.
    Returns True if recipient has picked up/call is connected, False if ringing or idle.
    """
    hwnds = find_all_phone_link_windows()
    if not hwnds:
        return False

    import ctypes
    user32 = ctypes.windll.user32

    # 1. Immediate Title Check
    for h in hwnds:
        length = user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            title = buf.value.lower()
            if "call on pc" in title:
                return True

    # 2. UI Automation Inspection across all Phone Link windows
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen.UIAutomationClient import (
            IUIAutomation, TreeScope_Descendants,
            UIA_TextControlTypeId, UIA_ButtonControlTypeId,
            UIA_ControlTypePropertyId
        )
        import re

        co_ok = _safe_coinit()
        try:
            uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
            btn_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
            txt_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_TextControlTypeId)

            for hwnd in hwnds:
                try:
                    el = uia.ElementFromHandle(hwnd)
                    if not el:
                        continue

                    # Check text elements first: active banner or duration timer proves connected!
                    texts = el.FindAll(TreeScope_Descendants, txt_cond)
                    is_ringing = False
                    has_timer = False
                    has_incall_banner = False

                    if texts:
                        for i in range(texts.Length):
                            t = texts.GetElement(i)
                            name = (t.CurrentName or "").strip().lower()
                            if any(c in name for c in ["calling", "dialing", "connecting"]):
                                is_ringing = True
                            if "call on pc" in name:
                                return True
                            if any(k in name for k in ["currently in a call", "in a call on", "on your mobile device"]):
                                has_incall_banner = True
                            # Look for active call duration timer (e.g. 0:01, 00:01, 01:01, 12:45)
                            m = re.match(r"^\d{1,2}:\d{2}$", name)
                            if m:
                                parts = name.split(":")
                                sec = int(parts[0]) * 60 + int(parts[1])
                                if sec > 0:
                                    has_timer = True

                    if has_timer:
                        return True
                    if has_incall_banner and not is_ringing:
                        return True

                    # Check for in-call buttons
                    btns = el.FindAll(TreeScope_Descendants, btn_cond)
                    has_incall_btn = False
                    if btns:
                        for i in range(btns.Length):
                            b = btns.GetElement(i)
                            bid = (b.CurrentAutomationId or "").strip()
                            bname = (b.CurrentName or "").strip().lower()
                            if bid in ["MiniEndButton", "KeypadEndCallButton", "EndCallButton", "ButtonEndCall"] or "end call" in bname:
                                has_incall_btn = True
                                break
                            if any(k in bname for k in ["transfer to pc", "transfer to phone"]):
                                return True

                    if has_incall_btn and not is_ringing:
                        return True
                except Exception:
                    pass

            return False
        finally:
            _safe_couninit(co_ok)
    except Exception:
        return False


def is_phone_link_call_present() -> bool:
    """
    Checks if Microsoft Phone Link currently displays an active call session
    (either dialing, ringing, or connected, indicated by call controls, in-call banner,
    or window titled 'Call on PC' / 'Call on mobile device').
    Returns True if an active cellular call session exists in Phone Link.
    """
    hwnds = find_all_phone_link_windows()
    if not hwnds:
        return False

    import ctypes
    user32 = ctypes.windll.user32

    # 1. Quick Title Check: "Call on PC" or "Call on mobile" indicates active call
    for h in hwnds:
        length = user32.GetWindowTextLengthW(h)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)
            title = buf.value.lower()
            if any(k in title for k in ["call on pc", "call on mobile", "call on"]):
                return True

    # 2. UI Automation Inspection across all Phone Link windows
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen.UIAutomationClient import (
            IUIAutomation, TreeScope_Descendants,
            UIA_ButtonControlTypeId, UIA_TextControlTypeId, UIA_ControlTypePropertyId
        )

        co_ok = _safe_coinit()
        try:
            uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
            btn_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
            txt_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_TextControlTypeId)
            for hwnd in hwnds:
                try:
                    el = uia.ElementFromHandle(hwnd)
                    if not el:
                        continue

                    # Check text elements for active in-call banner or timer
                    texts = el.FindAll(TreeScope_Descendants, txt_cond)
                    if texts:
                        for i in range(texts.Length):
                            t = texts.GetElement(i)
                            tname = (t.CurrentName or "").strip().lower()
                            if any(k in tname for k in [
                                "currently in a call", "in a call on", "on your mobile device",
                                "call on pc", "call on mobile", "transfer to pc", "transfer to phone"
                            ]):
                                return True
                            import re
                            if re.match(r"^\d{1,2}:\d{2}$", tname):
                                return True

                    # Check buttons
                    btns = el.FindAll(TreeScope_Descendants, btn_cond)
                    if btns:
                        for i in range(btns.Length):
                            b = btns.GetElement(i)
                            bid = (b.CurrentAutomationId or "").strip()
                            bname = (b.CurrentName or "").strip().lower()
                            if bid in ["MiniEndButton", "KeypadEndCallButton", "EndCallButton", "ButtonEndCall"] or "end call" in bname:
                                return True
                            if any(k in bname for k in ["transfer to pc", "transfer to phone", "to pc", "to phone"]):
                                return True
                except Exception:
                    pass
            return False
        finally:
            _safe_couninit(co_ok)
    except Exception:
        return False


def trigger_transfer_to_pc_async(timeout: float = 60.0):
    """Launches ensure_call_transferred_to_pc in a daemon background thread."""
    threading.Thread(target=ensure_call_transferred_to_pc, args=(timeout,), daemon=True).start()


def keep_phone_link_minimized(duration: float = 8.0, restore_hwnd: int = None):
    """
    Background daemon that prevents Microsoft Phone Link from stealing focus
    from the Callink web dashboard, while keeping Phone Link active so its
    in-call audio controls, call presence, and PC audio transfer function properly.
    """
    def _worker():
        import ctypes
        from ctypes import wintypes
        import psutil

        user32 = ctypes.windll.user32
        target_restore = restore_hwnd
        if not target_restore:
            try:
                target_restore = user32.GetForegroundWindow()
            except Exception:
                target_restore = None

        end_time = time.time() + duration
        while time.time() < end_time:
            try:
                curr_fg = user32.GetForegroundWindow()
                if curr_fg and target_restore and curr_fg != target_restore:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(curr_fg, ctypes.byref(pid))
                    try:
                        pname = psutil.Process(pid.value).name().lower()
                    except Exception:
                        pname = ""
                    if "phone" in pname:
                        user32.SetForegroundWindow(target_restore)
            except Exception:
                pass
            time.sleep(0.2)

    threading.Thread(target=_worker, daemon=True).start()


def _auto_press_dial(phone_number: str):
    """
    Background automation helper that locates the on-screen green 'Call' / 'Dial'
    button (ButtonCall), automatically triggers the call while keeping Phone Link
    minimized in the background without stealing user focus.
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

    _find_phone_link = find_phone_link_window

    # Initial brief pause for Phone Link UI to render the dial pad
    time.sleep(0.5)

    # Launch asynchronous audio transfer watcher early so as soon as in-call banner appears, audio transfers to PC
    trigger_transfer_to_pc_async(timeout=15.0)

    clicked_count = 0
    # Poll for up to 8 seconds (20 iterations x 0.4s)
    for _ in range(20):
        hwnd = _find_phone_link()
        if hwnd:
            # 1. Check if Phone Link already has in-call buttons (call is already dialing or answered)
            call_invoked = False
            has_incall_button = False
            try:
                import comtypes
                import comtypes.client
                from comtypes.gen.UIAutomationClient import (
                    IUIAutomation, TreeScope_Descendants,
                    UIA_ButtonControlTypeId, UIA_ControlTypePropertyId,
                    UIA_InvokePatternId, IUIAutomationInvokePattern
                )
                co_ok = _safe_coinit()
                try:
                    uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
                    el = uia.ElementFromHandle(hwnd)
                    if el:
                        cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
                        btns = el.FindAll(TreeScope_Descendants, cond)
                        if btns:
                            for i in range(btns.Length):
                                b = btns.GetElement(i)
                                bid = (b.CurrentAutomationId or "").strip()
                                bname = (b.CurrentName or "").strip().lower()

                                # Check if in-call controls already exist
                                if bid in ["MiniEndButton", "KeypadEndCallButton", "EndCallButton", "ButtonEndCall"] or "end call" in bname:
                                    has_incall_button = True
                                    break

                                if bid == "ButtonCall" or "call dialled number" in bname:
                                    p = b.GetCurrentPattern(UIA_InvokePatternId)
                                    if p:
                                        inv = p.QueryInterface(IUIAutomationInvokePattern)
                                        inv.Invoke()
                                        call_invoked = True
                                        print("[Outbound Dialer] Invoked ButtonCall directly via UIAutomation!")
                                        break
                finally:
                    _safe_couninit(co_ok)
            except Exception:
                pass

            if has_incall_button:
                print("[Outbound Dialer] Cellular in-call controls active in Phone Link. Dialer auto-disengaging.")
                trigger_transfer_to_pc_async(timeout=10.0)
                return

            if call_invoked:
                trigger_transfer_to_pc_async(timeout=12.0)
                print("[Outbound Dialer] Call triggered via UIAutomation. Dialer auto-disengaging.")
                return

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            # 2. Window coordinate click fallback: Keypad green Call button center (84.5% X, 83.5% Y)
            # Only attempt once if ButtonCall was not directly invokable and no in-call buttons exist
            if w > 200 and h > 200 and clicked_count == 0:
                orig_fg = user32.GetForegroundWindow()

                SW_SHOWNOACTIVATE = 4
                user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)

                click_x = rect.left + int(w * 0.845)
                click_y = rect.top + int(h * 0.835)

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



                # Restore original cursor position and previous foreground window
                time.sleep(0.03)
                user32.SetCursorPos(orig_pt.x, orig_pt.y)
                if orig_fg and orig_fg != hwnd:
                    user32.SetForegroundWindow(orig_fg)

                clicked_count += 1
                print(f"[Outbound Dialer] Dispatched Call trigger at ({click_x}, {click_y}) [attempt {clicked_count}].")
                trigger_transfer_to_pc_async(timeout=12.0)

        time.sleep(0.4)


def trigger_system_dialer(clean_number: str) -> bool:
    """
    Triggers Microsoft Phone Link specifically (bypassing PC VoIP software like MicroSIP,
    Skype, or Zoom that hijack the tel: protocol) and automatically enters and dials the number.
    Ensures Phone Link stays minimized in the background without popping up or stealing focus.
    """
    if not clean_number:
        return False

    e164_number = format_e164_number(clean_number)

    # Capture current foreground window (e.g. browser showing Callink dashboard)
    orig_fg = None
    try:
        user32 = ctypes.windll.user32
        orig_fg = user32.GetForegroundWindow()
    except Exception:
        pass

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

    # 3. Attempt direct WinRT cellular dial via PhoneLine API (Silent background dial)
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
            direct_dialed = pool.submit(_direct_winrt_dial, e164_number).result(timeout=2.5)
        if direct_dialed:
            print(f"[Outbound Dialer] Dispatched cellular call to {e164_number} directly via WinRT PhoneLine.")
            ensure_phone_link_visible()
            # Trigger audio transfer to PC watcher in background
            trigger_transfer_to_pc_async(timeout=20.0)
            return True
    except Exception as e:
        print(f"[Outbound Dialer] Direct WinRT line check: {e}")

    # 4. Fallback path: Launch auto-dial helper in background thread to navigate Calls tab & press Enter/ButtonCall
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
                try:
                    import winrt.windows.ui.viewmanagement as vm
                    options.desired_remaining_view = vm.ViewSizePreference.USE_NONE
                except Exception:
                    pass
                uri = fnd_winrt.Uri(f"tel:{num}")
                ok = await sys_winrt.Launcher.launch_uri_async(uri, options)
                return ok

            return asyncio.run(_coro())

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            phone_link_ok = pool.submit(_launch_targeted, e164_number).result(timeout=3.0)

        if phone_link_ok:
            print(f"[Outbound Dialer] Dispatched {e164_number} directly to Microsoft Phone Link.")
            trigger_transfer_to_pc_async(timeout=20.0)
            return True
    except Exception as e:
        print(f"[Outbound Dialer] WinRT Phone Link targeting: {e}")

    # 6. Fallback: Ensure Phone Link is active and let _auto_press_dial handle the dial
    ensure_phone_link_visible()
    print(f"[Outbound Dialer] Brought Phone Link to foreground for automated dial of {e164_number}.")
    trigger_transfer_to_pc_async(timeout=20.0)
    return True



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


def hangup_current_call() -> bool:
    """
    Terminates / cancels any active or dialing cellular call across Windows Phone Link
    and connected telephony subsystems by sending the official HFP hangup command.
    """
    import ctypes
    from ctypes import wintypes
    import psutil

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Save original foreground window so we can restore user focus immediately
    orig_fg = None
    try:
        orig_fg = user32.GetForegroundWindow()
    except Exception:
        pass

    # 1. Attach thread to interactive desktop session (WinSta0\Default)
    hdesk = None
    try:
        hwinsta = user32.OpenWindowStationW("WinSta0", False, 0x10000000)
        if hwinsta:
            user32.SetProcessWindowStation(hwinsta)
        hdesk = user32.OpenDesktopW("Default", 0, False, 0x10000000)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

    hung_up = False

    # 2. Collect all candidate window handles (Phone Experience Host app & call windows)
    phone_pids = []
    try:
        phone_pids = [
            p.info['pid'] for p in psutil.process_iter(['name', 'pid'])
            if 'phoneexperiencehost' in (p.info['name'] or '').lower()
        ]
    except Exception:
        pass

    ignored_classes = [
        "ime", "msctfime ui", "gdi+ hook window class",
        ".net-broadcasteventwindow", "cicerouiwendframe", "cicerouiwndframe",
        "pyinstalleronefilehiddenwindow"
    ]

    candidate_hwnds = []
    def _enum_candidates(h, lp):
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls_buf, 256)
        cls = cls_buf.value.lower()
        if any(ic in cls for ic in ignored_classes):
            return True

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pid.value in phone_pids:
            if h not in candidate_hwnds:
                candidate_hwnds.append(h)
        else:
            length = user32.GetWindowTextLengthW(h)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(h, buf, length + 1)
                title = buf.value.lower()

                if (
                    ("phone link" in title or "your phone" in title or "call" in title or "+" in title)
                    and "edge" not in title and "chrome" not in title and "ide" not in title and "cursor" not in title
                ) or ("winuidesktop" in cls and ("phone" in title or "call" in title or title == "")):
                    if h not in candidate_hwnds:
                        candidate_hwnds.append(h)
        return True

    cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_enum_candidates)
    if hdesk:
        user32.EnumDesktopWindows(hdesk, cb, 0)
    user32.EnumWindows(cb, 0)

    # Also include the explicit Phone Link window if found
    try:
        pl_h = find_phone_link_window()
        if pl_h and pl_h not in candidate_hwnds:
            candidate_hwnds.append(pl_h)
    except Exception:
        pass

    print(f"[Outbound Dialer] Found {len(candidate_hwnds)} candidate windows to disconnect.")

    # 3. UI Automation: Inspect each candidate window using ElementFromHandle for End Call buttons
    try:
        import comtypes
        import comtypes.client
        from comtypes.gen.UIAutomationClient import (
            IUIAutomation, TreeScope_Descendants,
            UIA_ButtonControlTypeId, UIA_ControlTypePropertyId,
            UIA_InvokePatternId, IUIAutomationInvokePattern
        )

        co_ok = _safe_coinit()
        try:
            uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
            btn_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)

            for hwnd in candidate_hwnds:
                try:
                    was_iconic = user32.IsIconic(hwnd)
                    if was_iconic:
                        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                        time.sleep(0.04)

                    el = uia.ElementFromHandle(hwnd)
                    if not el:
                        if was_iconic:
                            user32.ShowWindow(hwnd, 6)
                        continue

                    btns = el.FindAll(TreeScope_Descendants, btn_cond)
                    if not btns or btns.Length == 0:
                        if was_iconic:
                            user32.ShowWindow(hwnd, 6)
                        continue

                    for i in range(btns.Length):
                        b = btns.GetElement(i)
                        name = (b.CurrentName or "").strip().lower()
                        aid = (b.CurrentAutomationId or "").strip()

                        is_end = False
                        if aid in [
                            "MiniEndButton", "KeypadEndCallButton", "EndCallButton",
                            "EndButton", "ButtonEndCall", "DeclineCall",
                            "CloseWindowButton", "DismissButton"
                        ]:
                            is_end = True
                        elif name in ["end call", "end", "hang up", "decline", "cancel call", "disconnect"]:
                            is_end = True
                        elif "end call" in name or "hang up" in name or (name == "decline"):
                            is_end = True

                        if is_end:
                            # 1. InvokePattern
                            try:
                                pattern = b.GetCurrentPattern(UIA_InvokePatternId)
                                if pattern:
                                    invoker = pattern.QueryInterface(IUIAutomationInvokePattern)
                                    invoker.Invoke()
                                    hung_up = True
                                    print(f"[Outbound Dialer] Invoked End Call button '{aid}' ('{name}') via UIAutomation on HWND {hwnd}.")
                            except Exception as ex:
                                print(f"[Outbound Dialer] Invoke pattern error: {ex}")

                            # 2. Hardware mouse click fallback
                            try:
                                rect = b.CurrentBoundingRectangle
                                bw = rect.right - rect.left
                                bh = rect.bottom - rect.top
                                if bw > 0 and bh > 0:
                                    cx = rect.left + bw // 2
                                    cy = rect.top + bh // 2
                                    orig_pt = wintypes.POINT()
                                    user32.GetCursorPos(ctypes.byref(orig_pt))
                                    user32.SetCursorPos(cx, cy)
                                    time.sleep(0.04)
                                    user32.mouse_event(0x0002, 0, 0, 0, 0)
                                    time.sleep(0.04)
                                    user32.mouse_event(0x0004, 0, 0, 0, 0)
                                    user32.SetCursorPos(orig_pt.x, orig_pt.y)
                                    hung_up = True
                                    print(f"[Outbound Dialer] Hardware clicked End Call button '{aid}' at ({cx}, {cy}).")
                            except Exception:
                                pass
                            break

                    if was_iconic:
                        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
                    if hung_up:
                        break
                except Exception:
                    pass
        finally:
            _safe_couninit(co_ok)
    except Exception as ex:
        print(f"[Outbound Dialer] UIAutomation hangup note: {ex}")

    # 4. Trigger WM_CLOSE and SC_CLOSE on Phone Link call windows (triggers endCallsOnWindowClosed)
    for hwnd in candidate_hwnds:
        try:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in phone_pids:
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                cls = cls_buf.value.lower()
                if any(ic in cls for ic in ignored_classes):
                    continue

                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)

                # Post WM_CLOSE and SC_CLOSE
                user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
                user32.PostMessageW(hwnd, 0x0112, 0xF060, 0)  # WM_SYSCOMMAND, SC_CLOSE
                print(f"[Outbound Dialer] Sent WM_CLOSE / SC_CLOSE to Phone Link window {hwnd} ('{buf.value}')")
        except Exception:
            pass

    # 5. Bring Phone Link window to focus briefly to process Escape / Alt+F4
    pl_h = find_phone_link_window()
    if pl_h:
        try:
            user32.ShowWindow(pl_h, 9)  # SW_RESTORE
            user32.SetForegroundWindow(pl_h)
            time.sleep(0.02)
            # Escape
            user32.keybd_event(0x1B, 0, 0, 0)
            time.sleep(0.02)
            user32.keybd_event(0x1B, 0, 2, 0)
            # Alt+F4
            user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU (Alt) down
            user32.keybd_event(0x73, 0, 0, 0)  # VK_F4 down
            time.sleep(0.02)
            user32.keybd_event(0x73, 0, 2, 0)  # VK_F4 up
            user32.keybd_event(0x12, 0, 2, 0)  # VK_MENU up
        except Exception:
            pass
        finally:
            user32.ShowWindow(pl_h, 6)  # SW_MINIMIZE
            if orig_fg and orig_fg != pl_h:
                try:
                    user32.SetForegroundWindow(orig_fg)
                except Exception:
                    pass

    # 6. Check WinRT cellular call status
    try:
        from winrt.windows.applicationmodel.calls import PhoneCallManager
        active = PhoneCallManager.get_is_call_active()
        if not active:
            print("[Outbound Dialer] Cellular call verified ceased in WinRT telephony.")
            hung_up = True
        else:
            print("[Outbound Dialer] WinRT reports cellular line still clearing...")
    except Exception:
        pass

    keep_phone_link_minimized(duration=3.0, restore_hwnd=orig_fg)
    return hung_up


def cancel_outbound_dial(force_hangup: bool = False) -> bool:
    """Cancels any pending outbound call and actively terminates/hangs up the cellular call if an outbound call was active."""
    global _active_outbound, _user_cancelled
    with _lock:
        _user_cancelled = True
        had_outbound = _active_outbound is not None
        _active_outbound = None

    if not had_outbound and not force_hangup:
        return False

    print("[Outbound Dialer] Outbound dialing cancelled — terminating cellular call...")
    hung_up = hangup_current_call()
    try:
        from winrt.windows.applicationmodel.calls import PhoneCallManager
        time.sleep(0.25)
        if PhoneCallManager.get_is_call_active():
            print("[Outbound Dialer] Cellular call still active after initial disconnect; re-invoking hangup...")
            hangup_current_call()
    except Exception:
        pass
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
