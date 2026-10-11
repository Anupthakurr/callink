# Callink: GSM-to-Gemini Live AI Voice Gateway

A real-time Windows voice gateway bridging cellular telephone calls (via an Android smartphone connected over Bluetooth Hands-Free Profile / HFP) directly to Google's **Gemini Live API** (`gemini-3.1-flash-live-preview`).

Allows an AI agent to hold real-time, low-latency, full-duplex spoken conversations with anyone calling your phone number over the standard cellular GSM network.

---

## Architecture Overview

```
                      +-------------------+
                      |   Cellular GSM    |
                      |   Caller / PSTN   |
                      +---------+---------+
                                |
                                v (Cellular SIM)
                      +-------------------+
                      |   Android Phone   |
                      +---------+---------+
                                |
                                | Bluetooth HFP (SCO Audio)
                                v
             +-------------------------------------+
             |         Windows PC Gateway          |
             |                                     |
             |  1. PhoneCallManager (WinRT)        |
             |     - Auto-detects active GSM calls |
             |                                     |
             |  2. HFP Audio Bridge (sounddevice)  |
             |     - 16 kHz Mono PCM capture/play  |
             |                                     |
             |  3. VAD & Barge-In Engine           |
             |     - Client-side RMS cutoff        |
             |     - Server-side interruption sync |
             |                                     |
             |  4. Resampler (audioop)             |
             |     - 24 kHz (Gemini) <-> 16 kHz    |
             +------------------+------------------+
                                |
                                | WebSocket (Bidirectional PCM)
                                v
                  +---------------------------+
                  |  Google Gemini Live API   |
                  |  gemini-3.1-flash-live    |
                  +---------------------------+
```

---

## Key Features

- **Direct Gemini Live Connection:** Direct streaming using the official `google-genai` SDK without intermediate SIP servers, Asterisk, or third-party wrappers.
- **Ultra-Low Latency:** Streams audio in 20ms blocks (320 samples @ 16 kHz) directly over WebSockets.
- **Immediate Proactive Greeting:** Automatically greets the caller within ~1.3 seconds of call pickup via `session.send_client_content`, eliminating awkward dead air.
- **Anti-Cut Barge-In Engine:** 
  - **Calibrated Client-Side VAD:** Thresholded at 3,600 RMS over 100ms with a 350ms speech start grace period, eliminating false mid-sentence cut-offs from breaths, room noise, and earpiece acoustic reflection.
  - **Server-Side VAD Synchronization:** Coordinates with Gemini Live's native server-side interruption signals to synchronize conversational state.
- **Windows Sleep Prevention (Stay-Awake):** Native Win32 `SetThreadExecutionState` integration prevents your PC/laptop from entering sleep or turning off Bluetooth/network while waiting for calls or in active conversation.
- **Automatic Call Lifecycle:** Automatically initiates the AI gateway when an incoming call starts and cleanly tears down sessions when the call hangs up using Windows Runtime (`PhoneCallManager`).
- **Resilient Reconnection:** Handles Bluetooth route transfers and link dropouts gracefully with automatic retry logic and safe resource cleanup.
- **Continuous Multi-Call Operation:** Automatically resets into standby mode to handle subsequent incoming calls without process restarts.

---

## Hardware & Operating System Requirements

- **OS:** Windows 10 or Windows 11 (build 19041+ recommended).
- **Phone:** Android smartphone with an active cellular SIM card.
- **Phone Link Apps (Required for Bluetooth Pairing & Call Audio):**
  - **On Windows:** Microsoft **Phone Link** (pre-installed on Windows 10/11 or free on Microsoft Store).
  - **On Android:** Microsoft **Link to Windows** (free on Google Play Store).
  - Both devices must be paired through Phone Link via Bluetooth with the **Calls** feature enabled. This creates the required `Hands-Free HF Audio` SCO endpoints and enables Windows `PhoneCallManager` to detect incoming GSM cellular calls.
- **Bluetooth:** PC Bluetooth adapter paired to the phone with Hands-Free Audio enabled.

---

## Quickstart (1-Click for Windows)

The easiest way for you or a friend to run this on Windows:

1. **Pair Phone & PC via Phone Link:**
   - Open **Phone Link** on Windows and **Link to Windows** on your Android phone.
   - Pair via Bluetooth and complete the setup for **Calls** (make and receive calls on PC).
2. **Download or clone this folder** onto the PC.
3. **Double-click `run.bat`**:
   - Automatically checks for Python.
   - Automatically prompts for your `GEMINI_API_KEY` on first launch (if `.env` is missing) and creates `.env`.
   - Automatically creates an isolated virtual environment (`venv`) and installs dependencies.
   - Automatically launches the gateway!

---

## Manual Setup

### 1. Clone the Repository
```bash
git clone https://github.com/Anupthakurr/callink.git
cd callink
```

### 2. Set Up Python Environment
Python 3.9+ is supported:
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Gemini API Key
Create a `.env` file in the root directory (or use `.env.example` as a template):
```bash
copy .env.example .env
```
Add your Google Gemini API key:
```env
GEMINI_API_KEY=your_actual_api_key_here
```

### 4. Pair Your Smartphone via Phone Link
1. **On your Windows PC:** Open **Phone Link** (download from Microsoft Store if not pre-installed).
2. **On your Android phone:** Install and launch **Link to Windows** from Google Play Store.
3. **Pair via Bluetooth:** Follow the on-screen pairing guide (scan the QR code or sign into your Microsoft account).
4. **Enable Call Permissions:** When prompted in Phone Link, set up the **Calls** feature and grant Bluetooth pairing and call log permissions on Android.
5. **Verify Audio Profile:** Ensure the device appears with **Hands-Free HF Audio** playback and recording endpoints in Windows sound settings.

---

## Running the Gateway

Start the gateway:
```bash
python gsm_gemini_gateway.py
```
*(Or simply double-click `run.bat`)*

### Execution Flow:
1. Discovers the Bluetooth `Hands-Free HF Audio` input and output endpoints.
2. Enables Windows Stay-Awake mode (`SetThreadExecutionState`), displays the startup banner, and waits in standby:
   ```text
   Waiting for smartphone cellular call...
   ```
3. When someone calls your phone:
   - Cellular call is detected automatically via `PhoneCallManager`.
   - HFP audio bridge starts and Gemini Live connects.
   - AI immediately welcomes the caller (~1.3s proactive greeting).
   - Real-time bidirectional voice conversation begins with robust barge-in support.
4. When the call ends:
   - Session tears down cleanly with zero zombie processes.
   - Automatically returns to standby waiting for the next call.

---

## Packaging for Distribution

Want to share Callink with a friend? Choose the option that fits best:

### Option 1: 1-Click Batch Runner (Recommended if Python is installed)
- Share the repository folder (or a `.zip` without `venv` and `.env`).
- Your friend simply double-clicks **`run.bat`**.
- It checks Python, prompts for their `GEMINI_API_KEY` on first launch, builds the virtual environment, and starts Callink automatically.

### Option 2: Standalone Portable Executable (.exe) (No Python required)
If your friend does not have Python installed:
1. On your PC, double-click **`build_exe.bat`** (or run `pip install pyinstaller && build_exe.bat`).
2. This compiles everything into a self-contained portable folder: `dist\CallinkGateway\`.
3. Copy or create `.env` inside `dist\CallinkGateway\` (with `GEMINI_API_KEY=...`).
4. Zip the `CallinkGateway` folder and send it to your friend.
5. They can extract the zip and double-click `CallinkGateway.exe` directly.

---

## Troubleshooting & Tips

### 1. Bluetooth HFP Device Not Found
- Ensure Microsoft **Phone Link** on PC and **Link to Windows** on Android are paired and connected.
- In your phone's Bluetooth settings (tap the gear icon next to your PC's name), ensure **Phone calls** is toggled ON.
- In Windows Device Manager, ensure the Bluetooth Hands-Free Audio driver (`bthhfenum.sys`) is active and not disabled.

### 2. Calls Not Triggering the Gateway
- Callink uses Windows Runtime (`PhoneCallManager`) to monitor active cellular states.
- Ensure the **Calls** feature in the Windows **Phone Link** app is set up and permitted.
- The phone must be actively engaged in a cellular telephone call (PSTN/GSM), not an app-specific VoIP call (e.g. WhatsApp) unless routed through HFP.

### 3. Laptop Sleep & Screen Lock
- Callink automatically calls `kernel32.SetThreadExecutionState` (`ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED`) to prevent Windows from putting the CPU or Bluetooth adapter to sleep while listening for or handling calls.

### 4. Barge-In & Voice Cut-Off Calibration
- Default barge-in is calibrated for mobile cellular networks:
  - `VAD_RMS_THRESHOLD = 3600` (avoids cutting off on breathing or earpiece acoustics)
  - `VAD_CONSECUTIVE_FRAMES = 5` (~100ms continuous speech)
  - `VAD_SPEECH_START_GRACE_PERIOD = 0.35` (350ms grace window after AI starts speaking)
- If your friend's microphone input volume is unusually quiet or loud, adjust `VAD_RMS_THRESHOLD` in `gsm_gemini_gateway.py` or through the Windows sound input device volume slider.

---

## Testing & Verification Utilities

- **`test_gemini_live_standalone.py`**: Standalone audio pipeline test that sends a sample audio file (`sample.wav`) to Gemini Live, measures response latency, and verifies synthesis playback.
- **`check_env.py`**: Validates the Python environment, dependencies, and API credentials.

---

## License

MIT License.

