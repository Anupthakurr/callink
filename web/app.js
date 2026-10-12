/**
 * Callink — Modern Web Platform & Dual-Track Audio Player
 * Real-time GSM Telemetry, Browser Studio, Web Audio Channel Isolator & Recordings Library
 */

// Global Application State
const appState = {
  activeTab: 'telemetry',
  callActive: false,
  callTimerInterval: null,
  callSeconds: 0,
  recordings: [],
  currentRecording: null,
  audioPlayer: new Audio(),
  audioContext: null,
  audioSourceNode: null,
  splitterNode: null,
  mergerNode: null,
  gainNodes: { left: null, right: null },
  channelMode: 'stereo', // 'stereo', 'left', 'right', 'mono'
  telemetryWs: null,
  studioWs: null,
  studioMediaStream: null,
  studioAudioContext: null,
  studioProcessor: null,
  studioPlayingQueue: [],
  isStudioCalling: false,
  studioTimerInterval: null,
  studioSeconds: 0,
};

// Preset Prompts for AI Persona
const PRESET_PROMPTS = {
  receptionist:
    "You are a professional, warm, and helpful corporate receptionist for Callink Enterprises. " +
    "Welcome the caller, ask politely how you can direct their call or assist them, and provide concise, natural answers (1-2 sentences).",
  sales:
    "You are a charismatic, high-energy sales representative and lead qualifier. " +
    "Greet the caller, discover their business needs, answer their questions with enthusiasm, and guide them toward scheduling a product demo.",
  appointment:
    "You are an efficient, polite appointment scheduling coordinator. " +
    "Help the caller book, reschedule, or check existing appointment times. Speak clearly, confirm their name and preferred date/time, and keep turns brief.",
  support:
    "You are a knowledgeable, calm, and empathetic technical customer support specialist. " +
    "Listen carefully to the customer's issue, ask clarifying questions, provide immediate troubleshooting steps, and ensure they feel valued."
};

// ==========================================================================
// Initialization
// ==========================================================================

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initTelemetryWebSocket();
  initMasterAudioPlayer();
  initRecordingsLibrary();
  initStudioMode();
  initSettings();
});

// ==========================================================================
// Navigation & Tabs
// ==========================================================================

function initTabs() {
  const tabs = document.querySelectorAll('.nav-tab');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const targetPaneId = 'pane' + tab.dataset.tab.charAt(0).toUpperCase() + tab.dataset.tab.slice(1);
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');

      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      const targetPane = document.getElementById(targetPaneId);
      if (targetPane) targetPane.classList.add('active');

      appState.activeTab = tab.dataset.tab;
      if (appState.activeTab === 'recordings') {
        loadRecordings();
      }
    });
  });
}

// ==========================================================================
// Live Telemetry & WebSockets
// ==========================================================================

function initTelemetryWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

  function connect() {
    const ws = new WebSocket(wsUrl);
    appState.telemetryWs = ws;

    ws.onopen = () => {
      console.log('[Telemetry WS] Connected to gateway');
    };

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        handleTelemetryMessage(payload);
      } catch (e) {
        console.error('[Telemetry WS] Message parsing error:', e);
      }
    };

    ws.onclose = () => {
      console.log('[Telemetry WS] Disconnected. Reconnecting in 2s...');
      setTimeout(connect, 2000);
    };

    ws.onerror = (err) => {
      console.warn('[Telemetry WS] Socket error:', err);
      ws.close();
    };
  }

  connect();

  // Polling fallback every 1.5s for audio levels and gateway state
  setInterval(fetchStatusPolling, 1500);
}

function handleTelemetryMessage(msg) {
  if (msg.type === 'state_update') {
    updateGatewayState(msg.state);
  } else if (msg.type === 'call_started') {
    startLiveCallUI(msg.data.call_id, msg.data.call_type || 'gsm');
  } else if (msg.type === 'call_ended') {
    endLiveCallUI(msg.data.metadata);
  } else if (msg.type === 'transcript') {
    appendLiveTranscript(msg.data.role, msg.data.text);
  } else if (msg.type === 'barge_in') {
    triggerBargeInFlash();
  }
}

async function fetchStatusPolling() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    updateGatewayState(data);
  } catch (e) {
    // server momentarily unreachable
  }
}

function updateGatewayState(state) {
  if (!state) return;

  const statusPill = document.getElementById('statusPill');
  const statusText = document.getElementById('statusText');
  const deviceText = document.getElementById('deviceText');

  // HFP / Devices info
  if (state.hfp_input_name && state.hfp_input_name !== 'Detecting...') {
    deviceText.textContent = `HFP: ${state.hfp_input_name.slice(0, 24)}...`;
  }

  // Update Status Pill
  if (state.status === 'in_call') {
    statusPill.className = 'status-pill status-in-call';
    statusText.textContent = `${state.call_type === 'browser' ? 'BROWSER CALL' : 'GSM CELLULAR CALL'} IN PROGRESS`;
    if (!appState.callActive) {
      startLiveCallUI(state.call_id, state.call_type);
    }
  } else {
    statusPill.className = 'status-pill status-standby';
    statusText.textContent = 'STANDBY — WAITING FOR CALL';
    if (appState.callActive) {
      endLiveCallUI();
    }
  }

  // VU Meters Audio Level animation
  renderAudioMeter('callerMeterBar', 'callerRmsVal', state.caller_rms || 0);
  renderAudioMeter('aiMeterBar', 'aiRmsVal', state.ai_rms || 0);
}

function renderAudioMeter(barId, labelId, rms) {
  const bar = document.getElementById(barId);
  const label = document.getElementById(labelId);
  if (!bar || !label) return;

  label.textContent = `RMS: ${rms}`;
  // Normalize 0 to 8000 RMS -> 0 to 100%
  const pct = Math.min(100, Math.max(0, Math.round((rms / 6000) * 100)));
  bar.style.width = `${pct}%`;
}

function startLiveCallUI(callId, callType) {
  appState.callActive = true;
  appState.callSeconds = 0;

  const banner = document.getElementById('liveCallBanner');
  banner.classList.remove('hidden');

  const title = document.getElementById('bannerCallTitle');
  title.textContent = callType === 'browser' ? 'BROWSER STUDIO CALL IN PROGRESS' : 'CELLULAR GSM CALL IN PROGRESS';

  clearInterval(appState.callTimerInterval);
  appState.callTimerInterval = setInterval(() => {
    appState.callSeconds++;
    const timerElem = document.getElementById('liveCallTimer');
    if (timerElem) timerElem.textContent = formatDuration(appState.callSeconds);
  }, 1000);
}

function endLiveCallUI(metadata) {
  appState.callActive = false;
  clearInterval(appState.callTimerInterval);

  const banner = document.getElementById('liveCallBanner');
  if (banner) banner.classList.add('hidden');

  renderAudioMeter('callerMeterBar', 'callerRmsVal', 0);
  renderAudioMeter('aiMeterBar', 'aiRmsVal', 0);

  // If in recordings tab or background, refresh list
  loadRecordings();
}

function triggerBargeInFlash() {
  const alert = document.getElementById('bargeInAlert');
  if (!alert) return;
  alert.classList.remove('hidden');
  setTimeout(() => {
    alert.classList.add('hidden');
  }, 1200);
}

function appendLiveTranscript(role, text) {
  const container = document.getElementById('liveTranscriptBox');
  const placeholder = document.getElementById('transcriptPlaceholder');
  if (placeholder) placeholder.remove();

  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role === 'model' ? 'bubble-ai' : 'bubble-caller'}`;

  const now = new Date();
  const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

  bubble.innerHTML = `
    <div class="bubble-header">
      <span>${role === 'model' ? '🤖 Gemini AI' : '📞 Caller'}</span>
      <span class="bubble-time">${timeStr}</span>
    </div>
    <div class="bubble-text">${escapeHtml(text)}</div>
  `;

  container.appendChild(bubble);
  container.scrollTop = container.scrollHeight;
}

document.getElementById('btnClearLiveTranscript')?.addEventListener('click', () => {
  const box = document.getElementById('liveTranscriptBox');
  box.innerHTML = `
    <div class="transcript-placeholder" id="transcriptPlaceholder">
      <div class="pulse-placeholder-dot"></div>
      Waiting for an incoming GSM cellular call or Browser Studio session to begin transcription...
    </div>
  `;
});

// ==========================================================================
// Master Audio Player & Web Audio Channel Isolator
// ==========================================================================

function initMasterAudioPlayer() {
  const player = appState.audioPlayer;
  const playBtn = document.getElementById('btnPlayerPlay');
  const playIcon = document.getElementById('playIcon');
  const pauseIcon = document.getElementById('pauseIcon');
  const scrubSlider = document.getElementById('playerScrubSlider');
  const scrubProgressFill = document.getElementById('scrubProgressFill');
  const currentTimeEl = document.getElementById('playerCurrentTime');
  const totalTimeEl = document.getElementById('playerTotalTime');
  const volumeSlider = document.getElementById('playerVolume');
  const speedSelect = document.getElementById('playerSpeedSelect');

  // Play / Pause Toggle
  playBtn.addEventListener('click', () => {
    if (!player.src) return;
    ensureAudioContext();

    if (player.paused) {
      player.play();
    } else {
      player.pause();
    }
  });

  player.addEventListener('play', () => {
    playIcon.classList.add('hidden');
    pauseIcon.classList.remove('hidden');
  });

  player.addEventListener('pause', () => {
    playIcon.classList.remove('hidden');
    pauseIcon.classList.add('hidden');
  });

  // Time & Progress Updates
  player.addEventListener('timeupdate', () => {
    if (isNaN(player.duration)) return;
    const current = player.currentTime;
    const total = player.duration;
    currentTimeEl.textContent = formatDuration(Math.floor(current));
    totalTimeEl.textContent = formatDuration(Math.floor(total));

    const pct = (current / total) * 100;
    scrubSlider.value = pct;
    scrubProgressFill.style.width = `${pct}%`;
  });

  // Scrubbing
  scrubSlider.addEventListener('input', () => {
    if (isNaN(player.duration)) return;
    const targetTime = (scrubSlider.value / 100) * player.duration;
    player.currentTime = targetTime;
    scrubProgressFill.style.width = `${scrubSlider.value}%`;
  });

  // Volume
  volumeSlider.addEventListener('input', () => {
    player.volume = parseFloat(volumeSlider.value);
  });

  // Speed
  speedSelect.addEventListener('change', () => {
    player.playbackRate = parseFloat(speedSelect.value);
  });

  // Channel Mode Buttons
  const modeButtons = document.querySelectorAll('.btn-toggle');
  modeButtons.forEach(btn => {
    btn.addEventListener('click', () => {
      modeButtons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      setChannelMode(btn.dataset.mode);
    });
  });
}

function ensureAudioContext() {
  if (appState.audioContext) {
    if (appState.audioContext.state === 'suspended') {
      appState.audioContext.resume();
    }
    return;
  }

  try {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    const ctx = new AudioCtx();
    const player = appState.audioPlayer;

    const source = ctx.createMediaElementSource(player);
    const splitter = ctx.createChannelSplitter(2);
    const merger = ctx.createChannelMerger(2);

    // Left Gain (Caller) and Right Gain (AI)
    const gainLeft = ctx.createGain();
    const gainRight = ctx.createGain();

    source.connect(splitter);

    // Default Stereo:
    // Splitter Ch0 (Left/Caller) -> gainLeft -> Merger Ch0 (Left speaker)
    // Splitter Ch1 (Right/AI)   -> gainRight -> Merger Ch1 (Right speaker)
    splitter.connect(gainLeft, 0);
    splitter.connect(gainRight, 1);

    gainLeft.connect(merger, 0, 0);
    gainRight.connect(merger, 0, 1);

    merger.connect(ctx.destination);

    appState.audioContext = ctx;
    appState.audioSourceNode = source;
    appState.splitterNode = splitter;
    appState.mergerNode = merger;
    appState.gainNodes.left = gainLeft;
    appState.gainNodes.right = gainRight;

    setChannelMode(appState.channelMode);
  } catch (e) {
    console.warn('[AudioContext] Could not initialize Web Audio Graph:', e);
  }
}

function setChannelMode(mode) {
  appState.channelMode = mode;
  if (!appState.audioContext || !appState.splitterNode || !appState.mergerNode) return;

  const { splitterNode, mergerNode, audioContext } = appState;

  // Disconnect existing routing
  try {
    splitterNode.disconnect();
    mergerNode.disconnect();
    mergerNode.connect(audioContext.destination);
  } catch (e) {}

  if (mode === 'stereo') {
    // Normal Stereo: Caller in Left Ear, AI in Right Ear
    splitterNode.connect(mergerNode, 0, 0); // L -> L
    splitterNode.connect(mergerNode, 1, 1); // R -> R
  } else if (mode === 'left') {
    // Caller Isolated: Send Channel 0 to both Left & Right speakers
    splitterNode.connect(mergerNode, 0, 0); // L -> L
    splitterNode.connect(mergerNode, 0, 1); // L -> R
  } else if (mode === 'right') {
    // Gemini AI Isolated: Send Channel 1 to both Left & Right speakers
    splitterNode.connect(mergerNode, 1, 0); // R -> L
    splitterNode.connect(mergerNode, 1, 1); // R -> R
  } else if (mode === 'mono') {
    // Mixed Mono: Sum both channels into center
    splitterNode.connect(mergerNode, 0, 0);
    splitterNode.connect(mergerNode, 1, 0);
    splitterNode.connect(mergerNode, 0, 1);
    splitterNode.connect(mergerNode, 1, 1);
  }
}

function playRecording(callId) {
  const rec = appState.recordings.find(r => r.call_id === callId);
  if (!rec) return;

  appState.currentRecording = rec;
  const player = appState.audioPlayer;

  // Update track headers
  document.getElementById('playerTrackTitle').textContent = `Call #${rec.call_id}`;
  const dateFormatted = new Date(rec.start_time).toLocaleString();
  document.getElementById('playerTrackMeta').textContent = `${rec.call_type?.toUpperCase()} Call • ${dateFormatted} • ${rec.duration_seconds}s`;

  // Download link
  const dlBtn = document.getElementById('btnDownloadActive');
  dlBtn.href = `/api/recordings/${rec.call_id}/audio`;
  dlBtn.setAttribute('download', `${rec.call_id}.wav`);

  // Load audio source
  player.src = `/api/recordings/${rec.call_id}/audio`;
  ensureAudioContext();
  player.play().catch(e => console.warn('Autoplay prevented:', e));

  // Switch to recordings tab if not already
  document.getElementById('tabBtnRecordings')?.click();
}

// ==========================================================================
// Recordings Library
// ==========================================================================

function initRecordingsLibrary() {
  document.getElementById('btnRefreshRecordings')?.addEventListener('click', loadRecordings);
  document.getElementById('btnModalClose')?.addEventListener('click', closeModal);
  document.getElementById('transcriptModal')?.addEventListener('click', (e) => {
    if (e.target.id === 'transcriptModal') closeModal();
  });
  loadRecordings();
}

async function loadRecordings() {
  try {
    const res = await fetch('/api/recordings');
    if (!res.ok) return;
    const data = await res.json();
    appState.recordings = data.recordings || [];
    renderRecordingsList();
  } catch (e) {
    console.error('Failed to load recordings:', e);
  }
}

function renderRecordingsList() {
  const grid = document.getElementById('recordingsGrid');
  const countTag = document.getElementById('recordingsCountTag');
  const badge = document.getElementById('recordingsBadge');
  const noBox = document.getElementById('noRecordingsBox');

  const count = appState.recordings.length;
  countTag.textContent = `${count} Total`;
  badge.textContent = count;

  if (count === 0) {
    grid.innerHTML = '';
    grid.appendChild(noBox);
    return;
  }

  grid.innerHTML = '';
  appState.recordings.forEach(rec => {
    const card = document.createElement('div');
    card.className = 'recording-card';

    const startDate = new Date(rec.start_time);
    const dateStr = startDate.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
    const timeStr = startDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const durationStr = formatDuration(Math.round(rec.duration_seconds));
    const sizeStr = rec.file_size_bytes ? `${(rec.file_size_bytes / (1024 * 1024)).toFixed(2)} MB` : 'WAV';

    // Transcript preview snippet
    let previewText = 'No spoken transcripts recorded for this session.';
    if (rec.transcripts && rec.transcripts.length > 0) {
      const first = rec.transcripts[0];
      const speaker = first.role === 'model' ? 'AI' : 'Caller';
      previewText = `"${speaker}: ${first.text}"`;
    }

    const typeBadgeClass = rec.call_type === 'gsm' ? 'badge-gsm' : 'badge-browser';
    const typeLabel = rec.call_type === 'gsm' ? '📱 GSM Cellular' : '💻 Browser Studio';

    card.innerHTML = `
      <div class="recording-card-header">
        <div class="rec-title-wrap">
          <h4>Call #${rec.call_id}</h4>
          <div class="rec-meta-row">
            <span>📅 ${dateStr} at ${timeStr}</span>
            <span>⏱️ ${durationStr}</span>
            <span>💾 ${sizeStr}</span>
          </div>
        </div>
        <span class="rec-type-badge ${typeBadgeClass}">${typeLabel}</span>
      </div>

      <div class="rec-transcript-preview">
        ${escapeHtml(previewText)}
      </div>

      <div class="rec-card-actions">
        <div class="rec-actions-left">
          <button class="btn-sm btn-primary btn-play-rec" data-id="${rec.call_id}">
            ▶ Play
          </button>
          <button class="btn-sm btn-ghost btn-view-trans" data-id="${rec.call_id}">
            💬 Transcripts (${rec.transcripts?.length || 0})
          </button>
          <a href="/api/recordings/${rec.call_id}/audio" download="${rec.call_id}.wav" class="btn-sm btn-ghost">
            ⬇ WAV
          </a>
        </div>
        <button class="btn-icon-danger btn-del-rec" data-id="${rec.call_id}" title="Delete recording">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/></svg>
        </button>
      </div>
    `;

    // Event listeners
    card.querySelector('.btn-play-rec').addEventListener('click', () => playRecording(rec.call_id));
    card.querySelector('.btn-view-trans').addEventListener('click', () => openTranscriptModal(rec));
    card.querySelector('.btn-del-rec').addEventListener('click', () => deleteRecordingConfirm(rec.call_id));

    grid.appendChild(card);
  });
}

async function deleteRecordingConfirm(callId) {
  if (!confirm(`Are you sure you want to delete call recording #${callId}?`)) return;
  try {
    const res = await fetch(`/api/recordings/${callId}`, { method: 'DELETE' });
    if (res.ok) {
      loadRecordings();
    }
  } catch (e) {
    alert('Failed to delete recording: ' + e);
  }
}

function openTranscriptModal(rec) {
  const modal = document.getElementById('transcriptModal');
  const title = document.getElementById('modalCallTitle');
  const body = document.getElementById('modalTranscriptBody');

  title.textContent = `Call #${rec.call_id} — Conversation Transcripts`;
  body.innerHTML = '';

  if (!rec.transcripts || rec.transcripts.length === 0) {
    body.innerHTML = '<div class="transcript-placeholder">No spoken words recorded in this call.</div>';
  } else {
    rec.transcripts.forEach(t => {
      const bubble = document.createElement('div');
      bubble.className = `chat-bubble ${t.role === 'model' ? 'bubble-ai' : 'bubble-caller'}`;
      const timeOffset = formatDuration(Math.round(t.offset_seconds || 0));
      bubble.innerHTML = `
        <div class="bubble-header">
          <span>${t.role === 'model' ? '🤖 Gemini AI' : '📞 Caller'}</span>
          <span class="bubble-time">+${timeOffset}</span>
        </div>
        <div class="bubble-text">${escapeHtml(t.text)}</div>
      `;
      body.appendChild(bubble);
    });
  }

  modal.classList.remove('hidden');
}

function closeModal() {
  document.getElementById('transcriptModal')?.classList.add('hidden');
}

// ==========================================================================
// Browser Call Studio (Microphone & Speaker In-Browser AI Call)
// ==========================================================================

function initStudioMode() {
  const startBtn = document.getElementById('btnStartBrowserCall');
  const endBtn = document.getElementById('btnEndBrowserCall');

  startBtn?.addEventListener('click', startBrowserVoiceCall);
  endBtn?.addEventListener('click', endBrowserVoiceCall);
}

async function startBrowserVoiceCall() {
  const startBtn = document.getElementById('btnStartBrowserCall');
  const endBtn = document.getElementById('btnEndBrowserCall');
  const orb = document.getElementById('studioOrb');
  const statusLabel = document.getElementById('studioStatusLabel');
  const box = document.getElementById('studioTranscriptsBox');

  statusLabel.textContent = 'Requesting microphone permission...';

  try {
    // 1. Get browser microphone stream
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: 16000,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      }
    });
    appState.studioMediaStream = stream;

    // 2. Connect WebSocket
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/browser_call`;
    const ws = new WebSocket(wsUrl);
    appState.studioWs = ws;
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      statusLabel.textContent = 'Connected! Speak to the AI assistant...';
      orb.classList.add('active');
      startBtn.classList.add('hidden');
      endBtn.classList.remove('hidden');
      box.innerHTML = '';

      appState.isStudioCalling = true;
      appState.studioSeconds = 0;
      clearInterval(appState.studioTimerInterval);
      appState.studioTimerInterval = setInterval(() => {
        appState.studioSeconds++;
        document.getElementById('studioCallTimer').textContent = formatDuration(appState.studioSeconds);
      }, 1000);

      // Start audio processing pipeline
      setupStudioAudioPipeline(stream, ws);
    };

    ws.onmessage = async (event) => {
      if (typeof event.data === 'string') {
        const msg = JSON.parse(event.data);
        if (msg.type === 'transcript') {
          appendStudioTranscript(msg.role, msg.text);
        } else if (msg.type === 'call_ended') {
          teardownStudioSession();
          loadRecordings();
        } else if (msg.type === 'error') {
          alert('Studio error: ' + msg.message);
          endBrowserVoiceCall();
        }
      } else {
        // Raw 24kHz PCM from Gemini Live
        playGeminiPcmChunk(event.data);
      }
    };

    ws.onclose = () => {
      teardownStudioSession();
    };

  } catch (err) {
    statusLabel.textContent = 'Microphone access denied or error: ' + err.message;
    console.error('Browser call initiation failed:', err);
  }
}

function setupStudioAudioPipeline(stream, ws) {
  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const ctx = new AudioCtx({ sampleRate: 16000 });
  appState.studioAudioContext = ctx;

  const source = ctx.createMediaStreamSource(stream);
  // ScriptProcessor to grab 16-bit PCM blocks
  const processor = ctx.createScriptProcessor(512, 1, 1);
  appState.studioProcessor = processor;

  processor.onaudioprocess = (e) => {
    if (!appState.isStudioCalling || ws.readyState !== WebSocket.OPEN) return;
    const inputData = e.inputBuffer.getChannelData(0);

    // Convert Float32Array to 16-bit PCM ArrayBuffer
    const pcm16 = new Int16Array(inputData.length);
    for (let i = 0; i < inputData.length; i++) {
      let s = Math.max(-1, Math.min(1, inputData[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    ws.send(pcm16.buffer);
  };

  source.connect(processor);
  processor.connect(ctx.destination);
}

// Browser audio playback for Gemini Live 24kHz raw PCM
let studioPlaybackCtx = null;
let studioNextStartTime = 0;

function playGeminiPcmChunk(arrayBuffer) {
  if (!studioPlaybackCtx) {
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    studioPlaybackCtx = new AudioCtx({ sampleRate: 24000 });
  }

  const int16Array = new Int16Array(arrayBuffer);
  const float32Array = new Float32Array(int16Array.length);
  for (let i = 0; i < int16Array.length; i++) {
    float32Array[i] = int16Array[i] / 32768.0;
  }

  const audioBuffer = studioPlaybackCtx.createBuffer(1, float32Array.length, 24000);
  audioBuffer.copyToChannel(float32Array, 0);

  const bufferSource = studioPlaybackCtx.createBufferSource();
  bufferSource.buffer = audioBuffer;
  bufferSource.connect(studioPlaybackCtx.destination);

  const currentTime = studioPlaybackCtx.currentTime;
  if (studioNextStartTime < currentTime) {
    studioNextStartTime = currentTime;
  }
  bufferSource.start(studioNextStartTime);
  studioNextStartTime += audioBuffer.duration;
}

function appendStudioTranscript(role, text) {
  const box = document.getElementById('studioTranscriptsBox');
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble ${role === 'model' ? 'bubble-ai' : 'bubble-caller'}`;
  bubble.innerHTML = `
    <div class="bubble-header">
      <span>${role === 'model' ? '🤖 Gemini AI' : '👤 You'}</span>
    </div>
    <div class="bubble-text">${escapeHtml(text)}</div>
  `;
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
}

function endBrowserVoiceCall() {
  if (appState.studioWs && appState.studioWs.readyState === WebSocket.OPEN) {
    appState.studioWs.send(JSON.stringify({ type: 'end_call' }));
  }
  teardownStudioSession();
}

function teardownStudioSession() {
  appState.isStudioCalling = false;
  clearInterval(appState.studioTimerInterval);

  if (appState.studioMediaStream) {
    appState.studioMediaStream.getTracks().forEach(t => t.stop());
    appState.studioMediaStream = null;
  }

  if (appState.studioProcessor) {
    try { appState.studioProcessor.disconnect(); } catch (e) {}
    appState.studioProcessor = null;
  }

  if (appState.studioAudioContext) {
    try { appState.studioAudioContext.close(); } catch (e) {}
    appState.studioAudioContext = null;
  }

  const startBtn = document.getElementById('btnStartBrowserCall');
  const endBtn = document.getElementById('btnEndBrowserCall');
  const orb = document.getElementById('studioOrb');
  const statusLabel = document.getElementById('studioStatusLabel');

  orb?.classList.remove('active');
  startBtn?.classList.remove('hidden');
  endBtn?.classList.add('hidden');
  if (statusLabel) statusLabel.textContent = 'Call completed. Recording saved to library!';
}

// ==========================================================================
// Settings & AI Persona
// ==========================================================================

function initSettings() {
  loadSettings();

  // Presets
  document.querySelectorAll('.btn-preset').forEach(btn => {
    btn.addEventListener('click', () => {
      const presetKey = btn.dataset.preset;
      if (PRESET_PROMPTS[presetKey]) {
        document.getElementById('textareaPrompt').value = PRESET_PROMPTS[presetKey];
      }
    });
  });

  // Barge-in slider
  const slider = document.getElementById('sliderVad');
  const sliderVal = document.getElementById('sliderVadVal');
  slider?.addEventListener('input', () => {
    sliderVal.textContent = `${Number(slider.value).toLocaleString()} RMS`;
  });

  // Toggle API Key visibility
  const toggleBtn = document.getElementById('btnToggleApiKey');
  const keyInput = document.getElementById('inputApiKey');
  toggleBtn?.addEventListener('click', () => {
    if (keyInput.type === 'password') {
      keyInput.type = 'text';
      toggleBtn.textContent = 'Hide';
    } else {
      keyInput.type = 'password';
      toggleBtn.textContent = 'Show';
    }
  });

  // Save Settings Button
  document.getElementById('btnSaveSettings')?.addEventListener('click', saveSettings);
}

async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    if (!res.ok) return;
    const data = await res.json();

    if (data.voice_name) {
      document.getElementById('selectVoice').value = data.voice_name;
    }
    if (data.system_instruction) {
      document.getElementById('textareaPrompt').value = data.system_instruction;
    }
    if (data.vad_threshold) {
      document.getElementById('sliderVad').value = data.vad_threshold;
      document.getElementById('sliderVadVal').textContent = `${Number(data.vad_threshold).toLocaleString()} RMS`;
      document.getElementById('vadThresholdDisplay').textContent = `${Number(data.vad_threshold).toLocaleString()} RMS`;
    }
    if (data.api_key_masked) {
      document.getElementById('inputApiKey').placeholder = `Current Key: ${data.api_key_masked}`;
    }
  } catch (e) {
    console.error('Failed to load settings:', e);
  }
}

async function saveSettings() {
  const apiKey = document.getElementById('inputApiKey').value.trim();
  const voiceName = document.getElementById('selectVoice').value;
  const prompt = document.getElementById('textareaPrompt').value.trim();
  const vad = parseInt(document.getElementById('sliderVad').value, 10);

  const payload = {
    voice_name: voiceName,
    system_instruction: prompt,
    vad_threshold: vad,
  };

  if (apiKey) {
    payload.gemini_api_key = apiKey;
  }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });

    if (res.ok) {
      const toast = document.getElementById('settingsSaveToast');
      toast.classList.remove('hidden');
      setTimeout(() => toast.classList.add('hidden'), 3000);
      loadSettings();
    } else {
      alert('Error saving settings');
    }
  } catch (e) {
    alert('Failed saving settings: ' + e);
  }
}

// ==========================================================================
// Utilities
// ==========================================================================

function formatDuration(sec) {
  if (!sec || isNaN(sec)) return '00:00';
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
