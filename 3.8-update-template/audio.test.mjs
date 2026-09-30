import test from "node:test";
import assert from "node:assert/strict";
import { AudioPlayer, PCMEncoder } from "./audio.js";

for (const inputRate of [16000, 44100, 48000]) {
  test(`${inputRate} Hz input produces one second of continuous 16 kHz PCM`, () => {
    const packets = [];
    const encoder = new PCMEncoder(inputRate, (packet) => packets.push(packet), 160);
    const samples = new Float32Array(inputRate).fill(0.5);
    for (let i = 0; i < samples.length; i += 128) encoder.push(samples.subarray(i, i + 128));
    assert.equal(packets.length, 100);
    for (const packet of packets) {
      const view = new DataView(packet);
      for (let i = 0; i < 160; i++) assert.equal(view.getInt16(i * 2, true), 16384);
    }
  });
}

test("PCM clips to signed 16-bit little-endian limits", () => {
  let result;
  const encoder = new PCMEncoder(16000, (packet) => { result = new Uint8Array(packet); }, 3);
  encoder.push(new Float32Array([-2, 0, 2]));
  assert.deepEqual([...result], [0, 128, 0, 0, 255, 127]);
});

function fakeContext() {
  const sources = [];
  return {
    currentTime: 2,
    destination: {},
    sources,
    createBuffer(_channels, length, rate) {
      const data = new Float32Array(length);
      return { duration: length / rate, getChannelData: () => data };
    },
    createBufferSource() {
      const source = {
        connect() {},
        disconnect() { this.disconnected = true; },
        start(time) { this.startTime = time; },
        stop() { this.stopped = true; },
      };
      sources.push(source);
      return source;
    },
  };
}

test("interruption stops scheduled audio and resets the playback clock", () => {
  const context = fakeContext();
  const states = [];
  const player = new AudioPlayer(context, (value) => states.push(value));
  const audio = Buffer.alloc(4800).toString("base64");
  player.enqueue(audio, "audio/pcm;rate=24000");
  player.enqueue(audio, "audio/pcm;rate=24000");
  assert.equal(context.sources[1].startTime, context.sources[0].startTime + 0.1);
  assert.equal(player.speaking, true);
  player.clear();
  assert.ok(context.sources.every((source) => source.stopped && source.disconnected && source.onended === null));
  assert.equal(player.speaking, false);
  player.enqueue(audio, "audio/pcm;rate=24000");
  assert.equal(context.sources[2].startTime, 2.025);
  assert.deepEqual(states, [true, true, false, true]);
});

test("invalid audio is rejected before playback", () => {
  const context = fakeContext();
  const player = new AudioPlayer(context, () => {});
  assert.throws(() => player.enqueue("AA==", "audio/pcm;rate=24000"));
  assert.throws(() => player.enqueue("AAA=", "audio/mp3"));
  assert.throws(() => player.enqueue("AAA=", "audio/pcm;rate=1"));
  assert.equal(context.sources.length, 0);
});

