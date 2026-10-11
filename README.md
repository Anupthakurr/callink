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
- **Instant Barge-In (Interruption Handling):** 
  - **Client-Side VAD:** Detects caller voice locally using RMS amplitude thresholding and immediately clears the playback queue to stop AI speech without waiting for network round-trips.
  - **Server-Side VAD:** Coordinates with Gemini Live's native server-side interruption signals to synchronize conversation state.
- **Automatic Call Lifecycle:** Automatically initiates the AI gateway when an incoming call starts and cleanly tears down sessions when the call hangs up using Windows Runtime (`PhoneCallManager`).
- **Resilient Reconnection:** Handles Bluetooth route transfers and link dropouts gracefully with automatic retry logic and safe resource cleanup.
- **Continuous Multi-Call Operation:** Automatically resets into standby mode to handle subsequent incoming calls without process restarts.

---

## Hardware & Operating System Requirements

- **OS:** Windows 10 or Windows 11 (build 19041+ recommended)
- **Phone:** Android smartphone with an active cellular SIM card
- **Bluetooth:** PC with Bluetooth adapter paired to the smartphone with **Hands-Free Audio** enabled (creates `Hands-Free HF Audio` playback and recording endpoints).

---

## Installation & Setup

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

### 4. Pair Your Smartphone
1. In Windows Settings -> **Bluetooth & devices**, pair your Android phone.
2. In device properties, verify that **Hands-Free** / **Audio** profiles are active.

---

## Running the Gateway

Start the gateway:
```bash
python gsm_gemini_gateway.py
```

### Execution Flow:
1. Discovers the Bluetooth `Hands-Free HF Audio` input and output endpoints.
2. Displays the startup banner and waits in standby:
   ```text
   Waiting for smartphone cellular call...
   ```
3. When someone calls your phone:
   - Cellular call is detected automatically.
   - HFP audio bridge starts.
   - Gemini Live session connects.
   - AI converses with caller with real-time barge-in support.
4. When the call ends:
   - Session tears down cleanly.
   - Returns to standby waiting for the next call.

---

## Testing & Verification Utilities

- **`test_gemini_live_standalone.py`**: Standalone audio pipeline test that sends a sample audio file (`sample.wav`) to Gemini Live, measures response latency, and verifies synthesis playback.
- **`check_env.py`**: Validates the Python environment, dependencies, and API credentials.

---

## License

MIT License.
