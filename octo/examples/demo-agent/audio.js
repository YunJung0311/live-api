// Stateful area averaging keeps 44.1/48 kHz input continuous across worklet blocks.
export class PCMEncoder {
  constructor(inputRate, onChunk, chunkSize = 512) {
    this.ratio = inputRate / 16000;
    this.onChunk = onChunk;
    this.chunkSize = chunkSize;
    this.packet = new ArrayBuffer(chunkSize * 2);
    this.view = new DataView(this.packet);
    this.count = 0;
    this.area = 0;
    this.weight = 0;
  }

  push(samples) {
    for (const sample of samples) {
      let remaining = 1;
      while (remaining > 1e-9) {
        const take = Math.min(remaining, this.ratio - this.weight);
        this.area += sample * take;
        this.weight += take;
        remaining -= take;
        if (this.weight >= this.ratio - 1e-9) {
          const value = Math.max(-1, Math.min(1, this.area / this.weight));
          this.view.setInt16(
            this.count * 2,
            Math.round(value * (value < 0 ? 32768 : 32767)),
            true,
          );
          this.count++;
          this.area = 0;
          this.weight = 0;
          if (this.count === this.chunkSize) {
            this.onChunk(this.packet);
            this.packet = new ArrayBuffer(this.chunkSize * 2);
            this.view = new DataView(this.packet);
            this.count = 0;
          }
        }
      }
    }
  }
}

export class AudioPlayer {
  constructor(context, onSpeaking) {
    this.context = context;
    this.onSpeaking = onSpeaking;
    this.sources = new Set();
    this.nextTime = 0;
  }

  get speaking() {
    return this.sources.size > 0 && this.nextTime > this.context.currentTime;
  }

  enqueue(data, mimeType) {
    if (!/^audio\/pcm(?:;|$)/.test(mimeType)) {
      throw new Error("지원하지 않는 응답 오디오 형식입니다.");
    }
    const rate = Number(/rate=(\d+)/.exec(mimeType)?.[1] ?? 24000);
    if (rate < 8000 || rate > 96000) {
      throw new Error("응답 오디오 샘플레이트를 확인해 주세요.");
    }
    const raw = Uint8Array.from(atob(data), (character) => character.charCodeAt(0));
    if (!raw.length || raw.length % 2) {
      throw new Error("응답 오디오 데이터가 올바르지 않습니다.");
    }
    if (this.nextTime - this.context.currentTime > 20) {
      throw new Error("오디오 재생이 지연되었습니다. 다시 시작해 주세요.");
    }

    const view = new DataView(raw.buffer);
    const buffer = this.context.createBuffer(1, raw.length / 2, rate);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < channel.length; i++) {
      channel[i] = view.getInt16(i * 2, true) / 32768;
    }

    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    this.sources.add(source);
    source.onended = () => {
      source.disconnect();
      this.sources.delete(source);
      if (!this.sources.size) this.onSpeaking(false);
    };
    const start = Math.max(this.context.currentTime + 0.025, this.nextTime);
    source.start(start);
    this.nextTime = start + buffer.duration;
    this.onSpeaking(true);
  }

  clear() {
    for (const source of this.sources) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // The source may have ended between callbacks.
      }
      source.disconnect();
    }
    this.sources.clear();
    this.nextTime = 0;
    this.onSpeaking(false);
  }
}

