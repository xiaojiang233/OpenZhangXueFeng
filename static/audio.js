/** Playback and microphone resources are owned here; callers own turn/session IDs. */
const AudioContextClass = () => window.AudioContext || window.webkitAudioContext;

export function encodeWAV(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const word = (offset, text) =>
    [...text].forEach((char, i) => view.setUint8(offset + i, char.charCodeAt(0)));
  word(0, 'RIFF');
  view.setUint32(4, 36 + samples.length * 2, true);
  word(8, 'WAVE');
  word(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  word(36, 'data');
  view.setUint32(40, samples.length * 2, true);
  samples.forEach((value, i) => {
    const clamped = Math.max(-1, Math.min(1, value));
    view.setInt16(44 + i * 2, clamped < 0 ? clamped * 32768 : clamped * 32767, true);
  });
  return new Blob([buffer], { type: 'audio/wav' });
}

export class AudioPlayer {
  constructor(onChange, onError) {
    this.onChange = onChange;
    this.onError = onError;
    this.context = null;
    this.queue = [];
    this.source = null;
    this.epoch = 0;
    this.playing = false;
  }
  async unlock() {
    if (!AudioContextClass()) throw new Error('浏览器不支持音频播放');
    this.context ||= new (AudioContextClass())();
    if (this.context.state === 'suspended') await this.context.resume();
    return this.context;
  }
  enqueue(packet) {
    if (!packet.data) return;
    this.queue.push(packet);
    this.onChange(true);
    this.next();
  }
  async next() {
    if (this.playing || !this.queue.length) return;
    this.playing = true;
    const epoch = this.epoch;
    const packet = this.queue.shift();
    try {
      const context = await this.unlock();
      if (epoch !== this.epoch) return;
      const raw = atob(packet.data);
      const bytes = Uint8Array.from(raw, (char) => char.charCodeAt(0));
      let buffer;
      if (!packet.format || packet.format === 'pcm') {
        const channels = Math.max(1, Math.min(2, Number(packet.channels) || 1));
        const frameCount = Math.floor(bytes.byteLength / (2 * channels));
        if (!frameCount) throw new Error('语音数据为空');
        buffer = context.createBuffer(channels, frameCount, Number(packet.sample_rate) || 44100);
        const view = new DataView(bytes.buffer);
        for (let c = 0; c < channels; c++) {
          const channel = buffer.getChannelData(c);
          for (let i = 0; i < frameCount; i++)
            channel[i] = view.getInt16((i * channels + c) * 2, true) / 32768;
        }
      } else buffer = await context.decodeAudioData(bytes.buffer);
      if (epoch !== this.epoch) return;
      const source = context.createBufferSource();
      source.buffer = buffer;
      source.connect(context.destination);
      this.source = source;
      source.onended = () => {
        source.disconnect();
        if (epoch !== this.epoch) return;
        this.source = null;
        this.playing = false;
        this.onChange(this.queue.length > 0);
        this.next();
      };
      source.start();
    } catch (error) {
      if (epoch !== this.epoch) return;
      this.playing = false;
      this.onError?.(error);
      this.onChange(this.queue.length > 0);
      this.next();
    }
  }
  stop() {
    this.epoch++;
    this.queue = [];
    if (this.source) {
      this.source.onended = null;
      try {
        this.source.stop();
        this.source.disconnect();
      } catch {}
    }
    this.source = null;
    this.playing = false;
    this.onChange(false);
  }
  dispose() {
    this.stop();
    this.context?.close();
    this.context = null;
  }
}

export class WaveRecorder {
  constructor(onLimit) {
    this.epoch = 0;
    this.pending = false;
    this.recording = false;
    this.onLimit = onLimit;
    this.samples = [];
    this.length = 0;
  }
  async start() {
    if (this.pending || this.recording) return false;
    if (!navigator.mediaDevices?.getUserMedia || !AudioContextClass())
      throw new Error('当前环境无法录音，请使用 HTTPS 或 localhost 并允许麦克风权限');
    const epoch = ++this.epoch;
    this.pending = true;
    let stream;
    let context;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      if (epoch !== this.epoch) {
        stream.getTracks().forEach((track) => track.stop());
        return false;
      }
      this.stream = stream;
      context = new (AudioContextClass())();
      this.context = context;
      await context.resume();
      if (epoch !== this.epoch) return false;
      this.rate = context.sampleRate;
      this.samples = [];
      this.length = 0;
      this.source = context.createMediaStreamSource(stream);
      const receive = (data) => {
        if (!this.recording || epoch !== this.epoch) return;
        this.samples.push(new Float32Array(data));
        this.length += data.length;
        if (this.length >= this.rate * 60) this.onLimit?.();
      };
      if (context.audioWorklet) {
        await context.audioWorklet.addModule('/static/recorder-worklet.js');
        if (epoch !== this.epoch) return false;
        this.processor = new AudioWorkletNode(context, 'pcm-recorder');
        this.processor.port.onmessage = (event) => receive(event.data);
      } else {
        this.processor = context.createScriptProcessor(4096, 1, 1);
        this.processor.onaudioprocess = (event) => receive(event.inputBuffer.getChannelData(0));
      }
      this.gain = context.createGain();
      this.gain.gain.value = 0;
      this.source.connect(this.processor);
      this.processor.connect(this.gain);
      this.gain.connect(context.destination);
      this.recording = true;
      return true;
    } catch (error) {
      if (epoch === this.epoch) this.stop(true);
      else {
        stream?.getTracks().forEach((track) => track.stop());
        context?.close().catch(() => {});
      }
      throw error;
    } finally {
      if (epoch === this.epoch) this.pending = false;
    }
  }
  stop(discard = false) {
    this.epoch++;
    this.pending = false;
    this.recording = false;
    this.stream?.getTracks().forEach((track) => track.stop());
    this.stream = null;
    if (this.processor?.port) this.processor.port.onmessage = null;
    if (this.processor) this.processor.onaudioprocess = null;
    [this.source, this.processor, this.gain].forEach((node) => {
      try {
        node?.disconnect();
      } catch {}
    });
    this.source = this.processor = this.gain = null;
    this.context?.close().catch(() => {});
    this.context = null;
    const length = this.length;
    const rate = this.rate;
    const chunks = this.samples;
    this.samples = [];
    this.length = 0;
    if (discard || !length) return null;
    const samples = new Float32Array(length);
    let offset = 0;
    chunks.forEach((chunk) => {
      samples.set(chunk, offset);
      offset += chunk.length;
    });
    return { blob: encodeWAV(samples, rate), duration: length / rate };
  }
}
