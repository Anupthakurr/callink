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


def ensure_call_transferred_to_pc(timeout: float = 15.0) -> bool:
    """
    Monitors Phone Link during an active or outgoing cellular call and automatically clicks
    'Transfer to PC' so call audio is routed to PC by default (switching the button to 'Transfer to phone').
    If the button already shows 'Transfer to phone', it leaves it alone and reports success.
    """
    import ctypes
    from ctypes import wintypes
    import os

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

    def _get_proc_name(h):
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
        return os.path.basename(buf.value)

    start_time = time.time()
    while time.time() - start_time < timeout:
        hdesk = _attach_desktop()

        # Find visible Phone Link windows and Windows Call toast popups
        candidate_hwnds = []
        pl_h1 = user32.FindWindowW("WinUIDesktopWin32WindowClass", "Phone Link")
        if pl_h1:
            candidate_hwnds.append(pl_h1)
        pl_h2 = user32.FindWindowW(None, "Phone Link")
        if pl_h2 and pl_h2 not in candidate_hwnds:
            candidate_hwnds.append(pl_h2)

        def _enum_cb(h, lp):
            if user32.IsWindowVisible(h):
                pname = _get_proc_name(h).lower()
                # Exclude browsers and code editors to avoid false positive text matches
                if pname in ["msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "code.exe", "antigravity ide.exe"]:
                    return True
                if pname in ["phoneexperiencehost.exe", "yourphone.exe", "shellexperiencehost.exe"]:
                    if h not in candidate_hwnds:
                        candidate_hwnds.append(h)
                else:
                    length = user32.GetWindowTextLengthW(h)
                    if length > 0:
                        buf = ctypes.create_unicode_buffer(length + 1)
                        user32.GetWindowTextW(h, buf, length + 1)
                        title = buf.value.lower()
                        if ("phone link" in title or "your phone" in title or title == "calls") and "edge" not in title and "chrome" not in title:
                            if h not in candidate_hwnds:
                                candidate_hwnds.append(h)
            return True

        cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum_cb)
        if hdesk:
            user32.EnumDesktopWindows(hdesk, cb, 0)
        user32.EnumWindows(cb, 0)

        # Inspect controls via UI Automation
        try:
            import comtypes
            import comtypes.client
            from comtypes.gen.UIAutomationClient import (
                IUIAutomation, TreeScope_Descendants,
                UIA_ButtonControlTypeId, UIA_ControlTypePropertyId,
                UIA_InvokePatternId, IUIAutomationInvokePattern
            )

            comtypes.CoInitialize()
            try:
                uia = comtypes.client.CreateObject("{ff48dba4-60ef-4201-aa87-54103eef594e}", interface=IUIAutomation)
                root = uia.GetRootElement()

                UIA_NamePropertyId = 30005
                UIA_AutomationIdPropertyId = 30011

                # Global fast check if already transferred
                try:
                    c_already = uia.CreatePropertyCondition(UIA_NamePropertyId, "Transfer to phone")
                    b_already = root.FindFirst(TreeScope_Descendants, c_already)
                    if b_already:
                        print("[Audio Router] Call audio is already routed to PC (button displays 'Transfer to phone').")
                        return True
                except Exception:
                    pass

                def _click_transfer_btn(b):
                    name = (b.CurrentName or "").strip()
                    aid = (b.CurrentAutomationId or "").strip()
                    # Validate: exact name match or known button AutomationId
                    if (
                        name.lower() != "transfer to pc"
                        and aid not in ["VerbButton", "TransferButton", "ActiveCallBannerTransferButtonTarget", "SurpriseCallToastTransferButtonTarget"]
                        and aid.lower() != "transfertopc"
                    ):
                        return False

                    print(f"[Audio Router] Detected 'Transfer to PC' button (Name='{name}', AutomationId='{aid}'). Routing audio to PC...")
                    # 1. InvokePattern
                    try:
                        pattern = b.GetCurrentPattern(UIA_InvokePatternId)
                        if pattern:
                            invoker = pattern.QueryInterface(IUIAutomationInvokePattern)
                            invoker.Invoke()
                            print("[Audio Router] Successfully triggered 'Transfer to PC' via UI Automation!")
                    except Exception as ex:
                        print(f"[Audio Router] InvokePattern notice: {ex}")

                    # 2. Hardware mouse click fallback
                    try:
                        rect = b.CurrentBoundingRectangle
                        bw = rect.right - rect.left
                        bh = rect.bottom - rect.top
                        # Verify coordinate is physically on-screen
                        if bw > 0 and bh > 0 and rect.left >= 0 and rect.top >= 0:
                            cx = rect.left + bw // 2
                            cy = rect.top + bh // 2
                            orig_pt = wintypes.POINT()
                            user32.GetCursorPos(ctypes.byref(orig_pt))
                            user32.SetCursorPos(cx, cy)
                            time.sleep(0.04)
                            user32.mouse_event(0x0002, 0, 0, 0, 0)
                            time.sleep(0.04)
                            user32.mouse_event(0x0004, 0, 0, 0, 0)
                            time.sleep(0.04)
                            user32.SetCursorPos(orig_pt.x, orig_pt.y)
                            print(f"[Audio Router] Hardware click dispatched to Transfer button at ({cx}, {cy}).")
                    except Exception:
                        pass
                    time.sleep(0.4)
                    print("[Audio Router] Call audio transferred to PC! Now showing 'Transfer to phone'.")
                    return True

                # Check root targets directly
                for target_id in ["VerbButton", "TransferButton", "ActiveCallBannerTransferButtonTarget", "SurpriseCallToastTransferButtonTarget"]:
                    try:
                        c_target = uia.CreatePropertyCondition(UIA_AutomationIdPropertyId, target_id)
                        b_target = root.FindFirst(TreeScope_Descendants, c_target)
                        if b_target and _click_transfer_btn(b_target):
                            return True
                    except Exception:
                        pass

                try:
                    c_name_transfer = uia.CreatePropertyCondition(UIA_NamePropertyId, "Transfer to PC")
                    b_name_transfer = root.FindFirst(TreeScope_Descendants, c_name_transfer)
                    if b_name_transfer and _click_transfer_btn(b_name_transfer):
                        return True
                except Exception:
                    pass

                # Inspect candidates
                btn_cond = uia.CreatePropertyCondition(UIA_ControlTypePropertyId, UIA_ButtonControlTypeId)
                for hwnd in candidate_hwnds:
                    try:
                        el = uia.ElementFromHandle(hwnd)
                        if not el:
                            continue
                        btns = el.FindAll(TreeScope_Descendants, btn_cond)
                        if not btns:
                            continue

                        for i in range(btns.Length):
                            b = btns.GetElement(i)
                            name = (b.CurrentName or "").strip().lower()
                            aid = (b.CurrentAutomationId or "").strip()

                            if name in ["transfer to phone", "send call to mobile"]:
                                print("[Audio Router] Call audio is already routed to PC (button displays 'Transfer to phone').")
                                return True

                            if (
                                name == "transfer to pc"
                                or aid in ["VerbButton", "TransferButton", "ActiveCallBannerTransferButtonTarget", "SurpriseCallToastTransferButtonTarget"]
                                or aid.lower() == "transfertopc"
                            ):
                                if _click_transfer_btn(b):
                                    return True
                    except Exception:
                        pass
            finally:
                try:
                    comtypes.CoUninitialize()
                except Exception:
                    pass
        except Exception:
            pass

        time.sleep(0.35)

    return False


def trigger_transfer_to_pc_async(timeout: float = 15.0):
    """Launches ensure_call_transferred_to_pc in a daemon background thread."""
    threading.Thread(target=ensure_call_transferred_to_pc, args=(timeout,), daemon=True).start()


def keep_phone_link_minimized(duration: float = 8.0, restore_hwnd: int = None):
    """
    Background daemon that monitors top-level windows and immediately minimizes
    Microsoft Phone Link if it attempts to pop up, ensuring it never steals focus
    or covers the Callink web dashboard.
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
                def _enum_cb(hwnd, lparam):
                    if not user32.IsWindow(hwnd):
                        return True
                    if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                        return True

                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    try:
                        pname = psutil.Process(pid.value).name().lower()
                    except Exception:
                        pname = ""

                    length = user32.GetWindowTextLengthW(hwnd)
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    title = buf.value.lower()

                    cls_buf = ctypes.create_unicode_buffer(256)
                    user32.GetClassNameW(hwnd, cls_buf, 256)
                    cls = cls_buf.value.lower()

                    # Only target the Phone Link application window, avoid toasts or shell trays
                    is_phone_link = False
                    if "phoneexperiencehost" in pname:
                        if (
                            "winuidesktopwin32windowclass" in cls
                            or "applicationframewindow" in cls
                            or "phone link" in title
                            or "your phone" in title
                        ):
                            is_phone_link = True
                    elif "phone link" in title or "your phone" in title:
                        is_phone_link = True

                    if is_phone_link:
                        rect = wintypes.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        if w > 100 and h > 100:
                            SW_FORCEMINIMIZE = 11
                            SW_SHOWMINNOACTIVE = 7
                            user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
                            user32.ShowWindow(hwnd, SW_FORCEMINIMIZE)
                            current_fg = user32.GetForegroundWindow()
                            if current_fg == hwnd and target_restore and target_restore != hwnd:
                                user32.SetForegroundWindow(target_restore)
                    return True

                cb = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_enum_cb)
                user32.EnumWindows(cb, 0)
            except Exception:
                pass
            time.sleep(0.1)

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
    time.sleep(0.5)

    # Launch asynchronous audio transfer watcher early so as soon as in-call banner appears, audio transfers to PC
    trigger_transfer_to_pc_async(timeout=15.0)

    clicked_count = 0
    # Poll for up to 8 seconds (20 iterations x 0.4s)
    for _ in range(20):
        if _is_call_active():
            print("[Outbound Dialer] Cellular call active detected! Auto-dialer disengaging.")
            # Re-ensure audio transfer once active
            trigger_transfer_to_pc_async(timeout=10.0)
            return

        hwnd = _find_phone_link()
        if hwnd:
            # 1. First attempt to invoke ButtonCall directly via UIAutomation WITHOUT unminimizing or focusing
            call_invoked = False
            try:
                import comtypes
                import comtypes.client
                from comtypes.gen.UIAutomationClient import (
                    IUIAutomation, TreeScope_Descendants,
                    UIA_ButtonControlTypeId, UIA_ControlTypePropertyId,
                    UIA_InvokePatternId, IUIAutomationInvokePattern
                )
                comtypes.CoInitialize()
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
                                if bid == "ButtonCall" or "call dialled number" in bname:
                                    p = b.GetCurrentPattern(UIA_InvokePatternId)
                                    if p:
                                        inv = p.QueryInterface(IUIAutomationInvokePattern)
                                        inv.Invoke()
                                        call_invoked = True
                                        print("[Outbound Dialer] Invoked ButtonCall directly via UIAutomation!")
                                        # Immediately keep minimized
                                        user32.ShowWindow(hwnd, 6) # SW_MINIMIZE
                                        break
                finally:
                    try:
                        comtypes.CoUninitialize()
                    except Exception:
                        pass
            except Exception:
                pass

            if call_invoked:
                trigger_transfer_to_pc_async(timeout=12.0)
                time.sleep(1.0)
                if _is_call_active():
                    return
                continue

            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top

            # 2. Window coordinate click fallback: Keypad green Call button center (84.5% X, 83.5% Y)
            if w > 200 and h > 200:
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

                # Immediately minimize Phone Link back down
                user32.ShowWindow(hwnd, 6) # SW_MINIMIZE

                # Restore original cursor position and previous foreground window
                time.sleep(0.03)
                user32.SetCursorPos(orig_pt.x, orig_pt.y)
                if orig_fg and orig_fg != hwnd:
                    user32.SetForegroundWindow(orig_fg)

                clicked_count += 1
                print(f"[Outbound Dialer] Dispatched Call trigger at ({click_x}, {click_y}) [attempt {clicked_count}].")

                # Re-engage audio transfer to PC watcher
                trigger_transfer_to_pc_async(timeout=12.0)

                # If clicked at least twice, pause longer to check if connected
                if clicked_count >= 3:
                    time.sleep(1.0)
                    if _is_call_active():
                        trigger_transfer_to_pc_async(timeout=10.0)
                        return

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
            direct_dialed = pool.submit(_direct_winrt_dial, e164_number).result(timeout=2.0)
        if direct_dialed:
            print(f"[Outbound Dialer] Dispatched cellular call to {e164_number} directly via WinRT PhoneLine.")
            # Trigger audio transfer to PC watcher in background
            trigger_transfer_to_pc_async(timeout=20.0)
            # Ensure Phone Link window never pops up or steals focus
            keep_phone_link_minimized(duration=8.0, restore_hwnd=orig_fg)
            return True
    except Exception as e:
        print(f"[Outbound Dialer] Direct WinRT line check: {e}")

    # 4. Fallback path: Start Phone Link minimizer before launching Phone Link
    keep_phone_link_minimized(duration=8.0, restore_hwnd=orig_fg)

    # Launch auto-dial helper in background thread to navigate Calls tab & press Enter/ButtonCall
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
            keep_phone_link_minimized(duration=8.0, restore_hwnd=orig_fg)
            return True
    except Exception as e:
        print(f"[Outbound Dialer] WinRT Phone Link targeting: {e}")

    # 6. Dedicated Phone Link URI protocol (ms-phone: protocol unique to Phone Link)
    try:
        os.startfile(f"ms-phone:{e164_number}")
        print(f"[Outbound Dialer] Dispatched via ms-phone URI for {e164_number}.")
        keep_phone_link_minimized(duration=8.0, restore_hwnd=orig_fg)
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
        keep_phone_link_minimized(duration=8.0, restore_hwnd=orig_fg)
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
