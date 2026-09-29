// Presentation-only activity tracking. It never controls Gemini's VAD or audio.
export class CharacterActivity {
  constructor(onState) {
    this.onState = onState;
    this.state = 'listening';
    this.playing = false;
    this.awaitingSince = null;
    this.voiceSince = null;
    this.lastVoice = null;
  }

  publish() {
    const next = this.playing ? 'speaking' : this.awaitingSince !== null ? 'processing' : 'listening';
    if (next !== this.state) {
      this.state = next;
      this.onState(next);
    }
  }

  input(level, now) {
    if (level >= 0.025) {
      this.voiceSince ??= now;
      this.lastVoice = now;
      // Brief bumps should not count as a user turn.
      if (now - this.voiceSince >= 100 && !this.playing) this.awaitingSince = null;
      this.publish();
    }
  }

  tick(now) {
    if (this.lastVoice !== null && now - this.lastVoice >= 650) {
      if (this.lastVoice - this.voiceSince >= 100 && !this.playing) this.awaitingSince = now;
      this.voiceSince = this.lastVoice = null;
    }
    // This is an estimate of response waiting, not a claim about model reasoning.
    if (this.awaitingSince !== null && now - this.awaitingSince >= 15000) this.awaitingSince = null;
    this.publish();
  }

  submitted(now) {
    this.voiceSince = this.lastVoice = null;
    this.awaitingSince = now;
    this.publish();
  }

  playback(active) {
    this.playing = active;
    if (active) {
      this.awaitingSince = null;
      this.voiceSince = this.lastVoice = null;
    }
    this.publish();
  }

  complete() {
    this.awaitingSince = null;
    this.voiceSince = this.lastVoice = null;
    this.publish(); // Scheduled audio may still be playing after turnComplete.
  }

  interrupted() {
    this.playing = false;
    this.complete();
  }
}

// Observe the existing audio stream; animate at most 30 times per second.
export function animateCharacter(element, session) {
  const samples = new Float32Array(session.analyser.fftSize);
  let frame = 0;
  let previous = -Infinity;
  let smoothLevel = 0;
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  function update(now) {
    if (session.done) return;
    if (now - previous >= 32) {
      previous = now;
      if (session.ready) session.activity.tick(now);
      let level = 0;
      if (session.activity.playing) {
        session.analyser.getFloatTimeDomainData(samples);
        level = Math.sqrt(samples.reduce((sum, value) => sum + value * value, 0) / samples.length) * 5;
      } else if (now - (session.lastMicAt ?? -Infinity) < 150) {
        level = session.micLevel ?? 0;
      }
      smoothLevel += (Math.min(1, level) - smoothLevel) * 0.35;
      const amount = reduced.matches || element.dataset.motion === 'off' ? 0 : smoothLevel;
      element.style.setProperty('--voice-lift', `${(-amount * 9).toFixed(2)}px`);
      element.style.setProperty('--voice-scale', (1 + amount * 0.025).toFixed(4));
      element.style.setProperty('--signal-opacity', (0.2 + amount * 0.65).toFixed(3));
    }
    frame = requestAnimationFrame(update);
  }
  frame = requestAnimationFrame(update);
  return () => {
    cancelAnimationFrame(frame);
    for (const name of ['--voice-lift', '--voice-scale', '--signal-opacity']) element.style.removeProperty(name);
  };
}
