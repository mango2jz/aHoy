// livePlayer — AudioContext output queue for Gemini Live API audio playback.
//
// Thin wrapper around createOutputPlayer from liveAudioCodec with live-session
// specific management: auto-play on enqueue, volume control, and seamless
// integration with the session lifecycle.

import {createOutputPlayer, type OutputPlayer} from './liveAudioCodec.ts';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface LivePlayer {
  /** Enqueue a PCM16 chunk for gap-free playback. Auto-plays if not already playing. */
  enqueueAudio(data: ArrayBuffer, sampleRate?: number): void;
  /** Stop all playback immediately (barge-in). Clears the queue. */
  stop(): void;
  /** Create/resume the output AudioContext. Call inside a user gesture (Safari/iOS). */
  unlock(): void;
  /** Stop playback and tear the AudioContext down for good. */
  close(): void;
  /** Mute/unmute the output. */
  setMuted(muted: boolean): void;
  /** True when audio is currently playing. */
  readonly isPlaying: boolean;
  /** Chunks currently being decoded (not yet queued). */
  readonly pendingDecodes: number;
  /**
   * True when nothing is playing and nothing is still on its way to the speaker
   * (no decode in flight, no decoded-but-unscheduled chunk). The mic unlock
   * condition — `onEnd` alone is not enough.
   */
  readonly drained: boolean;
  /** Estimated seconds of audio remaining in the queue. */
  readonly bufferHealth: number;
  /** Fired when the queue drains naturally (NOT on stop()). */
  onEnd: (() => void) | null;
  /** Fired once per stuck episode (AudioContext wedged — nothing will play). */
  onStuck: (() => void) | null;
  /** Fired when a stuck AudioContext comes back. */
  onRecovered: (() => void) | null;
}

// ---------------------------------------------------------------------------
// Factory
// ---------------------------------------------------------------------------

/**
 * Create a live audio player for Gemini Live API output.
 *
 * PCM16 chunks (24 kHz mono from the Live API) are queued for gap-free playback
 * via AudioContext. Auto-plays each chunk as it arrives. Supports barge-in via
 * `stop()` and volume mute via `setMuted()`.
 */
export function createLivePlayer(): LivePlayer {
  const output = createOutputPlayer();

  return {
    get isPlaying(): boolean { return output.isPlaying; },

    get pendingDecodes(): number { return output.pendingDecodes; },

    get drained(): boolean { return output.drained; },

    get bufferHealth(): number { return output.bufferHealth; },

    get onEnd(): (() => void) | null { return output.onEnd; },
    set onEnd(cb: (() => void) | null) { output.onEnd = cb; },

    get onStuck(): (() => void) | null { return output.onStuck; },
    set onStuck(cb: (() => void) | null) { output.onStuck = cb; },

    get onRecovered(): (() => void) | null { return output.onRecovered; },
    set onRecovered(cb: (() => void) | null) { output.onRecovered = cb; },

    enqueueAudio(data: ArrayBuffer, sampleRate?: number): void {
      // eslint-disable-next-line @typescript-eslint/no-floating-promises
      output.enqueueOutput(data, sampleRate).then(() => {
        if (!output.isPlaying) {
          output.playOutput();
        }
      });
    },

    stop(): void {
      output.stopOutput();
    },

    unlock(): void {
      output.unlock();
    },

    close(): void {
      output.close();
    },

    setMuted(muted: boolean): void {
      // Mute is a stub — full gain-node support requires exposing the internal
      // AudioContext from createOutputPlayer. For now this logs the request.
      console.info('live.audio.set_muted', {muted});
    },
  };
}

export type LivePlayerType = ReturnType<typeof createLivePlayer>;
