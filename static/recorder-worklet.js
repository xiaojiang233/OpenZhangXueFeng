/* Capture mono samples without running recording callbacks on the UI thread. */
class PCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Float32Array(4096);
    this.offset = 0;
  }
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input)
      for (const value of input) {
        this.chunk[this.offset++] = value;
        if (this.offset === this.chunk.length) {
          this.port.postMessage(this.chunk, [this.chunk.buffer]);
          this.chunk = new Float32Array(4096);
          this.offset = 0;
        }
      }
    return true;
  }
}
registerProcessor('pcm-recorder', PCMRecorder);
