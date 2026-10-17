/** HTTP and reconnecting socket only. Never replays user chat requests. */
export async function request(path, options = {}) {
  const timeout = new AbortController();
  const timer = setTimeout(() => timeout.abort(), 15000);
  const external = options.signal;
  const abort = () => timeout.abort();
  external?.addEventListener('abort', abort, { once: true });
  if (external?.aborted) timeout.abort();
  try {
    const response = await fetch(path, {
      ...options,
      signal: timeout.signal,
      headers: { Accept: 'application/json', ...options.headers },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok)
      throw new Error(data.detail || data.message || `请求失败（${response.status}）`);
    return data;
  } catch (error) {
    if (error.name === 'AbortError' && !external?.aborted) throw new Error('请求超时，请稍后重试');
    throw error;
  } finally {
    clearTimeout(timer);
    external?.removeEventListener('abort', abort);
  }
}

export class ChatSocket {
  constructor({ onMessage, onConnection }) {
    this.onMessage = onMessage;
    this.onConnection = onConnection;
    this.attempts = 0;
    this.closed = false;
    this.socket = null;
    this.timer = null;
  }
  connect() {
    if (
      this.closed ||
      this.socket?.readyState === WebSocket.OPEN ||
      this.socket?.readyState === WebSocket.CONNECTING
    )
      return;
    clearTimeout(this.timer);
    this.onConnection('connecting');
    const socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws`,
    );
    this.socket = socket;
    socket.onopen = () => {
      if (this.socket !== socket || this.closed) return;
      this.attempts = 0;
      this.onConnection('online');
    };
    socket.onmessage = (event) => {
      if (this.socket !== socket || this.closed) return;
      try {
        this.onMessage(JSON.parse(event.data));
      } catch (error) {
        console.error('Invalid server event', error);
      }
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (this.socket !== socket || this.closed) return;
      this.onConnection('offline');
      this.timer = setTimeout(
        () => this.connect(),
        Math.min(1000 * 2 ** this.attempts++, 15000) + Math.random() * 300,
      );
    };
  }
  send(message) {
    if (this.socket?.readyState !== WebSocket.OPEN) return false;
    this.socket.send(JSON.stringify(message));
    return true;
  }
  close() {
    this.closed = true;
    clearTimeout(this.timer);
    this.socket?.close();
  }
}
