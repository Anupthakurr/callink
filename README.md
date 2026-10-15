# Callink: GSM-to-Gemini Live AI Voice Gateway & Studio

<div align="center">

![Platform](https://img.shields.io/badge/Platform-Windows%2010%20%7C%2011-blue?style=for-the-badge&logo=windows)
![Python](https://img.shields.io/badge/Python-3.9%2B-brightgreen?style=for-the-badge&logo=python)
![Model](https://img.shields.io/badge/AI%20Engine-Gemini%203.1%20Flash%20Live-orange?style=for-the-badge&logo=google)
![Latency](https://img.shields.io/badge/Latency-~800ms%20Roundtrip-success?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-purple?style=for-the-badge)

**A high-performance Windows telephony gateway bridging standard cellular telephone calls directly to Google's multimodal Gemini Live API (`gemini-3.1-flash-live-preview`) over Bluetooth Hands-Free Profile (HFP).**

[Features](#-key-features) • [Architecture](#-architecture--telephony-pipeline) • [Latency Benchmark](#-latency-benchmark-callink-vs-legacy-telephony) • [Quickstart](#-quickstart-1-click-on-windows) • [Dashboard Overview](#-glassmorphic-web-dashboard) • [Troubleshooting](#-troubleshooting--audio-tuning)

</div>

---

## 🌟 What is Callink?

**Callink** enables an AI agent to hold real-time, low-latency, full-duplex spoken conversations with anyone calling your phone number over the standard cellular GSM/PSTN network—or when you dispatch proactive outbound calls to customers.

Unlike traditional telephony solutions that require expensive cloud SIP trunks, Asterisk PBX servers, and cascaded STT &rarr; LLM &rarr; TTS pipelines that suffer from 3–5 seconds of awkward conversational delay, **Callink bridges your physical smartphone's Bluetooth baseband audio directly to Google's native speech-to-speech Gemini Live WebSocket gateway on Windows**.

The result is a conversational turn-around latency of **~800ms to 1.2s**, complete with natural barge-in interruptions, multilingual code-switching, automated dual-track recording, and enterprise knowledge grounding.

---

## 🏛️ Architecture & Telephony Pipeline

```text
                      +-----------------------------+
                      |   Standard Cellular Phone   |
                      |     Caller / PSTN / GSM     |
                      +--------------+--------------+
                                     |
                                     v (Cellular Carrier Network)
                      +-----------------------------+
                      |   Android Mobile Phone      |
                      |   with "Link to Windows"    |
                      +--------------+--------------+
                                     |
                                     | Bluetooth HFP / SCO Audio Link
                                     | (16 kHz 16-Bit PCM Bidirectional)
                                     v
       +-------------------------------------------------------------+
       |                  Windows PC Gateway Host                    |
       |                                                             |
       |  1. Windows Runtime Telephony Detector (WinRT)              |
       |     - PhoneCallManager hooks cellular call state changes    |
       |     - Zero polling overhead; instant pickup & hangup events |
       |                                                             |
       |  2. Bluetooth SCO Audio Bridge (sounddevice)                |
       |     - Continuous 20ms frame streaming (320 samples @ 16kHz) |
       |     - 120ms Dynamic SCO Jitter Buffer                       |
       |                                                             |
       |  3. Hardware-Calibrated VAD & Barge-In Engine               |
       |     - Client-side RMS speech energy threshold               |
       |     - Instant local playback queue flushing on interrupt   |
       |                                                             |
       |  4. In-Memory Resampler (audioop / scipy)                   |
       |     - 16 kHz Bluetooth PCM <---> 24 kHz Gemini Live PCM     |
       |                                                             |
       |  5. Dual-Track Synchronized Call Recorder                   |
       |     - Left Channel: Clean Caller Voice                      |
       |     - Right Channel: Gemini AI Voice                        |
       |                                                             |
       |  6. Stay-Awake Guard (Win32 kernel32)                       |
       |     - ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED             |
       +-----------------------------+-------------------------------+
                                     |
                                     | Bidirectional Secure WebSocket
                                     | (Raw Audio PCM Chunks)
                                     v
                      +-----------------------------+
                      |   Google Gemini Live API    |
                      |   gemini-3.1-flash-live     |
                      +-----------------------------+
```

---

## ⚡ Latency Benchmark: Callink vs. Legacy Telephony

Callink was engineered from first principles to overcome the prohibitive conversational latency inherent in traditional telephony architectures (Asterisk PBX + Twilio/Telnyx SIP trunks + Dograh/LiveKit proxy + cascaded STT &rarr; LLM &rarr; TTS pipelines).

### Performance Breakdown

| Pipeline Stage | Legacy Architecture <br>*(Asterisk + SIP Trunk + STT/LLM/TTS)* | Callink Architecture <br>*(Direct Bluetooth HFP + Gemini Live)* | Latency Eliminated |
| :--- | :--- | :--- | :--- |
| **Call Capture & Ingestion** | **~250 – 400 ms** <br>GSM carrier &rarr; SIP Trunk &rarr; Asterisk PBX &rarr; RTP jitter buffer & transcoding | **~30 – 50 ms** <br>Physical GSM modem &rarr; Bluetooth HFP (SCO link) hardware capture (16 kHz 16-bit PCM) | **~250 – 350 ms** |
| **Telephony Gateway / Proxy** | **~150 – 250 ms** <br>SIP proxy / Dograh webhook overhead, framing & packet forwarding | **0 ms** <br>Completely eliminated (Zero PBX / Zero SIP server) | **~150 – 250 ms** |
| **Speech-to-Text (STT)** | **~400 – 700 ms** <br>Buffer chunk accumulation & text transcription (Whisper / Deepgram) | **0 ms** <br>Eliminated (Direct native audio-in multimodal tokens) | **~400 – 700 ms** |
| **LLM Reasoning & TTFT** | **~800 – 1,500 ms** <br>Text prompt serialization & HTTP stream wait time | **~400 – 600 ms** <br>Native continuous token streaming | **~400 – 900 ms** |
| **Text-to-Speech (TTS)** | **~450 – 800 ms** <br>External TTS buffer synthesis wait (ElevenLabs / Cartesia) | **0 ms** <br>Eliminated (Model directly streams 24 kHz raw PCM audio chunks) | **~450 – 800 ms** |
| **Turn Detection / VAD** | **~600 – 900 ms** <br>Client-side silence threshold wait before dispatching audio to STT | **~350 – 500 ms** <br>Server-side continuous streaming semantic turn detection | **~250 – 400 ms** |
| **Return Audio Streaming** | **~200 – 300 ms** <br>TTS audio &rarr; middleware &rarr; Asterisk RTP &rarr; SIP Trunk carrier | **~30 – 50 ms** <br>Gemini WebSocket chunk &rarr; Windows Bluetooth SCO output queue | **~170 – 250 ms** |
| **TOTAL ROUND-TRIP LATENCY** | **~3,200 – 4,500+ ms (3.2 – 4.5s)** | **~800 – 1,200 ms (0.8 – 1.2s)** | **~2,400 – 3,300 ms saved (~70% to 75% drop)** |

---

## ✨ Key Features

### 1. 📞 Inbound & Outbound Cellular AI Telephony
- **Automatic Inbound Detection:** Automatically answers incoming phone calls via Windows Runtime (`PhoneCallManager`) and starts the AI conversation without manual intervention.
- **Outbound AI Call Dispatcher:** Dispatch proactive outbound calls directly from the web dashboard. Enter the recipient's phone number, name, and conversation goal—the AI dials via Windows Phone Link / Android dialer and speaks first with a tailored greeting!
- **Call Pre-Warming:** Telephony listeners detect incoming rings and pre-initialize WebSocket connections to achieve zero-latency pickup.

### 2. 🌐 Multilingual Auto-Detection & Code-Switching
- **Dynamic Language Detection:** The AI automatically detects the caller's spoken language in real-time (Hindi, English, Spanish, French, German, Japanese, Hinglish, etc.) and naturally matches their language and dialect.
- **Custom Default Language:** Set your business's default conversational language in the dashboard settings with seamless fallback adaptation.

### 3. 🎨 Obsidian Glassmorphic Web Dashboard
- **Futuristic Studio UI:** A sleek dark-mode glassmorphic dashboard (`http://localhost:5000`) built with multi-layer ambient lighting, frosted glass cards, and glowing telemetry nodes.
- **Dual-Channel Live VU Meters:** Real-time LED audio level meters with decibel and RMS readouts for both the caller's microphone and Gemini AI's voice.
- **Live Hardware Nodes:** Instant visual status for Windows Phone Link, Bluetooth HFP SCO bridge, Gemini Live API connection, and Windows Stay-Awake guard.
- **Real-Time Auto-Scrolling Transcripts:** Watch speech bubbles appear live on screen as caller and AI converse.

### 4. 🎙️ Dual-Track Call Recording & Master Web Player
- **Studio-Grade Stereo WAVs:** Every call is automatically recorded to disk (`recordings/`) in synchronized 16 kHz 16-bit stereo WAV format:
  - **Left Channel (L):** Isolated caller microphone.
  - **Right Channel (R):** Isolated Gemini AI voice.
- **Master Audio Player with Channel Isolation:** Built-in Web Audio API player allows you to toggle **Stereo (Dual)**, isolate **Caller Only**, isolate **AI Only**, or blend to **Mixed Mono** with playback speed control (0.75x – 2.0x).
- **JSON Metadata & Transcripts:** Full timestamped JSON transcripts saved alongside every WAV recording.

### 5. 🧠 Instant In-Browser Voice Studio
- Talk directly to the Gemini Live AI voice agent in your browser using your PC's microphone and speakers.
- Perfect for testing prompts, rehearsing outbound scripts, or testing document knowledge without dialing a phone number.

### 6. 📚 Zero-Latency Business Knowledge Base Grounding
- Upload business documents (`.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.json`) directly through the web UI.
- Text is automatically extracted, tokenized, and injected straight into Gemini Live's system context.
- Provides grounded, accurate answers on pricing, business policies, menus, and support questions with **zero vector database lookup latency**.

### 7. 🛡️ Enterprise Hardware Resilience
- **Anti-Cut Barge-In Engine:** Calibrated client-side RMS speech detection and immediate audio playback buffer purging allows callers to interrupt the AI naturally.
- **Windows Stay-Awake Guard:** Integrated with Win32 `kernel32.SetThreadExecutionState` (`ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED`) to prevent PC/laptop sleep and keep Bluetooth and network links alive 24/7.
- **120ms Dynamic SCO Jitter Buffer:** Absorbs Bluetooth packet jitter on Windows for smooth, glitch-free telephony playback.

---

## 💻 Hardware & System Requirements

| Component | Minimum Specification | Recommended Specification |
| :--- | :--- | :--- |
| **Operating System** | Windows 10 (64-bit, build 19041+) | Windows 11 (64-bit, latest build) |
| **Smartphone** | Android smartphone with active cellular SIM | Android 10+ with active SIM & Bluetooth 5.0+ |
| **PC Bluetooth** | Bluetooth 4.2 adapter | Bluetooth 5.0+ USB dongle or internal card |
| **Companion Software** | Microsoft **Phone Link** on PC <br>Microsoft **Link to Windows** on Android | Pre-installed on Windows 10/11 <br>Available free on Google Play Store |
| **API Credentials** | Google Gemini API Key | High-quota key from [Google AI Studio](https://aistudio.google.com/) |

> [!IMPORTANT]
> Both devices must be paired through **Phone Link** with the **Calls** feature enabled. This activates the required `Hands-Free HF Audio` Bluetooth SCO endpoints in Windows Sound Settings and allows Windows `PhoneCallManager` to detect cellular telephone events.

---

## 🚀 Quickstart (1-Click on Windows)

### Option 1: Standalone Single Executable (`Callink.exe`) — Zero Python Required!
If you want to run Callink or share it with others who don't have Python installed:
1. Double-click **`Callink.exe`** in the repository root (or download from Releases).
2. On first launch, if your API key isn't configured, enter your `GEMINI_API_KEY` when prompted.
3. The dashboard opens automatically at **`http://localhost:5000`**!

### Option 2: 1-Click Batch Runner (`run.bat`)
1. Clone this repository:
   ```bash
   git clone https://github.com/Anupthakurr/callink.git
   cd callink
   ```
2. Double-click **`run.bat`**:
   - Automatically detects Python 3.9+.
   - Prompts for your `GEMINI_API_KEY` on first launch and writes `.env`.
   - Creates a clean isolated virtual environment (`venv`) and installs dependencies.
   - Launches the gateway and web dashboard.

---

## 🛠️ Manual Installation & Developer Setup

### 1. Clone & Setup Virtual Environment
```bash
git clone https://github.com/Anupthakurr/callink.git
cd callink

python -m venv venv
# On Windows PowerShell:
.\venv\Scripts\Activate.ps1
# On Windows CMD:
venv\Scripts\activate.bat
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Environment Variables
Copy `.env.example` to `.env`:
```bash
copy .env.example .env
```
Add your Gemini API key:
```env
GEMINI_API_KEY=your_actual_gemini_api_key_here
```

### 4. Pair Your Android Phone with Windows Phone Link
1. On your Windows PC, open **Phone Link**.
2. On your Android phone, install and open **Link to Windows**.
3. Follow the on-screen instructions to pair via Bluetooth.
4. In Phone Link, click **Calls** in the sidebar, click **Get Started**, and grant call permissions on your phone.
5. In Windows Sound Settings, verify that **Hands-Free HF Audio** appears under Input and Output devices.

### 5. Launch the Gateway
```bash
python gsm_gemini_gateway.py
```
Open **`http://localhost:5000`** in your browser.

---

## 🖥️ Glassmorphic Web Dashboard

The web dashboard is organized into 5 dedicated studio suites:

```
+-----------------------------------------------------------------------------------------+
| [📞 Callink LIVE AI]  (STANDBY - WAITING)  (Bluetooth HFP: Connected)  [Quit Gateway]    |
| [ ~ Live Gateway ] [ 🎙️ Voice Studio ] [ ⏯️ Recordings (18) ] [ 📄 Knowledge (1) ] [ ⚙️ AI Persona ] |
+-----------------------------------------------------------------------------------------+
```

1. **Live Gateway (`#paneTelemetry`)**:
   - **Outbound AI Call Dispatcher:** Enter phone number, recipient name, select one of the quick goal chips (🎯 Sales Follow-Up, 📅 Appointment Reminder, ⭐ VIP Lead Check-In, 📦 Order & Delivery), or type a custom goal.
   - **Dual-Channel VU Meters:** Live audio activity bars with RMS meter metrics.
   - **Hardware Connectivity:** Telephony status, Bluetooth HFP bridge, Gemini Live connection, and Stay-Awake status.
   - **Live Conversation Stream:** Real-time speech bubbles and event timestamps.

2. **Voice Studio (`#paneStudio`)**:
   - Test conversational interactions directly using your computer's mic and speaker.
   - Features animated pulsating audio orb, real-time waveform, and session recording.

3. **Recordings Library (`#paneRecordings`)**:
   - Master Audio Player with interactive audio scrubber, playback speed selector, and instant download.
   - **Channel Isolation:** Listen to `Stereo (Dual)`, `Caller Only (L)`, `AI Only (R)`, or `Mixed Mono`.
   - Complete history of all calls with metadata and synchronized transcripts.

4. **Knowledge Base (`#paneKnowledge`)**:
   - Drag-and-drop document upload area.
   - Active document cards with extracted word counts and token estimations.
   - **Inspect Injected Prompt:** Preview the exact grounding prompt injected into Gemini Live.

5. **AI Persona & Settings (`#paneSettings`)**:
   - **Voice Persona Selector:** Choose between `Puck`, `Charon`, `Aoede`, `Fenrir`, or `Kore`.
   - **Default Language:** Auto-detect or lock to a specific language (Hindi, English, etc.).
   - **System Instruction Templates:** One-click pre-built templates for Receptionist, Sales, Support, and Scheduling.
   - **Barge-In Calibration:** Adjust VAD RMS threshold and speech start grace period.

---

## 📦 Building Standalone `Callink.exe`

To package the entire Python runtime, dependencies, and web platform into a single distributable Windows `.exe`:

```bash
# Run the automated build script:
build_exe.bat
```
*Or manually with PyInstaller:*
```bash
pyinstaller --noconfirm Callink.spec
```

The resulting binary will be generated in `dist\Callink.exe`. It can be run on any 64-bit Windows 10/11 PC with zero prerequisites.

---

## 🔧 Troubleshooting & Audio Tuning

### 1. "Bluetooth Hands-Free device not found"
- Ensure your phone is paired in Windows **Phone Link** and that the **Calls** feature is active.
- On Android, go to **Settings &rarr; Bluetooth &rarr; Your PC**, tap the gear icon, and ensure **Phone calls** is toggled ON.
- Ensure the Bluetooth Hands-Free Audio driver (`bthhfenum.sys`) is active and not disabled in Device Manager.

### 2. Audio Echo or Beeping on Laptop Speakers
- During phone calls, audio is routed bidirectionally between your phone's cellular baseband modem and your PC's Bluetooth Hands-Free device (`Hands-Free HF Audio`).
- If you want to listen to live calls without disturbing the caller, use headphones or adjust device volume in Windows Sound Settings to prevent acoustic feedback loop into the microphone.

### 3. Tuning Conversational Barge-In
If callers report that the AI stops speaking too easily or fails to stop when interrupted:
- Open the dashboard at `http://localhost:5000` &rarr; **AI Persona** tab.
- **VAD RMS Threshold:**
  - Increase (e.g. `3800 – 4200`) if background noise or breath causes accidental interruptions.
  - Decrease (e.g. `2400 – 3000`) if the caller has a quiet voice or weak cellular signal.
- **Barge-In Grace Period:**
  - Increase (e.g. `0.45s`) to give the AI a brief window to start speaking before interruptions can trigger.

### 4. Preventing Windows Sleep During Standby
Callink automatically invokes `SetThreadExecutionState` on startup to keep the PC active while waiting for cellular calls. If using a laptop on battery, ensure Windows Power & Battery settings allow background network activity when the lid is closed.

---

## 📁 Repository Structure

```text
callink/
├── gsm_gemini_gateway.py        # Core telephony engine, WinRT call detector & Gemini Live bridge
├── web_server.py                # Aiohttp server, WebSocket gateway & API routes
├── outbound_dialer.py           # Outbound cellular dialing via Phone Link & ADB
├── call_recorder.py             # Dual-track stereo WAV recorder & transcript logger
├── knowledge_manager.py         # Document parser (.pdf, .docx, .txt) & prompt injector
├── settings.json                # Persisted user configuration & AI persona settings
├── build_exe.bat                # 1-Click PyInstaller compilation script
├── Callink.spec                 # PyInstaller specification for standalone binary
├── run.bat                      # 1-Click Windows launcher and auto-setup
├── requirements.txt             # Python package dependencies
├── web/                         # Modern Obsidian Glassmorphic Web Dashboard
│   ├── index.html               # Main dashboard layout
│   ├── styles.css               # Obsidian Glassmorphic 2.0 design system
│   └── app.js                  # Frontend WebSocket client, Web Audio player & UI logic
├── recordings/                  # Automated dual-track stereo WAV call recordings
└── knowledge/                   # Uploaded enterprise grounding documents
```

---

## 📄 License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

---

<div align="center">
<b>Callink</b> • Built for ultra-low latency real-time voice AI on standard cellular networks.
</div>
