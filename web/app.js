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
  knowledgeDocs: [],
  knowledgeSearchTerm: '',
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
  terminalEntries: [],
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
  fetchStatusPolling();
  initTelemetryWebSocket();
  initMasterAudioPlayer();
  initRecordingsLibrary();
  initKnowledgeBase();
  initStudioMode();
  initOutboundDialer();
  initSettings();
  initTerminalConsole();
});

// ==========================================================================
// Navigation & Tabs
// ==========================================================================

function initTabs() {
  const tabs = document.querySelectorAll('.nav-tab');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      if (!tab.dataset.tab) return;
      const targetPaneId = 'pane' + tab.dataset.tab.charAt(0).toUpperCase() + tab.dataset.tab.slice(1);
      tabs.forEach(t => { if (t.dataset.tab) t.classList.remove('active'); });
      tab.classList.add('active');

      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      const targetPane = document.getElementById(targetPaneId);
      if (targetPane) targetPane.classList.add('active');

      appState.activeTab = tab.dataset.tab;
      if (appState.activeTab === 'recordings') {
        loadRecordings();
      } else if (appState.activeTab === 'knowledge') {
        loadKnowledgeDocuments();
      }
    });
  });

  const quitBtn = document.getElementById('btnQuitGateway');
  if (quitBtn) {
    quitBtn.addEventListener('click', async () => {
      if (confirm('Are you sure you want to stop and quit the Callink Gateway?')) {
        try {
          await fetch('/api/shutdown', { method: 'POST' });
        } catch (e) {}
        document.body.innerHTML = `
          <div style="display:flex;align-items:center;justify-content:center;height:100vh;background:#0b0f19;color:#e2e8f0;font-family:sans-serif;flex-direction:column;gap:16px;text-align:center;padding:20px;">
            <div style="width:50px;height:50px;border-radius:50%;background:rgba(239,68,68,0.15);border:1px solid #ef4444;display:flex;align-items:center;justify-content:center;font-size:24px;">✓</div>
            <h2 style="font-size:1.4rem;font-weight:700;">Callink Gateway Stopped</h2>
            <p style="color:#94a3b8;max-width:400px;font-size:0.9rem;">The background engine and telephony bridge have been safely shut down. You can safely close this browser window.</p>
          </div>
        `;
      }
    });
  }
}

// ==========================================================================
// Live Telemetry & WebSockets
// ==========================================================================

let pingTimer = null;

function initTelemetryWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws/telemetry`;

  function sendPing(ws) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'ping', t: performance.now() }));
    }
  }

  function connect() {
    const ws = new WebSocket(wsUrl);
    appState.telemetryWs = ws;

    ws.onopen = () => {
      console.log('[Telemetry WS] Connected to gateway');
      sendPing(ws);
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = setInterval(() => sendPing(ws), 1500);
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
      if (pingTimer) clearInterval(pingTimer);
      const latencyDisplay = document.getElementById('latencyDisplay');
      if (latencyDisplay) latencyDisplay.textContent = 'Reconnecting...';
      const statusPill = document.getElementById('statusPill');
      const statusText = document.getElementById('statusText');
      if (statusPill && statusText) {
        statusPill.className = 'status-pill status-standby';
        statusText.textContent = 'GATEWAY RECONNECTING...';
      }
      setTimeout(connect, 2000);
    };

    ws.onerror = (err) => {
      console.warn('[Telemetry WS] Socket error:', err);
      ws.close();
    };
  }

  connect();

  // Gentle fallback polling only when WebSocket is disconnected
  setInterval(() => {
    if (!appState.telemetryWs || appState.telemetryWs.readyState !== WebSocket.OPEN) {
      fetchStatusPolling();
    }
  }, 8000);
}

function handleTelemetryMessage(msg) {
  if (msg.type === 'state_update') {
    updateGatewayState(msg.state);
  } else if (msg.type === 'audio_levels' && msg.data) {
    renderAudioMeter('callerMeterBar', 'callerRmsVal', msg.data.caller_rms || 0);
    renderAudioMeter('aiMeterBar', 'aiRmsVal', msg.data.ai_rms || 0);
  } else if (msg.type === 'call_ringing') {
    const statusPill = document.getElementById('statusPill');
    const statusText = document.getElementById('statusText');
    if (statusPill && statusText) {
      statusPill.className = 'status-pill status-warning';
      statusText.textContent = 'INCOMING CALL RINGING — PRE-WARMING AI...';
    }
  } else if (msg.type === 'call_cancelled') {
    const statusPill = document.getElementById('statusPill');
    const statusText = document.getElementById('statusText');
    if (statusPill && statusText) {
      statusPill.className = 'status-pill status-standby';
      statusText.textContent = 'STANDBY — WAITING FOR CALL';
    }
  } else if (msg.type === 'pong') {
    const rtt = Math.max(1, Math.round(performance.now() - (msg.t || 0)));
    const latencyDisplay = document.getElementById('latencyDisplay');
    if (latencyDisplay) {
      latencyDisplay.textContent = `${rtt}ms RTT (Live Telemetry)`;
    }
  } else if (msg.type === 'call_started') {
    startLiveCallUI(msg.data.call_id, msg.data.call_type || 'gsm');
  } else if (msg.type === 'outbound_dialing' && msg.data) {
    showOutboundDialingBanner(msg.data.outbound);
  } else if (msg.type === 'outbound_cancelled') {
    hideOutboundDialingBanner();
  } else if (msg.type === 'call_ended') {
    endLiveCallUI(msg.data.metadata);
  } else if (msg.type === 'transcript_chunk' && msg.data) {
    appendLiveTranscriptChunk(msg.data.role, msg.data.text);
  } else if (msg.type === 'transcript_turn_complete' && msg.data) {
    finalizeLiveTranscriptTurn(msg.data.role, msg.data.text);
  } else if (msg.type === 'transcript' && msg.data) {
    appendLiveTranscript(msg.data.role, msg.data.text);
  } else if (msg.type === 'barge_in') {
    triggerBargeInFlash();
  } else if (msg.type === 'init_terminal_logs' && msg.entries) {
    renderInitTerminalLogs(msg.entries);
  } else if (msg.type === 'terminal_logs' && msg.entries) {
    appendTerminalLogs(msg.entries);
  } else if (msg.type === 'terminal_logs_cleared') {
    clearTerminalUI();
  } else if (msg.type === 'knowledge_updated' && msg.summary) {
    updateKnowledgeStats(msg.summary);
    if (appState.activeTab === 'knowledge') {
      loadKnowledgeDocuments();
    }
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

  if (state.knowledge_summary) {
    updateKnowledgeStats(state.knowledge_summary);
  }

  const statusPill = document.getElementById('statusPill');
  const statusText = document.getElementById('statusText');
  const deviceText = document.getElementById('deviceText');

  // Update Status Pill
  if (state.status === 'in_call') {
    statusPill.className = 'status-pill status-in-call';
    let typeName = 'CELLULAR GSM CALL';
    if (state.call_type === 'outbound_gsm') typeName = 'OUTBOUND GSM CALL';
    else if (state.call_type === 'outbound_browser') typeName = 'OUTBOUND STUDIO CALL';
    else if (state.call_type === 'browser') typeName = 'BROWSER STUDIO CALL';
    statusText.textContent = `${typeName} IN PROGRESS`;
    hideOutboundDialingBanner();
    if (!appState.callActive) {
      startLiveCallUI(state.call_id, state.call_type);
    }
  } else if (state.outbound_state) {
    statusPill.className = 'status-pill status-warning';
    const num = state.outbound_state.display_number || state.outbound_state.phone_number || '';
    const name = state.outbound_state.contact_name || '';
    statusText.textContent = `DIALING ${num} (${name})...`;
    showOutboundDialingBanner(state.outbound_state);
  } else {
    statusPill.className = 'status-pill status-standby';
    statusText.textContent = 'STANDBY — WAITING FOR CALL';
    hideOutboundDialingBanner();
    if (appState.callActive) {
      endLiveCallUI();
    }
  }

  // Device Pill (Top Nav)
  const devicePill = document.getElementById('devicePill');
  if (state.hfp_connected) {
    const alias = state.hfp_alias || '';
    deviceText.textContent = `Bluetooth HFP: Connected${alias ? ' (' + alias + ')' : ''}`;
    if (devicePill) {
      devicePill.className = 'device-pill device-connected';
    }
  } else {
    deviceText.textContent = 'Bluetooth HFP: Disconnected';
    if (devicePill) {
      devicePill.className = 'device-pill device-disconnected';
    }
  }

  // Dynamic Specs & Format Tags
  const audioFormatBadge = document.getElementById('audioFormatBadge');
  if (audioFormatBadge && state.audio_format) {
    audioFormatBadge.textContent = state.audio_format;
  }

  const callerChannelLabel = document.getElementById('callerChannelLabel');
  if (callerChannelLabel) {
    if (state.status === 'in_call' && state.call_type === 'browser') {
      callerChannelLabel.textContent = '🎙️ Browser Microphone (Channel L)';
    } else if (state.hfp_alias) {
      callerChannelLabel.textContent = `📞 Caller Mic [${state.hfp_alias}] (Channel L)`;
    } else {
      callerChannelLabel.textContent = '📞 Caller Mic (Channel L)';
    }
  }

  const aiChannelLabel = document.getElementById('aiChannelLabel');
  if (aiChannelLabel) {
    const voice = state.voice_name || 'Puck';
    aiChannelLabel.textContent = `🤖 Gemini AI Voice [${voice}] (Channel R)`;
  }

  const recMode = document.getElementById('recordingModeDisplay');
  if (recMode && state.recording_mode) {
    recMode.textContent = state.recording_mode;
  }

  const vadEl = document.getElementById('vadThresholdDisplay');
  if (vadEl && state.vad_threshold !== undefined) {
    vadEl.textContent = `${Number(state.vad_threshold).toLocaleString()} RMS`;
  }

  const platBadge = document.getElementById('hardwarePlatformBadge');
  if (platBadge && state.os_version) {
    platBadge.textContent = state.os_version;
  }

  // Update Hardware & Connectivity Card dynamically based on real hardware/API state
  const hfpIcon = document.getElementById('hfpIcon');
  const hfpDesc = document.getElementById('hfpDesc');
  if (hfpIcon && hfpDesc) {
    if (state.hfp_connected) {
      hfpIcon.textContent = '✓';
      hfpIcon.className = 'sys-icon active-icon';
      hfpDesc.textContent = `Active: ${state.hfp_input_name} (SCO Audio Bridge Ready)`;
    } else {
      hfpIcon.textContent = '✕';
      hfpIcon.className = 'sys-icon inactive-icon';
      hfpDesc.textContent = 'Not connected — Pair smartphone in Microsoft Phone Link';
    }
  }

  const geminiIcon = document.getElementById('geminiApiIcon');
  const geminiDesc = document.getElementById('geminiApiDesc');
  if (geminiIcon && geminiDesc) {
    if (state.api_key_configured) {
      geminiIcon.textContent = '✓';
      geminiIcon.className = 'sys-icon active-icon';
      geminiDesc.textContent = `${state.gemini_model || 'gemini-3.1-flash-live-preview'} • Voice: ${state.voice_name || 'Puck'} (API Key Active)`;
    } else {
      geminiIcon.textContent = '✕';
      geminiIcon.className = 'sys-icon inactive-icon';
      geminiDesc.textContent = 'Missing API Key — Configure in AI Persona tab';
    }
  }

  const phoneLinkIcon = document.getElementById('phoneLinkIcon');
  const phoneLinkDesc = document.getElementById('phoneLinkDesc');
  if (phoneLinkIcon && phoneLinkDesc) {
    if (state.phone_call_active) {
      phoneLinkIcon.textContent = '📞';
      phoneLinkIcon.className = 'sys-icon warning-icon';
      phoneLinkDesc.textContent = 'Cellular Call Active (SCO audio bridge engaged)';
    } else if (state.phone_call_incoming) {
      phoneLinkIcon.textContent = '🔔';
      phoneLinkIcon.className = 'sys-icon warning-icon';
      phoneLinkDesc.textContent = 'Incoming Cellular Call Ringing...';
    } else if (state.winrt_ready) {
      phoneLinkIcon.textContent = '✓';
      phoneLinkIcon.className = 'sys-icon active-icon';
      phoneLinkDesc.textContent = 'WinRT PhoneCallManager auto-detector active (Standby)';
    } else {
      phoneLinkIcon.textContent = '✕';
      phoneLinkIcon.className = 'sys-icon inactive-icon';
      phoneLinkDesc.textContent = 'WinRT PhoneCallManager unavailable';
    }
  }

  const stayAwakeIcon = document.getElementById('stayAwakeIcon');
  const stayAwakeDesc = document.getElementById('stayAwakeDesc');
  if (stayAwakeIcon && stayAwakeDesc) {
    if (state.stay_awake_active !== false) {
      stayAwakeIcon.textContent = '✓';
      stayAwakeIcon.className = 'sys-icon active-icon';
      stayAwakeDesc.textContent = 'Win32 SetThreadExecutionState prevents PC sleep';
    } else {
      stayAwakeIcon.textContent = '✕';
      stayAwakeIcon.className = 'sys-icon inactive-icon';
      stayAwakeDesc.textContent = 'Sleep prevention inactive';
    }
  }

  // Update recordings badge & count tag
  if (state.recordings_count !== undefined) {
    const badge = document.getElementById('recordingsBadge');
    if (badge) badge.textContent = state.recordings_count;
    const tag = document.getElementById('recordingsCountTag');
    if (tag) tag.textContent = `${state.recordings_count} Total`;
  }

  // VU Meters Audio Level animation
  renderAudioMeter('callerMeterBar', 'callerRmsVal', state.caller_rms || 0);
  renderAudioMeter('aiMeterBar', 'aiRmsVal', state.ai_rms || 0);

  // Restore live transcripts if available and box currently showing only placeholder
  if (state.live_transcripts && state.live_transcripts.length > 0) {
    const box = document.getElementById('liveTranscriptBox');
    if (box) {
      const placeholder = document.getElementById('transcriptPlaceholder');
      const bubbles = box.querySelectorAll('.chat-bubble');
      if (placeholder || bubbles.length === 0) {
        if (placeholder) placeholder.remove();
        state.live_transcripts.forEach(t => {
          appendLiveTranscript(t.role, t.text);
        });
      }
    }
  }
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
  finalizeLiveTranscriptTurn();

  hideOutboundDialingBanner();

  const banner = document.getElementById('liveCallBanner');
  banner.classList.remove('hidden');

  const title = document.getElementById('bannerCallTitle');
  const sub = document.getElementById('bannerCallSubtitle');
  if (callType === 'outbound_gsm') {
    title.textContent = 'OUTBOUND CELLULAR CALL IN PROGRESS';
    if (sub) sub.textContent = 'AI proactive voice agent connected (Speaking First with grounded business docs)';
  } else if (callType === 'outbound_browser') {
    title.textContent = 'OUTBOUND BROWSER STUDIO SESSION';
    if (sub) sub.textContent = 'Simulated outbound call — AI proactive agent speaking first';
  } else if (callType === 'browser') {
    title.textContent = 'BROWSER STUDIO CALL IN PROGRESS';
    if (sub) sub.textContent = 'Dual-track audio recording live to disk (Left: Mic | Right: AI)';
  } else {
    title.textContent = 'CELLULAR GSM CALL IN PROGRESS';
    if (sub) sub.textContent = 'Dual-track audio recording live to disk (Left: Caller | Right: AI)';
  }

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
  finalizeLiveTranscriptTurn();

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

let currentLiveBubble = null;
let currentLiveRole = null;
let currentLiveText = '';

function appendLiveTranscriptChunk(role, textChunk) {
  if (!textChunk) return;
  const container = document.getElementById('liveTranscriptBox');
  if (!container) return;
  const placeholder = document.getElementById('transcriptPlaceholder');
  if (placeholder) placeholder.remove();

  if (!currentLiveBubble || currentLiveRole !== role) {
    currentLiveRole = role;
    currentLiveText = textChunk;

    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role === 'model' ? 'bubble-ai' : 'bubble-caller'}`;

    const now = new Date();
    const timeStr = now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });

    bubble.innerHTML = `
      <div class="bubble-header">
        <span>${role === 'model' ? '🤖 Gemini AI' : '📞 Caller'}</span>
        <span class="bubble-time">${timeStr}</span>
      </div>
      <div class="bubble-text">${escapeHtml(textChunk)}</div>
    `;

    container.appendChild(bubble);
    currentLiveBubble = bubble;
  } else {
    currentLiveText += textChunk;
    const textEl = currentLiveBubble.querySelector('.bubble-text');
    if (textEl) {
      textEl.textContent = currentLiveText;
    }
  }
  container.scrollTop = container.scrollHeight;
}

function finalizeLiveTranscriptTurn(role, text) {
  if (currentLiveBubble && text) {
    const textEl = currentLiveBubble.querySelector('.bubble-text');
    if (textEl) {
      textEl.textContent = text;
    }
  }
  currentLiveBubble = null;
  currentLiveRole = null;
  currentLiveText = '';
}

function appendLiveTranscript(role, text) {
  if (!text) return;
  const container = document.getElementById('liveTranscriptBox');
  if (!container) return;
  const placeholder = document.getElementById('transcriptPlaceholder');
  if (placeholder) placeholder.remove();

  if (currentLiveBubble && currentLiveRole === role) {
    const textEl = currentLiveBubble.querySelector('.bubble-text');
    if (textEl) textEl.textContent = text;
    finalizeLiveTranscriptTurn(role, text);
    return;
  }

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
  finalizeLiveTranscriptTurn(role, text);
}

document.getElementById('btnClearLiveTranscript')?.addEventListener('click', () => {
  finalizeLiveTranscriptTurn();
  const box = document.getElementById('liveTranscriptBox');
  if (box) {
    box.innerHTML = `
      <div class="transcript-placeholder" id="transcriptPlaceholder">
        <div class="pulse-placeholder-dot"></div>
        Waiting for speech in active call...
      </div>
    `;
  }
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

    let typeBadgeClass = 'badge-gsm';
    let typeLabel = '📱 GSM Cellular';
    if (rec.call_type === 'outbound_gsm' || rec.direction === 'outbound') {
      typeBadgeClass = 'badge-outbound';
      const contact = rec.contact_name || rec.outbound_info?.contact_name || '';
      typeLabel = contact ? `📞 Outbound (${contact})` : '📞 Outbound GSM';
    } else if (rec.call_type === 'outbound_browser') {
      typeBadgeClass = 'badge-outbound';
      typeLabel = '🎙️ Outbound Studio';
    } else if (rec.call_type === 'browser') {
      typeBadgeClass = 'badge-browser';
      typeLabel = '💻 Browser Studio';
    }

    const goalSnippet = rec.call_goal || rec.outbound_info?.call_goal || '';

    card.innerHTML = `
      <div class="recording-card-header">
        <div class="rec-title-wrap">
          <h4>Call #${rec.call_id}</h4>
          <div class="rec-meta-row">
            <span>📅 ${dateStr} at ${timeStr}</span>
            <span>⏱️ ${durationStr}</span>
            <span>💾 ${sizeStr}</span>
            ${goalSnippet ? `<span>🎯 ${escapeHtml(goalSnippet)}</span>` : ''}
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

async function startBrowserVoiceCall(customWsUrl = null) {
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
    const wsUrl = customWsUrl || `${protocol}//${window.location.host}/ws/browser_call`;
    const ws = new WebSocket(wsUrl);
    appState.studioWs = ws;
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => {
      const isOutbound = wsUrl.includes('outbound=1');
      statusLabel.textContent = isOutbound
        ? 'Connected! AI is speaking first to initiate the call...'
        : 'Connected! Speak to the AI assistant...';
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
        if (msg.type === 'transcript_chunk') {
          appendStudioTranscriptChunk(msg.role, msg.text);
        } else if (msg.type === 'transcript_turn_complete') {
          finalizeStudioTranscriptTurn(msg.role, msg.text);
        } else if (msg.type === 'transcript') {
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

let currentStudioBubble = null;
let currentStudioRole = null;
let currentStudioText = '';

function appendStudioTranscriptChunk(role, textChunk) {
  if (!textChunk) return;
  const box = document.getElementById('studioTranscriptsBox');
  if (!box) return;
  const placeholder = box.querySelector('.transcript-placeholder');
  if (placeholder) placeholder.remove();

  if (!currentStudioBubble || currentStudioRole !== role) {
    currentStudioRole = role;
    currentStudioText = textChunk;

    const bubble = document.createElement('div');
    bubble.className = `chat-bubble ${role === 'model' ? 'bubble-ai' : 'bubble-caller'}`;
    bubble.innerHTML = `
      <div class="bubble-header">
        <span>${role === 'model' ? '🤖 Gemini AI' : '👤 You'}</span>
      </div>
      <div class="bubble-text">${escapeHtml(textChunk)}</div>
    `;
    box.appendChild(bubble);
    currentStudioBubble = bubble;
  } else {
    currentStudioText += textChunk;
    const textEl = currentStudioBubble.querySelector('.bubble-text');
    if (textEl) {
      textEl.textContent = currentStudioText;
    }
  }
  box.scrollTop = box.scrollHeight;
}

function finalizeStudioTranscriptTurn(role, text) {
  if (currentStudioBubble && text) {
    const textEl = currentStudioBubble.querySelector('.bubble-text');
    if (textEl) {
      textEl.textContent = text;
    }
  }
  currentStudioBubble = null;
  currentStudioRole = null;
  currentStudioText = '';
}

function appendStudioTranscript(role, text) {
  if (!text) return;
  const box = document.getElementById('studioTranscriptsBox');
  if (!box) return;
  const placeholder = box.querySelector('.transcript-placeholder');
  if (placeholder) placeholder.remove();

  if (currentStudioBubble && currentStudioRole === role) {
    const textEl = currentStudioBubble.querySelector('.bubble-text');
    if (textEl) textEl.textContent = text;
    finalizeStudioTranscriptTurn(role, text);
    return;
  }

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
  finalizeStudioTranscriptTurn(role, text);
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
    if (data.language) {
      const selectLang = document.getElementById('selectLanguage');
      if (selectLang) selectLang.value = data.language;
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
  const language = document.getElementById('selectLanguage')?.value || 'en';
  const prompt = document.getElementById('textareaPrompt').value.trim();
  const vad = parseInt(document.getElementById('sliderVad').value, 10);

  const payload = {
    voice_name: voiceName,
    language: language,
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

// ==========================================================================
// Live Terminal Console
// ==========================================================================

function initTerminalConsole() {
  const btnCopy = document.getElementById('btnCopyTerminal');
  const btnDownload = document.getElementById('btnDownloadTerminal');
  const btnClear = document.getElementById('btnClearTerminal');

  if (btnCopy) {
    btnCopy.addEventListener('click', () => {
      if (!appState.terminalEntries.length) return;
      const fullText = appState.terminalEntries.map(e => `[${e.time_str}] ${e.text}`).join('\n');
      navigator.clipboard.writeText(fullText).then(() => {
        const orig = btnCopy.innerHTML;
        btnCopy.innerHTML = '✓ Copied!';
        setTimeout(() => { btnCopy.innerHTML = orig; }, 2000);
      });
    });
  }

  if (btnDownload) {
    btnDownload.addEventListener('click', () => {
      if (!appState.terminalEntries.length) return;
      const fullText = appState.terminalEntries.map(e => `[${e.time_str}] ${e.text}`).join('\n');
      const blob = new Blob([fullText], { type: 'text/plain;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `callink_console_${new Date().toISOString().slice(0, 10)}.log`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    });
  }

  if (btnClear) {
    btnClear.addEventListener('click', async () => {
      try {
        await fetch('/api/logs/clear', { method: 'POST' });
      } catch (e) {}
      clearTerminalUI();
    });
  }
}

function formatTerminalText(rawText) {
  let escaped = escapeHtml(rawText);

  // Dividers
  if (/^={10,}/.test(escaped) || /^-{10,}/.test(escaped)) {
    return `<span class="term-tag-divider">${escaped}</span>`;
  }

  // Major section headers
  if (escaped.includes('&gt;&gt;&gt;') || escaped.includes('CALLINK:')) {
    escaped = `<span class="term-tag-header">${escaped}</span>`;
  }

  // Keywords & Tags
  escaped = escaped.replace(/(\[Telephony\][^—:]*)/g, '<span class="term-tag-telephony">$1</span>');
  escaped = escaped.replace(/(\[Pre-warm\])/g, '<span class="term-tag-prewarm">$1</span>');
  escaped = escaped.replace(/(\[Audio Link\])/g, '<span class="term-tag-audiolink">$1</span>');
  escaped = escaped.replace(/(\[Web Platform\])/g, '<span class="term-tag-gateway">$1</span>');
  escaped = escaped.replace(/(\[BARGE-IN\])/g, '<span class="term-tag-bargein">$1</span>');
  escaped = escaped.replace(/(\[Recorder\])/g, '<span class="term-tag-telephony">$1</span>');
  escaped = escaped.replace(/(\[Standby\]|Standby:)/g, '<span class="term-tag-audiolink">$1</span>');

  // Error / Warning highlights
  if (escaped.includes('Error') || escaped.includes('Traceback') || escaped.includes('Exception') || escaped.includes('Failed')) {
    escaped = `<span class="term-tag-error">${escaped}</span>`;
  } else if (escaped.includes('Warning') || escaped.includes('Retrying')) {
    escaped = `<span class="term-tag-warn">${escaped}</span>`;
  }

  return escaped;
}

function renderInitTerminalLogs(entries) {
  if (!Array.isArray(entries)) return;
  appState.terminalEntries = entries.slice(-1000);
  const screen = document.getElementById('terminalScreen');
  const badge = document.getElementById('terminalBadge');
  if (!screen) return;

  screen.innerHTML = '';
  const fragment = document.createDocumentFragment();
  appState.terminalEntries.forEach(entry => {
    const lineDiv = document.createElement('div');
    lineDiv.className = 'terminal-line';
    lineDiv.innerHTML = `<span class="term-time">${escapeHtml(entry.time_str || '')}</span><span class="term-text">${formatTerminalText(entry.text || '')}</span>`;
    fragment.appendChild(lineDiv);
  });
  screen.appendChild(fragment);

  if (badge) badge.textContent = appState.terminalEntries.length;

  const autoScroll = document.getElementById('chkTerminalAutoScroll');
  if (autoScroll && autoScroll.checked) {
    screen.scrollTop = screen.scrollHeight;
  }
}

function appendTerminalLogs(entries) {
  if (!Array.isArray(entries) || !entries.length) return;
  const screen = document.getElementById('terminalScreen');
  const badge = document.getElementById('terminalBadge');
  if (!screen) return;

  const fragment = document.createDocumentFragment();
  entries.forEach(entry => {
    appState.terminalEntries.push(entry);
    if (appState.terminalEntries.length > 2000) {
      appState.terminalEntries.shift();
    }
    const lineDiv = document.createElement('div');
    lineDiv.className = 'terminal-line';
    lineDiv.innerHTML = `<span class="term-time">${escapeHtml(entry.time_str || '')}</span><span class="term-text">${formatTerminalText(entry.text || '')}</span>`;
    fragment.appendChild(lineDiv);
  });
  screen.appendChild(fragment);

  while (screen.children.length > 2000) {
    screen.removeChild(screen.firstChild);
  }

  if (badge) badge.textContent = appState.terminalEntries.length;

  const autoScroll = document.getElementById('chkTerminalAutoScroll');
  if (autoScroll && autoScroll.checked) {
    screen.scrollTop = screen.scrollHeight;
  }
}

function clearTerminalUI() {
  appState.terminalEntries = [];
  const screen = document.getElementById('terminalScreen');
  const badge = document.getElementById('terminalBadge');
  if (screen) screen.innerHTML = '';
  if (badge) badge.textContent = '0';
}

// ==========================================================================
// Business Knowledge Base & Live Call Grounding
// ==========================================================================

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function formatFileSize(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function formatTimeAgo(isoString) {
  if (!isoString) return '';
  try {
    const d = new Date(isoString);
    return d.toLocaleDateString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return isoString;
  }
}

function initKnowledgeBase() {
  const dropzone = document.getElementById('knowledgeDropzone');
  const fileInput = document.getElementById('knowledgeFileInput');
  const searchInput = document.getElementById('knowledgeSearchInput');
  const btnRefresh = document.getElementById('btnRefreshKnowledge');
  const btnEnableAll = document.getElementById('btnEnableAllDocs');
  const btnDisableAll = document.getElementById('btnDisableAllDocs');
  const btnInspectPrompt = document.getElementById('btnInspectPrompt');

  // Modals
  const docModal = document.getElementById('docPreviewModal');
  const btnDocClose = document.getElementById('btnDocPreviewClose');
  const promptModal = document.getElementById('promptInspectorModal');
  const btnPromptClose = document.getElementById('btnPromptInspectorClose');
  const btnCopyDoc = document.getElementById('btnCopyDocText');
  const btnCopyPrompt = document.getElementById('btnCopyPromptInspector');

  // Drag & Drop
  if (dropzone && fileInput) {
    ['dragenter', 'dragover'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.add('dragover');
      });
    });

    ['dragleave', 'drop'].forEach(name => {
      dropzone.addEventListener(name, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('dragover');
      });
    });

    dropzone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        uploadKnowledgeFiles(dt.files);
      }
    });

    dropzone.addEventListener('click', (e) => {
      if (document.getElementById('knowledgeUploadProgress')?.classList.contains('hidden') === false) return;
      fileInput.click();
    });

    fileInput.addEventListener('change', (e) => {
      if (e.target.files && e.target.files.length) {
        uploadKnowledgeFiles(e.target.files);
      }
    });
  }

  // Search input
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      appState.knowledgeSearchTerm = e.target.value.toLowerCase().trim();
      filterAndRenderKnowledgeDocs();
    });
  }

  // Refresh
  if (btnRefresh) {
    btnRefresh.addEventListener('click', () => {
      loadKnowledgeDocuments();
    });
  }

  // Bulk actions
  if (btnEnableAll) {
    btnEnableAll.addEventListener('click', () => bulkToggleDocs(true));
  }
  if (btnDisableAll) {
    btnDisableAll.addEventListener('click', () => bulkToggleDocs(false));
  }

  // Inspect Prompt
  if (btnInspectPrompt) {
    btnInspectPrompt.addEventListener('click', openPromptInspector);
  }

  // Modal Closers
  if (btnDocClose) {
    btnDocClose.addEventListener('click', () => {
      docModal?.classList.add('hidden');
    });
  }
  if (btnPromptClose) {
    btnPromptClose.addEventListener('click', () => {
      promptModal?.classList.add('hidden');
    });
  }
  [docModal, promptModal].forEach(m => {
    if (m) {
      m.addEventListener('click', (e) => {
        if (e.target === m) m.classList.add('hidden');
      });
    }
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      docModal?.classList.add('hidden');
      promptModal?.classList.add('hidden');
    }
  });

  // Copy Buttons
  if (btnCopyDoc) {
    btnCopyDoc.addEventListener('click', () => {
      const text = document.getElementById('docPreviewContent')?.textContent || '';
      if (text) {
        navigator.clipboard.writeText(text).then(() => {
          const original = btnCopyDoc.innerHTML;
          btnCopyDoc.textContent = '✓ Copied!';
          setTimeout(() => { btnCopyDoc.innerHTML = original; }, 1800);
        });
      }
    });
  }

  if (btnCopyPrompt) {
    btnCopyPrompt.addEventListener('click', () => {
      const text = document.getElementById('promptInspectorContent')?.textContent || '';
      if (text) {
        navigator.clipboard.writeText(text).then(() => {
          const original = btnCopyPrompt.innerHTML;
          btnCopyPrompt.textContent = '✓ Copied!';
          setTimeout(() => { btnCopyPrompt.innerHTML = original; }, 1800);
        });
      }
    });
  }

  // Initial load
  loadKnowledgeDocuments();
}

async function uploadKnowledgeFiles(fileList) {
  const overlay = document.getElementById('knowledgeUploadProgress');
  const statusText = document.getElementById('uploadStatusText');
  const fileInput = document.getElementById('knowledgeFileInput');

  if (!fileList || !fileList.length) return;

  if (overlay) overlay.classList.remove('hidden');

  const files = Array.from(fileList);
  const formData = new FormData();
  let validFilesCount = 0;

  for (const file of files) {
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    const allowed = ['.pdf', '.docx', '.txt', '.md', '.csv', '.json'];
    if (!allowed.includes(ext)) {
      alert(`Unsupported file format: "${file.name}". Please upload PDF, DOCX, TXT, MD, CSV, or JSON.`);
      continue;
    }
    if (file.size > 20 * 1024 * 1024) {
      alert(`File "${file.name}" exceeds the 20MB limit.`);
      continue;
    }
    formData.append('file', file);
    validFilesCount++;
  }

  if (validFilesCount === 0) {
    if (overlay) overlay.classList.add('hidden');
    if (fileInput) fileInput.value = '';
    return;
  }

  if (statusText) {
    statusText.textContent = `Extracting & indexing ${validFilesCount} document${validFilesCount > 1 ? 's' : ''}...`;
  }

  try {
    const res = await fetch('/api/knowledge/upload', {
      method: 'POST',
      body: formData
    });

    const data = await res.json();
    if (res.ok) {
      await loadKnowledgeDocuments();
      if (data.failed && data.failed.length > 0) {
        const errors = data.failed.map(f => `${f.filename}: ${f.error}`).join('\n');
        alert(`Some documents failed to process:\n${errors}`);
      }
    } else {
      alert(`Upload failed: ${data.error || 'Server error'}`);
    }
  } catch (err) {
    console.error('Error uploading knowledge documents:', err);
    alert('Failed to upload document. Please check connection to gateway.');
  } finally {
    if (overlay) overlay.classList.add('hidden');
    if (fileInput) fileInput.value = '';
  }
}

async function loadKnowledgeDocuments() {
  try {
    const res = await fetch('/api/knowledge/documents');
    if (!res.ok) return;
    const data = await res.json();
    appState.knowledgeDocs = data.documents || [];
    updateKnowledgeStats(data.summary);
    filterAndRenderKnowledgeDocs();
  } catch (e) {
    console.error('Failed to load knowledge documents:', e);
  }
}

function updateKnowledgeStats(summary) {
  if (!summary) return;

  const totalDocs = summary.total_docs || 0;
  const activeDocs = summary.active_docs || 0;
  const totalWords = summary.total_words || 0;

  const badge = document.getElementById('knowledgeBadge');
  if (badge) badge.textContent = totalDocs;

  const activeCountTag = document.getElementById('knowledgeActiveCountTag');
  if (activeCountTag) {
    activeCountTag.textContent = `${activeDocs} Active / ${totalDocs} Total Docs`;
  }

  const statTotal = document.getElementById('statKnowledgeTotalDocs');
  if (statTotal) statTotal.textContent = totalDocs;

  const statActive = document.getElementById('statKnowledgeActiveDocs');
  if (statActive) statActive.textContent = activeDocs;

  const statActiveSub = document.getElementById('statKnowledgeActiveSub');
  if (statActiveSub) {
    statActiveSub.textContent = activeDocs > 0 ? 'Ready for live call grounding' : 'No active docs for live calls';
  }

  const statWords = document.getElementById('statKnowledgeTotalWords');
  if (statWords) statWords.textContent = totalWords.toLocaleString();

  const statWordsSub = document.getElementById('statKnowledgeWordsSub');
  if (statWordsSub) {
    const estTokens = Math.round(totalWords * 1.3);
    statWordsSub.textContent = `~${estTokens.toLocaleString()} tokens context`;
  }
}

function filterAndRenderKnowledgeDocs() {
  const grid = document.getElementById('knowledgeDocsGrid');
  const noBox = document.getElementById('noKnowledgeDocsBox');
  if (!grid) return;

  const term = appState.knowledgeSearchTerm || '';
  let filtered = appState.knowledgeDocs;

  if (term) {
    filtered = filtered.filter(doc => {
      const fn = (doc.filename || '').toLowerCase();
      const prev = (doc.preview || '').toLowerCase();
      return fn.includes(term) || prev.includes(term);
    });
  }

  if (filtered.length === 0) {
    grid.innerHTML = '';
    if (noBox) {
      if (term) {
        grid.innerHTML = `
          <div class="no-recordings" style="grid-column: 1 / -1;">
            <div class="empty-icon">🔍</div>
            <h3>No matching documents</h3>
            <p>No uploaded documents match "${escapeHtml(term)}".</p>
          </div>
        `;
      } else {
        grid.appendChild(noBox);
      }
    }
    return;
  }

  grid.innerHTML = '';

  filtered.forEach(doc => {
    const card = document.createElement('div');
    card.className = `knowledge-card ${doc.enabled ? '' : 'card-disabled'}`;

    const ext = (doc.file_type || '').toLowerCase().replace('.', '');
    let formatPillClass = 'format-txt';
    if (ext === 'pdf') formatPillClass = 'format-pdf';
    else if (ext === 'docx') formatPillClass = 'format-docx';
    else if (ext === 'md') formatPillClass = 'format-md';
    else if (ext === 'csv') formatPillClass = 'format-csv';
    else if (ext === 'json') formatPillClass = 'format-json';

    const safeTitle = escapeHtml(doc.filename);
    const safeSnippet = escapeHtml(doc.preview || 'No preview available.');
    const sizeStr = formatFileSize(doc.size_bytes || doc.file_size || 0);
    const wordsStr = (doc.word_count || 0).toLocaleString() + ' words';
    const timeStr = formatTimeAgo(doc.uploaded_at);

    card.innerHTML = `
      <div class="doc-card-top">
        <div class="doc-card-icon ${formatPillClass}">
          ${ext.toUpperCase()}
        </div>
        <div class="doc-card-body">
          <div class="doc-card-title" title="${safeTitle}">${safeTitle}</div>
          <div class="doc-card-meta">
            <span>${sizeStr}</span>
            <span class="meta-dot">•</span>
            <span>${wordsStr}</span>
            <span class="meta-dot">•</span>
            <span>${timeStr}</span>
          </div>
        </div>
      </div>

      <div class="doc-card-preview-snippet" title="Extracted document text preview">
        ${safeSnippet}
      </div>

      <div class="doc-card-bottom">
        <div class="toggle-grounding-wrap">
          <label class="switch">
            <input type="checkbox" class="doc-toggle-chk" ${doc.enabled ? 'checked' : ''} data-id="${doc.id}">
            <span class="slider"></span>
          </label>
          <span class="toggle-label ${doc.enabled ? 'active' : ''}">${doc.enabled ? 'Active in Calls' : 'Disabled'}</span>
        </div>

        <div class="doc-card-actions">
          <button class="btn-sm btn-ghost btn-doc-preview" data-id="${doc.id}" title="Preview full extracted text">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>
            Preview
          </button>
          <button class="btn-sm btn-ghost btn-icon-danger btn-doc-delete" data-id="${doc.id}" data-filename="${safeTitle}" title="Delete document">
            <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
          </button>
        </div>
      </div>
    `;

    // Event listeners
    const chk = card.querySelector('.doc-toggle-chk');
    chk?.addEventListener('change', (e) => {
      toggleKnowledgeDoc(doc.id, e.target.checked);
    });

    const previewBtn = card.querySelector('.btn-doc-preview');
    previewBtn?.addEventListener('click', () => {
      openDocPreview(doc.id);
    });

    const deleteBtn = card.querySelector('.btn-doc-delete');
    deleteBtn?.addEventListener('click', () => {
      deleteKnowledgeDoc(doc.id, doc.filename);
    });

    grid.appendChild(card);
  });
}

async function toggleKnowledgeDoc(docId, enabled) {
  try {
    const res = await fetch(`/api/knowledge/documents/${docId}/toggle`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled })
    });
    const data = await res.json();
    if (res.ok) {
      await loadKnowledgeDocuments();
    } else {
      alert(`Failed to toggle document: ${data.error || 'Server error'}`);
    }
  } catch (e) {
    console.error('Failed to toggle document:', e);
  }
}

async function bulkToggleDocs(enableState) {
  const docsToChange = appState.knowledgeDocs.filter(d => d.enabled !== enableState);
  if (!docsToChange.length) return;

  for (const doc of docsToChange) {
    try {
      await fetch(`/api/knowledge/documents/${doc.id}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: enableState })
      });
    } catch (e) {}
  }
  await loadKnowledgeDocuments();
}

async function deleteKnowledgeDoc(docId, filename) {
  if (!confirm(`Are you sure you want to delete "${filename}" from the Knowledge Base?`)) return;

  try {
    const res = await fetch(`/api/knowledge/documents/${docId}`, {
      method: 'DELETE'
    });
    const data = await res.json();
    if (res.ok) {
      await loadKnowledgeDocuments();
    } else {
      alert(`Failed to delete document: ${data.error || 'Server error'}`);
    }
  } catch (e) {
    console.error('Failed to delete document:', e);
    alert('Failed to delete document.');
  }
}

async function openDocPreview(docId) {
  const modal = document.getElementById('docPreviewModal');
  const title = document.getElementById('docPreviewTitle');
  const badge = document.getElementById('docPreviewBadge');
  const wordCount = document.getElementById('docPreviewWordCount');
  const size = document.getElementById('docPreviewSize');
  const status = document.getElementById('docPreviewStatus');
  const content = document.getElementById('docPreviewContent');

  if (content) content.textContent = 'Loading document preview...';
  if (modal) modal.classList.remove('hidden');

  try {
    const res = await fetch(`/api/knowledge/documents/${docId}/preview`);
    if (!res.ok) {
      if (content) content.textContent = 'Error loading document content.';
      return;
    }
    const data = await res.json();
    const doc = data.document || data;

    if (!doc || (!doc.text && !doc.preview && !doc.filename)) {
      if (content) content.textContent = 'Document content is unavailable.';
      return;
    }

    if (title) title.textContent = doc.filename || 'Document Preview';
    const ext = (doc.file_type || '').toLowerCase().replace('.', '');
    if (badge) {
      badge.textContent = ext.toUpperCase() || 'DOC';
      badge.className = `format-pill format-${ext}`;
    }
    const words = doc.word_count !== undefined ? doc.word_count : 0;
    if (wordCount) wordCount.textContent = `${words.toLocaleString()} words`;
    const bytes = doc.size_bytes || doc.file_size || 0;
    if (size) size.textContent = formatFileSize(bytes);
    if (status) {
      status.textContent = doc.enabled ? 'Active in Live Calls' : 'Disabled';
      status.className = `status-badge-inline ${doc.enabled ? 'active' : 'disabled'}`;
    }
    if (content) content.textContent = doc.text || doc.preview || '(Empty document)';
  } catch (e) {
    console.error('Error previewing document:', e);
    if (content) content.textContent = 'Failed to load preview: ' + (e.message || e);
  }
}

async function openPromptInspector() {
  const modal = document.getElementById('promptInspectorModal');
  const content = document.getElementById('promptInspectorContent');

  if (content) content.textContent = 'Generating live compiled prompt...';
  if (modal) modal.classList.remove('hidden');

  try {
    const res = await fetch('/api/knowledge/summary');
    if (res.ok) {
      const data = await res.json();
      if (data.effective_instruction) {
        if (content) content.textContent = data.effective_instruction;
        return;
      }
    }

    const settingsRes = await fetch('/api/settings');
    const settings = settingsRes.ok ? await settingsRes.json() : {};
    const basePrompt = settings.system_instruction || "You are a helpful AI voice assistant.";
    if (content) content.textContent = basePrompt;
  } catch (e) {
    console.error('Error opening prompt inspector:', e);
    if (content) content.textContent = 'Failed to load prompt preview.';
  }
}

// ==========================================================================
// Outbound Cellular & Voice Studio Dialer
// ==========================================================================

function showOutboundDialingBanner(outbound) {
  const banner = document.getElementById('outboundDialBanner');
  if (!banner) return;
  banner.classList.remove('hidden');

  const title = document.getElementById('bannerDialTitle');
  const sub = document.getElementById('bannerDialSubtitle');
  const name = outbound?.contact_name || 'Customer';
  const num = outbound?.display_number || outbound?.phone_number || '';
  if (title) title.textContent = `DIALING OUTBOUND CALL TO ${name.toUpperCase()}...`;
  if (sub) sub.textContent = `Number: ${num} • Objective: "${outbound?.call_goal || 'General Follow-Up'}" — AI is prepared to speak first upon connection!`;

  const statusPill = document.getElementById('statusPill');
  const statusText = document.getElementById('statusText');
  if (statusPill && statusText && !appState.callActive) {
    statusPill.className = 'status-pill status-warning';
    statusText.textContent = `DIALING ${num} (${name})...`;
  }
}

function hideOutboundDialingBanner() {
  const banner = document.getElementById('outboundDialBanner');
  if (banner) banner.classList.add('hidden');
}

function initOutboundDialer() {
  const form = document.getElementById('outboundDialForm');
  const phoneInput = document.getElementById('dialPhoneNumber');
  const nameInput = document.getElementById('dialContactName');
  const goalInput = document.getElementById('dialCallGoal');
  const notesInput = document.getElementById('dialLeadNotes');
  const statusNotice = document.getElementById('dialStatusNotice');
  const btnCancel = document.getElementById('btnCancelOutboundDial');
  const btnSimulate = document.getElementById('btnDialSimulateStudio');
  const presetChips = document.querySelectorAll('.preset-goal-chip');

  // Preset chips click
  presetChips.forEach(chip => {
    chip.addEventListener('click', () => {
      presetChips.forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
      if (goalInput) {
        goalInput.value = chip.dataset.goal || '';
        goalInput.focus();
      }
    });
  });

  // Dial Cellular form submission
  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const phone = phoneInput?.value?.trim();
    const name = nameInput?.value?.trim() || '';
    const goal = goalInput?.value?.trim() || '';
    const notes = notesInput?.value?.trim() || '';

    if (!phone) {
      showDialNotice('Please enter a valid phone number.', 'error');
      return;
    }

    showDialNotice('Triggering cellular dialer via Windows Phone Link...', 'success');

    try {
      const res = await fetch('/api/dial', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          phone_number: phone,
          contact_name: name,
          call_goal: goal,
          lead_notes: notes
        })
      });

      const data = await res.json();
      if (!res.ok || data.error) {
        showDialNotice(data.error || 'Failed to place call.', 'error');
        return;
      }

      showOutboundDialingBanner(data.outbound);
      showDialNotice(`Dialing placed! Windows Phone Link opened for ${phone}.`, 'success');
    } catch (err) {
      showDialNotice(`Error triggering dialer: ${err.message}`, 'error');
    }
  });

  // Cancel Dialing
  btnCancel?.addEventListener('click', async () => {
    try {
      await fetch('/api/dial/cancel', { method: 'POST' });
      hideOutboundDialingBanner();
      showDialNotice('Outbound call canceled.', 'success');
      fetchStatusPolling();
    } catch (err) {
      console.warn('Failed to cancel dial:', err);
    }
  });

  // Simulate in Studio
  btnSimulate?.addEventListener('click', () => {
    const phone = phoneInput?.value?.trim() || '+1 555-0199';
    const name = nameInput?.value?.trim() || 'Alex Johnson';
    const goal = goalInput?.value?.trim() || 'Follow up on business inquiry and qualify project requirements';
    const notes = notesInput?.value?.trim() || '';

    // Switch to Voice Studio tab
    const tabStudio = document.getElementById('tabBtnStudio');
    if (tabStudio) tabStudio.click();

    // Build websocket URL with outbound query parameters
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/browser_call?outbound=1&name=${encodeURIComponent(name)}&goal=${encodeURIComponent(goal)}&notes=${encodeURIComponent(notes)}&phone=${encodeURIComponent(phone)}`;

    // Start studio call
    startBrowserVoiceCall(wsUrl);
  });

  function showDialNotice(text, type = 'success') {
    if (!statusNotice) return;
    statusNotice.textContent = text;
    statusNotice.className = `dial-status-notice ${type}`;
    statusNotice.classList.remove('hidden');
    setTimeout(() => {
      if (statusNotice) statusNotice.classList.add('hidden');
    }, 5000);
  }
}


