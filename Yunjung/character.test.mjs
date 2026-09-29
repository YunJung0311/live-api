import test from 'node:test';
import assert from 'node:assert/strict';
import { CharacterActivity, animateCharacter } from './character.js';

test('confirmed speech transitions to waiting after silence, brief noise does not', () => {
  const activity = new CharacterActivity(() => {});
  activity.input(.1, 0);
  activity.tick(700);
  assert.equal(activity.state, 'listening');
  activity.input(.1, 1000);
  activity.input(.1, 1150);
  activity.tick(1700);
  assert.equal(activity.state, 'listening');
  activity.tick(1800);
  assert.equal(activity.state, 'processing');
  activity.input(.1, 1900);
  activity.input(.1, 2050);
  assert.equal(activity.state, 'listening');
});

test('text submission, playback and completion keep speaking until the audio drains', () => {
  const states = [];
  const activity = new CharacterActivity(s => states.push(s));
  activity.submitted(0);
  assert.equal(activity.state, 'processing');
  activity.playback(true);
  activity.complete();
  assert.equal(activity.state, 'speaking');
  activity.playback(false);
  assert.equal(activity.state, 'listening');
  assert.deepEqual(states, ['processing', 'speaking', 'listening']);
});

test('interruption immediately clears speaking and any estimated waiting', () => {
  const activity = new CharacterActivity(() => {});
  activity.submitted(0);
  activity.playback(true);
  activity.interrupted();
  activity.tick(5000);
  assert.equal(activity.state, 'listening');
  activity.submitted(6000);
  activity.interrupted();
  assert.equal(activity.state, 'listening');
});

test('waiting times out without getting stuck and silence does not invent turns', () => {
  const activity = new CharacterActivity(() => {});
  activity.input(.002, 0);
  activity.tick(1000);
  assert.equal(activity.state, 'listening');
  activity.submitted(1000);
  activity.tick(15999);
  assert.equal(activity.state, 'processing');
  activity.tick(16000);
  assert.equal(activity.state, 'listening');
});

test('mic activity during playback does not override speaking or create a later false wait', () => {
  const activity = new CharacterActivity(() => {});
  activity.playback(true);
  activity.input(.2, 0);
  activity.input(.2, 200);
  activity.tick(900);
  assert.equal(activity.state, 'speaking');
  activity.playback(false);
  assert.equal(activity.state, 'listening');
});

test('audio-driven motion respects reduce/off and cleanup cancels the loop', () => {
  const original = { window: globalThis.window, requestAnimationFrame: globalThis.requestAnimationFrame, cancelAnimationFrame: globalThis.cancelAnimationFrame };
  let next, cancelled;
  const preference = { matches: false };
  globalThis.window = { matchMedia: () => preference };
  globalThis.requestAnimationFrame = fn => { next = fn; return 42; };
  globalThis.cancelAnimationFrame = id => { cancelled = id; };
  try {
    const values = new Map();
    const element = { dataset: { motion: 'on' }, style: { setProperty: (k,v) => values.set(k,v), removeProperty: k => values.delete(k) } };
    const session = { done: false, ready: true, activity: new CharacterActivity(() => {}), analyser: { fftSize: 16, getFloatTimeDomainData: a => a.fill(.1) } };
    session.activity.playback(true);
    const stop = animateCharacter(element, session);
    next(0);
    assert.ok(parseFloat(values.get('--voice-lift')) < 0);
    preference.matches = true;
    next(40);
    assert.equal(values.get('--voice-scale'), '1.0000');
    preference.matches = false;
    element.dataset.motion = 'off';
    next(80);
    assert.equal(values.get('--voice-scale'), '1.0000');
    stop();
    assert.equal(cancelled, 42);
    assert.equal(values.size, 0);
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});
