import { PCMEncoder } from "./audio.js";

class MicrophoneProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.level = 0;
    this.encoder = new PCMEncoder(sampleRate, (buffer) => {
      this.port.postMessage({ pcm: buffer, level: this.level }, [buffer]);
    });
  }

  process(inputs) {
    const samples = inputs[0]?.[0];
    if (samples) {
      let power = 0;
      for (const sample of samples) power += sample * sample;
      this.level = Math.sqrt(power / samples.length);
      this.encoder.push(samples);
    }
    return true;
  }
}

registerProcessor("microphone-pcm", MicrophoneProcessor);
