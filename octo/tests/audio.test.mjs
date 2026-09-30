import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { test } from 'node:test';
const source = await readFile(new URL('../master/audio.js', import.meta.url), 'utf8');
const { VisitorAudio, PCMEncoder } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));

test('visitor audio drops silence, bounds twenty seconds, and consumes a request once', () => {
  const capture = new VisitorAudio();
  const pcm = new ArrayBuffer(1024);
  capture.push(pcm, 0);
  assert.equal(capture.take(), null);
  for (let i = 0; i < 800; i++) capture.push(pcm, .1);
  assert.ok(capture.bytes <= 640000);
  const audio = capture.take();
  assert.equal(audio.mimeType, 'audio/pcm;rate=16000');
  assert.ok(audio.chunks.length > 0);
  assert.equal(capture.take(), null);
  capture.push(pcm, .1);
  for (let i = 0; i < 100; i++) capture.push(pcm, 0);
  capture.push(pcm, .2);
  assert.equal(capture.chunks.length, 1, 'new visitor speech replaces an old utterance');
});

test('PCM stays continuous across 48 kHz worklet blocks', () => {
  const packets = [];
  const encoder = new PCMEncoder(48000, (p) => packets.push(p), 160);
  for (let i = 0; i < 750; i++) encoder.push(new Float32Array(128).fill(.5));
  assert.equal(packets.length, 200);
  assert.equal(new DataView(packets[0]).getInt16(0, true), 16384);
});
