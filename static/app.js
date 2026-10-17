import { hydrateIcons } from './icons.js';
import { ChatSocket, request } from './transport.js';
import { AudioPlayer, WaveRecorder } from './audio.js';
import { dom, ConversationView, renderConversations, toast } from './renderer.js';
import { initApiSettings } from './settings.js';

/** One controller owns conversation, request and microphone lifecycles. */
const state = {
  connected: false,
  syncing: false,
  activeId: null,
  viewRequest: null,
  turn: null,
  conversations: [],
  health: null,
  voice: false,
  phase: '',
  disposed: false,
};
const call = { active: false, epoch: 0, recognizing: false, abort: null, timer: null, start: 0 };
const id = () =>
  globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`;
const view = new ConversationView();
const player = new AudioPlayer(
  (playing) => {
    dom.audioIndicator.hidden = !playing;
  },
  () => toast('语音播放失败，文字回复仍可阅读', true),
);
const recorder = new WaveRecorder(() => {
  if (call.active && recorder.recording) stopRecording();
});
let listSequence = 0;
let healthSequence = 0;
let deleteTarget = null;
let composing = false;
const socket = new ChatSocket({ onMessage: handleMessage, onConnection: connectionChanged });

function renderSidebar() {
  renderConversations(
    state.conversations,
    state.activeId,
    dom.historySearch.value,
    loadConversation,
    askDelete,
  );
}
function chatAvailable() {
  return state.health?.services?.chat !== false;
}
function voiceAvailable() {
  return state.health?.services?.voice !== false;
}
function ready() {
  return state.connected && !state.syncing;
}
function busy() {
  return Boolean(state.turn && !state.turn.done);
}

function updateUI() {
  const processing = busy();
  dom.btnSend.hidden = processing;
  dom.btnStop.hidden = !processing;
  dom.btnSend.disabled = !ready() || !chatAvailable() || !dom.messageInput.value.trim();
  dom.btnVoiceToggle.disabled = !voiceAvailable();
  dom.btnVoiceToggle.setAttribute('aria-pressed', String(state.voice));
  dom.btnVoiceToggle.title = voiceAvailable()
    ? state.voice
      ? '关闭语音回复'
      : '开启语音回复'
    : '管理员配置语音服务后可用';
  dom.btnCall.disabled = !ready() || !chatAvailable() || !voiceAvailable() || processing;
  dom.btnCall.title = !voiceAvailable() ? '管理员配置语音服务后可用' : '开始语音通话';
  dom.modeLabel.textContent = state.voice ? '文字 + 语音回复' : '文字咨询';
  dom.charCounter.textContent = `${dom.messageInput.value.length} / ${dom.messageInput.maxLength}`;
  dom.messages.setAttribute('aria-busy', String(processing));
  dom.typingIndicator.hidden = !processing;
  dom.typingText.textContent =
    state.phase === 'searching'
      ? '正在查找相关资料…'
      : state.phase === 'speaking'
        ? '正在生成语音回复…'
        : state.turn?.response
          ? '正在继续整理回答…'
          : '正在整理思路…';
  dom.statusBadge.dataset.state = state.connected
    ? chatAvailable()
      ? 'online'
      : 'pending'
    : 'offline';
  dom.statusText.textContent = !state.connected
    ? '正在重连'
    : state.syncing
      ? '正在同步'
      : !chatAvailable()
        ? '等待配置'
        : processing
          ? state.phase === 'searching'
            ? '检索中'
            : state.phase === 'speaking'
              ? '语音生成中'
              : '思考中'
          : '已连接';
  updateCallUI();
}

function resizeInput() {
  dom.messageInput.style.height = 'auto';
  dom.messageInput.style.height = `${Math.max(38, Math.min(dom.messageInput.scrollHeight, 140))}px`;
  updateUI();
}
function setSidebar(open, returnFocus = false) {
  dom.sidebar.classList.toggle('open', open);
  dom.sidebarScrim.hidden = !open;
  dom.btnMenu.setAttribute('aria-expanded', String(open));
  const mobile = matchMedia('(max-width: 900px)').matches;
  dom.sidebar.inert = mobile && !open;
  dom.mainArea?.classList?.toggle('drawer-open', open);
  if (open && mobile) dom.btnNewChat.focus();
  else if (returnFocus) dom.btnMenu.focus();
}

async function refreshConversations() {
  const sequence = ++listSequence;
  try {
    const data = await request('/api/conversations');
    if (sequence !== listSequence || state.disposed) return;
    state.conversations = Array.isArray(data) ? data : [];
    renderSidebar();
  } catch {
    if (!state.disposed) toast('历史对话暂时无法载入，可以稍后重新检查服务', true);
  }
}

async function refreshHealth(showFeedback = false) {
  const sequence = ++healthSequence;
  dom.btnRefreshHealth.disabled = true;
  try {
    const data = await request('/api/health');
    if (sequence !== healthSequence || state.disposed) return;
    state.health = data;
    const services = data.services || {};
    const missing = [];
    if (services.chat === false) missing.push('咨询服务尚未配置，点击“配置 API”连接你的模型服务。');
    else if (services.voice === false)
      missing.push('文字咨询已就绪，配置语音服务后可开启语音回复与通话。');
    else if (services.search === false)
      missing.push('咨询服务已就绪，联网检索暂未配置。涉及招生政策时请自行核实官方信息。');
    dom.serviceNotice.hidden = !missing.length;
    dom.serviceNoticeText.textContent = missing.join(' ');
    const details = [
      services.chat === false ? '文字：待配置（DEEPSEEK_API_KEY）' : '文字：可用',
      services.voice === false
        ? '语音：待配置（FISH_AUDIO_API_KEY / FISH_AUDIO_VOICE_ID）'
        : '语音：可用',
      services.search === false ? '联网检索：待配置（LANGSEARCH_API_KEY）' : '联网检索：可用',
    ];
    dom.helpService.textContent = `${details.join('；')}。可在侧栏“API 设置”中配置，保存后立即生效。`;
    if (!voiceAvailable()) state.voice = false;
    if (!voiceAvailable() || !chatAvailable()) closeCall();
    if (Number.isInteger(data.limits?.message_chars) && data.limits.message_chars > 0)
      dom.messageInput.maxLength = data.limits.message_chars;
    if (showFeedback)
      toast(services.chat === false ? '咨询服务仍需管理员完成配置' : '服务状态已更新');
  } catch {
    if (sequence !== healthSequence || state.disposed) return;
    dom.serviceNotice.hidden = false;
    dom.serviceNoticeText.textContent = '暂时无法检查服务状态，请确认服务已启动后重试。';
    dom.helpService.textContent = '暂时无法检查服务状态，请确认服务已启动后重试。';
    if (showFeedback) toast('无法获取服务状态，请稍后重试', true);
  } finally {
    if (sequence === healthSequence) {
      dom.btnRefreshHealth.disabled = false;
      updateUI();
    }
  }
}

function connectionChanged(connection) {
  if (state.disposed) return;
  if (connection === 'online') {
    state.connected = true;
    refreshConversations();
    refreshHealth();
    if (state.activeId) loadConversation(state.activeId, true);
    else {
      state.syncing = false;
      state.viewRequest = null;
    }
  } else {
    const wasConnected = state.connected;
    state.connected = false;
    state.syncing = false;
    interruptTurn(false);
    closeCall();
    if (wasConnected) toast('连接已中断，正在自动重连。未发送的草稿已保留。', true);
  }
  updateUI();
}

function interruptTurn(notify = true) {
  const turn = state.turn;
  state.turn = null;
  state.phase = '';
  player.stop();
  if (turn && !turn.done) {
    if (notify) socket.send({ type: 'stop', request_id: turn.id });
    if (turn.response) {
      turn.response.streaming = false;
      turn.response.interrupted = true;
      view.update(turn.response);
    }
  }
  updateUI();
}

function loadConversation(convId, reconnect = false) {
  if (!reconnect) closeCall();
  interruptTurn();
  state.activeId = convId;
  state.viewRequest = id();
  state.syncing = true;
  state.phase = '';
  if (!reconnect) view.reset();
  const sent = socket.send({ type: 'load_conv', conv_id: convId, request_id: state.viewRequest });
  if (!sent) {
    state.syncing = false;
    toast('正在重新连接，恢复连接后会自动载入对话');
  }
  renderSidebar();
  setSidebar(false);
  updateUI();
}

function newConversation() {
  closeCall();
  interruptTurn();
  state.activeId = null;
  state.viewRequest = id();
  state.syncing = state.connected;
  view.reset();
  socket.send({ type: 'new_conv', request_id: state.viewRequest });
  renderSidebar();
  setSidebar(false);
  updateUI();
  dom.messageInput.focus();
}

function askDelete(conversation) {
  deleteTarget = conversation;
  dom.deleteDescription.textContent = `“${conversation.title || '新对话'}”将被永久删除，无法恢复。`;
  dom.deleteDialog.showModal();
  dom.btnDeleteCancel.focus();
}
async function confirmDelete() {
  if (!deleteTarget) return;
  const target = deleteTarget;
  dom.btnDeleteConfirm.disabled = true;
  try {
    await request(`/api/conversations/${encodeURIComponent(target.id)}`, { method: 'DELETE' });
    if (state.activeId === target.id) newConversation();
    state.conversations = state.conversations.filter((item) => item.id !== target.id);
    renderSidebar();
    dom.deleteDialog.close();
    deleteTarget = null;
    toast('对话已删除');
  } catch (error) {
    toast(error.message || '删除失败，请稍后重试', true);
  } finally {
    dom.btnDeleteConfirm.disabled = false;
  }
}

function sendChat(text, fromCall = false) {
  text = String(text).trim();
  if (!text || busy()) return false;
  if (!ready()) {
    toast('连接恢复并完成同步后即可发送', true);
    return false;
  }
  if (!chatAvailable()) {
    toast('咨询服务尚未配置，请联系管理员', true);
    return false;
  }
  if (text.length > dom.messageInput.maxLength) {
    toast(`单条消息最多 ${dom.messageInput.maxLength} 个字符`, true);
    return false;
  }
  const requestId = id();
  const useVoice = fromCall || state.voice;
  if (
    !socket.send({ type: 'chat', text, mode: useVoice ? 'voice' : 'text', request_id: requestId })
  ) {
    toast('连接已中断，问题尚未发送', true);
    return false;
  }
  player.stop();
  state.turn = { id: requestId, convId: state.activeId, response: null, done: false, fromCall };
  state.phase = 'thinking';
  view.append({ role: 'user', content: text });
  view.scroll(true);
  if (!fromCall) {
    dom.messageInput.value = '';
    resizeInput();
  } else {
    dom.callUserText.textContent = `“${text}”`;
    dom.callAssistantText.textContent = '';
    dom.callStatus.textContent = '正在整理回复…';
  }
  if (useVoice) player.unlock().catch(() => toast('请允许浏览器播放声音，文字回复仍可阅读', true));
  updateUI();
  return true;
}

function matchesTurn(message) {
  const turn = state.turn;
  if (!turn) return false;
  if (message.request_id && message.request_id !== turn.id) return false;
  if (message.conv_id && turn.convId && message.conv_id !== turn.convId) return false;
  return true;
}

function handleMessage(message) {
  if (!message || typeof message.type !== 'string' || state.disposed) return;
  if (message.type === 'config_updated') {
    refreshHealth();
    return;
  }
  if (message.type === 'conv_list') {
    ++listSequence;
    state.conversations = Array.isArray(message.conversations) ? message.conversations : [];
    renderSidebar();
    return;
  }
  if (message.type === 'history') {
    // Unsolicited empty history is a deletion from another window or REST.
    if (!message.request_id && message.conv_id === null && state.activeId && !state.syncing) {
      closeCall();
      interruptTurn(false);
      state.activeId = null;
      view.reset();
      refreshConversations();
      updateUI();
      return;
    }
    if (!state.viewRequest || (message.request_id && message.request_id !== state.viewRequest))
      return;
    if (message.conv_id !== state.activeId) return;
    state.viewRequest = null;
    state.syncing = false;
    state.turn = null;
    view.reset(
      (message.messages || [])
        .filter((item) => item.role === 'user' || item.role === 'assistant')
        .map((item) => ({ ...item, streaming: false })),
    );
    renderSidebar();
    updateUI();
    return;
  }
  if (message.type === 'error' && state.viewRequest && message.request_id === state.viewRequest) {
    state.viewRequest = null;
    state.syncing = false;
    if (message.code === 'not_found') {
      newConversation();
      refreshConversations();
    }
    toast(message.message || '无法载入这段对话', true);
    updateUI();
    return;
  }
  if (!matchesTurn(message)) return;
  const turn = state.turn;
  if (message.type === 'conv_active') {
    state.activeId = message.conv_id;
    turn.convId = message.conv_id;
    renderSidebar();
    refreshConversations();
    return;
  }
  if (turn.done && message.type !== 'audio') return;
  if (message.type === 'text_chunk' || message.type === 'text') {
    if (typeof message.content !== 'string') return;
    if (!turn.response) {
      turn.response = {
        role: 'assistant',
        content: '',
        citations: [],
        display_citations: [],
        streaming: true,
      };
      view.append(turn.response);
    }
    turn.response.content += message.content;
    view.update(turn.response);
    if (call.active && turn.fromCall) {
      dom.callAssistantText.textContent = turn.response.content;
      dom.callAssistantText.scrollTop = dom.callAssistantText.scrollHeight;
    }
  } else if (message.type === 'citations') {
    if (turn.response) {
      turn.response.citations = message.full || message.data || [];
      turn.response.display_citations = message.data || [];
      view.update(turn.response);
    }
  } else if (message.type === 'audio') {
    if (turn.fromCall || state.voice) player.enqueue(message);
  } else if (message.type === 'status') {
    state.phase = message.status;
    if (message.status === 'done') {
      turn.done = true;
      if (turn.response) {
        turn.response.streaming = false;
        turn.response.interrupted = Boolean(message.cancelled);
        view.update(turn.response);
      }
      if (call.active && turn.fromCall)
        dom.callStatus.textContent = message.cancelled
          ? '回复已停止，点击麦克风继续'
          : '回复已完成，点击麦克风继续聊';
      refreshConversations();
    }
  } else if (message.type === 'error') {
    if (turn.response) {
      turn.response.streaming = false;
      view.update(turn.response);
    }
    turn.done = true;
    player.stop();
    view.append({
      role: 'assistant',
      content: message.message || '服务暂时不可用，请稍后再试。',
      error: true,
    });
    if (call.active) dom.callStatus.textContent = message.message || '服务暂时不可用，请稍后再试';
    if (message.code === 'chat_unavailable' || message.code === 'voice_unavailable')
      refreshHealth();
  }
  updateUI();
}

function updateCallUI() {
  dom.btnCallMic.setAttribute('aria-pressed', String(recorder.recording));
  dom.btnCallMic.setAttribute('aria-label', recorder.recording ? '结束录音并发送' : '开始录音');
  dom.callAvatar.classList.toggle('recording', recorder.recording);
  dom.btnCallMic.disabled = recorder.pending || call.recognizing || busy();
}
function openCall() {
  if (!ready() || busy() || !chatAvailable() || !voiceAvailable()) return;
  call.active = true;
  call.epoch++;
  call.start = Date.now();
  dom.callTimer.textContent = '00:00';
  dom.callStatus.textContent = '点击麦克风开始说话，再次点击结束录音';
  dom.callUserText.textContent = '';
  dom.callAssistantText.textContent = '';
  dom.callDialog.showModal();
  player.unlock().catch(() => {});
  call.timer = setInterval(() => {
    const seconds = Math.floor((Date.now() - call.start) / 1000);
    dom.callTimer.textContent = `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
  }, 1000);
  updateCallUI();
  dom.btnCallMic.focus();
}
function closeCall() {
  if (!call.active && !recorder.pending && !recorder.recording) return;
  call.active = false;
  call.epoch++;
  call.abort?.abort();
  call.abort = null;
  call.recognizing = false;
  recorder.stop(true);
  clearInterval(call.timer);
  call.timer = null;
  if (state.turn?.fromCall && !state.turn.done) interruptTurn();
  else player.stop();
  if (dom.callDialog.open) dom.callDialog.close();
  updateCallUI();
}
async function startRecording() {
  if (!call.active || busy() || call.recognizing || recorder.pending) return;
  const epoch = call.epoch;
  player.stop();
  dom.callStatus.textContent = '正在开启麦克风…';
  const starting = recorder.start();
  updateCallUI();
  try {
    const started = await starting;
    if (epoch !== call.epoch || !call.active) return;
    if (started) {
      dom.callUserText.textContent = '';
      dom.callAssistantText.textContent = '';
      dom.callStatus.textContent = '正在听…说完后再次点击麦克风（最长 60 秒）';
    }
  } catch (error) {
    if (epoch !== call.epoch || !call.active) return;
    dom.callStatus.textContent =
      error.name === 'NotAllowedError'
        ? '请允许麦克风权限，然后再次点击重试'
        : error.name === 'NotFoundError'
          ? '未检测到麦克风，请连接设备后重试'
          : error.message || '麦克风暂时无法使用，请稍后重试';
  } finally {
    if (epoch === call.epoch) updateCallUI();
  }
}
async function stopRecording() {
  if (!call.active || !recorder.recording) return;
  const epoch = call.epoch;
  const recording = recorder.stop();
  if (!recording || recording.duration < 0.3) {
    dom.callStatus.textContent = '录音有点短，再点击麦克风试一次吧';
    updateCallUI();
    return;
  }
  call.recognizing = true;
  call.abort = new AbortController();
  dom.callStatus.textContent = '正在识别你说的话…';
  updateCallUI();
  const form = new FormData();
  form.append('audio', recording.blob, 'recording.wav');
  try {
    const result = await request('/api/asr', {
      method: 'POST',
      body: form,
      signal: call.abort.signal,
    });
    if (epoch !== call.epoch || !call.active) return;
    const text = typeof result.text === 'string' ? result.text.trim() : '';
    if (!text) dom.callStatus.textContent = '没有听清，点击麦克风再试一次吧';
    else if (!sendChat(text, true))
      dom.callStatus.textContent = '暂时无法发送，请确认连接状态后重试';
  } catch (error) {
    if (epoch === call.epoch && call.active && error.name !== 'AbortError')
      dom.callStatus.textContent = error.message || '识别失败，点击麦克风重试';
  } finally {
    if (epoch === call.epoch) {
      call.recognizing = false;
      call.abort = null;
      updateCallUI();
    }
  }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  const label = theme === 'dark' ? '切换浅色模式' : '切换深色模式';
  dom.btnThemeToggle.title = label;
  dom.btnThemeToggle.setAttribute('aria-label', label);
}

hydrateIcons();
applyTheme(document.documentElement.dataset.theme);
dom.composerForm.addEventListener('submit', (event) => {
  event.preventDefault();
  sendChat(dom.messageInput.value);
});
dom.messageInput.addEventListener('compositionstart', () => {
  composing = true;
});
dom.messageInput.addEventListener('compositionend', () => {
  composing = false;
});
dom.messageInput.addEventListener('keydown', (event) => {
  if (
    event.key === 'Enter' &&
    !event.shiftKey &&
    !event.isComposing &&
    !composing &&
    event.keyCode !== 229
  ) {
    event.preventDefault();
    sendChat(dom.messageInput.value);
  }
});
dom.messageInput.addEventListener('input', resizeInput);
dom.btnStop.addEventListener('click', () => {
  interruptTurn();
  toast('已停止生成，已有内容会保留');
});
dom.btnNewChat.addEventListener('click', newConversation);
dom.historySearch.addEventListener('input', renderSidebar);
dom.btnVoiceToggle.addEventListener('click', () => {
  state.voice = !state.voice;
  if (state.voice) player.unlock().catch(() => toast('请允许浏览器播放声音', true));
  else player.stop();
  updateUI();
});
dom.btnCall.addEventListener('click', openCall);
dom.btnCallEnd.addEventListener('click', closeCall);
dom.btnCallHangup.addEventListener('click', closeCall);
dom.btnCallMic.addEventListener('click', () =>
  recorder.recording ? stopRecording() : startRecording(),
);
dom.callDialog.addEventListener('cancel', (event) => {
  event.preventDefault();
  closeCall();
});
dom.btnMenu.addEventListener('click', () => setSidebar(!dom.sidebar.classList.contains('open')));
dom.sidebarScrim.addEventListener('click', () => setSidebar(false, true));
dom.btnThemeToggle.addEventListener('click', () => {
  const theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyTheme(theme);
  try {
    localStorage.setItem('ozxf-theme', theme);
  } catch {}
});
dom.btnHelp.addEventListener('click', () => {
  setSidebar(false);
  dom.helpDialog.showModal();
});
dom.btnHelpClose.addEventListener('click', () => dom.helpDialog.close());
dom.btnDeleteCancel.addEventListener('click', () => dom.deleteDialog.close());
dom.btnDeleteConfirm.addEventListener('click', confirmDelete);
dom.deleteDialog.addEventListener('close', () => {
  deleteTarget = null;
});
dom.btnRefreshHealth.addEventListener('click', () => {
  refreshHealth(true);
  refreshConversations();
});
dom.audioIndicator.addEventListener('click', () => player.stop());
document.querySelectorAll('.prompt-card').forEach((button) =>
  button.addEventListener('click', () => {
    dom.messageInput.value = button.dataset.text;
    resizeInput();
    dom.messageInput.focus();
  }),
);
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && dom.sidebar.classList.contains('open')) setSidebar(false, true);
  if (
    event.key.toLowerCase() === 'n' &&
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    !document.querySelector('dialog[open]')
  ) {
    event.preventDefault();
    newConversation();
  }
  if (
    event.key === 'Tab' &&
    dom.sidebar.classList.contains('open') &&
    matchMedia('(max-width: 900px)').matches
  ) {
    const focusable = [...dom.sidebar.querySelectorAll('a,button,input')].filter(
      (el) => !el.disabled && !el.hidden,
    );
    const first = focusable[0],
      last = focusable.at(-1);
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
});
matchMedia('(max-width: 900px)').addEventListener('change', () => setSidebar(false));
window.addEventListener('online', () => {
  socket.connect();
  refreshHealth();
});
window.addEventListener('pagehide', () => {
  state.disposed = true;
  closeCall();
  socket.close();
  player.dispose();
});
initApiSettings({ onSaved: () => refreshHealth(), closeSidebar: () => setSidebar(false) });
setSidebar(false);
renderSidebar();
updateUI();
refreshHealth();
socket.connect();
