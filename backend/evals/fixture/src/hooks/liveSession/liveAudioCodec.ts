// liveAudioCodec — Client-side audio codec for the Gemini Live WebSocket path.
//
// Two conversion domains:
//   INPUT (MediaRecorder → Gemini Live API):
//     WebM/Opus chunks → AudioContext.decodeAudioData → downsample to 16 kHz
//     → float-to-16-bit-PCM → Uint8Array ready for the WS "audio" event.
//
//   OUTPUT (Gemini Live API → Speaker):
//     PCM16 24 kHz mono chunks → AudioBuffer → AudioBufferSourceNode queue
//     → gap-free playback through AudioContext.
//
// Barge-in: stopOutput() stops all scheduled nodes and clears the queue in <50 ms.

import { downsampleBuffer, floatTo16BitPCM } from "../../utils/audio/pcm.ts";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Target sample rate for Gemini Live API input (16 kHz mono PCM16). */
export const INPUT_RATE = 16_000;

/** Sample rate received from Gemini Live API output (24 kHz PCM16 mono). */
export const OUTPUT_RATE = 24_000;

/** How long a resume() may fail to take hold before the context counts as stuck. */
const RESUME_STUCK_MS = 1_000;

// ---------------------------------------------------------------------------
// Input path: WebM/Opus → PCM16 (for sending to Gemini Live API)
// ---------------------------------------------------------------------------

/**
 * Decode a WebM/Opus chunk from MediaRecorder into PCM16 16 kHz mono bytes.
 *
 * The browser's AudioContext.decodeAudioData() handles the WebM container and
 * Opus codec natively on all modern browsers. The result is downsampled from
 * the native recording rate to 16 kHz, then packed as 16-bit little-endian PCM.
 *
 * @param webmChunk — Raw bytes from a MediaRecorder `dataavailable` event.
 * @param ctx       — A (lazy) AudioContext instance (shared across calls).
 * @returns A Uint8Array of PCM16 16 kHz mono bytes, or null on decode failure.
 */
export async function decodeInputToPcm16(
  webmChunk: ArrayBuffer,
  ctx: AudioContext,
): Promise<Uint8Array | null> {
  try {
    // 1. Decode WebM/Opus → AudioBuffer (Float32 at native recording rate).
    const audioBuffer = await ctx.decodeAudioData(webmChunk.slice(0));

    // 2. Extract the first channel (mono mix if multi-channel).
    const channelData = audioBuffer.getChannelData(0);
    const nativeRate = audioBuffer.sampleRate;

    // 3. Downsample to 16 kHz.
    const downsampled =
      nativeRate !== INPUT_RATE
        ? downsampleBuffer(channelData, nativeRate, INPUT_RATE)
        : channelData;

    // 4. Float32 → PCM16 little-endian.
    const pcm16Buf = floatTo16BitPCM(downsampled);

    return new Uint8Array(pcm16Buf);
  } catch (err) {
    console.error("live.audio.decode_input_failed", {
      byteLength: webmChunk.byteLength,
      reason: err instanceof Error ? err.message : String(err),
    });
    return null;
  }
}

// ---------------------------------------------------------------------------
// Output path: PCM16 → gap-free AudioContext playback
// ---------------------------------------------------------------------------

/** Internal schedule slot for output playback. */
interface OutputSlot {
  /** The decoded AudioBuffer ready for scheduling. */
  buffer: AudioBuffer;
  /** The source node once scheduled (null before play / after stop). */
  source: AudioBufferSourceNode | null;
  /** True when this slot's audio has fully played out. */
  played: boolean;
}

/**
 * Wrap raw PCM16 little-endian mono bytes in a minimal WAV header so that
 * AudioContext.decodeAudioData can parse them.
 *
 * Exported for unit testing.
 */
export function pcm16ToWav(data: ArrayBuffer, sampleRate: number): ArrayBuffer {
  const len = data.byteLength;
  const header = new ArrayBuffer(44);
  const h = new DataView(header);
  const w = (pos: number, s: string) => {
    for (let i = 0; i < s.length; i++) h.setUint8(pos + i, s.charCodeAt(i));
  };
  w(0, "RIFF");
  h.setUint32(4, 36 + len, true);
  w(8, "WAVE");
  w(12, "fmt ");
  h.setUint32(16, 16, true);
  h.setUint16(20, 1, true); // PCM
  h.setUint16(22, 1, true); // mono
  h.setUint32(24, sampleRate, true);
  h.setUint32(28, sampleRate * 2, true);
  h.setUint16(32, 2, true); // block align
  h.setUint16(34, 16, true); // bits per sample
  w(36, "data");
  h.setUint32(40, len, true);
  // Concatenate header + raw PCM16 into one ArrayBuffer.
  const result = new Uint8Array(44 + len);
  result.set(new Uint8Array(header), 0);
  result.set(new Uint8Array(data), 44);
  return result.buffer;
}

/**
 * Create a managed audio output queue for playing Gemini Live API output.
 *
 * Audio chunks (PCM16 24 kHz mono) are decoded and queued for gap-free playback.
 * The caller calls `enqueueOutput()` as chunks arrive and `playOutput()` to
 * start/resume. `stopOutput()` halts everything for barge-in.
 */
export function createOutputPlayer() {
  let ctx: AudioContext | null = null;
  let slots: OutputSlot[] = [];
  let nextStartTime = 0;
  let playing = false;
  let endFired = false;
  let onEndCb: (() => void) | null = null;
  let onStuckCb: (() => void) | null = null;
  let onRecoveredCb: (() => void) | null = null;
  // Chunks currently inside `decodeAudioData`. The mic must stay locked until
  // these land, or an in-flight decode would start playing over the student.
  let pendingDecodes = 0;
  // `decodeAudioData` is asynchronous. A stop can happen while a chunk is
  // decoding; this token prevents that stale decode from reviving playback.
  let generation = 0;

  // Safari creates the context suspended outside a user gesture, and iOS flips a
  // running one to the non-standard "interrupted" once the mic opens. Chrome
  // also passes briefly through "suspended" on creation, but its resume() lands
  // within milliseconds — so a non-running context only counts as *stuck* once a
  // resume attempt has failed to take hold within RESUME_STUCK_MS.
  let stuck = false;
  let resumeTimer: ReturnType<typeof setTimeout> | null = null;

  function clearResumeTimer(): void {
    if (resumeTimer === null) return;
    clearTimeout(resumeTimer);
    resumeTimer = null;
  }

  /**
   * Nothing will ever play through this context. Drop the queue (stale audio
   * must not fire over the student later) and re-fire onEnd so the strategy
   * re-evaluates the mic unlock instead of stranding it.
   */
  function markStuck(state: string): void {
    clearResumeTimer();
    const wasStuck = stuck;
    stuck = true;
    if (playing || pendingDecodes > 0 || slots.some((s) => !s.played)) {
      stopOutput();
      endFired = false;
      fireEnd();
    }
    console.warn("live.audio.context_stuck", {state});
    if (!wasStuck) onStuckCb?.();
  }

  function attemptResume(target: AudioContext): void {
    let pending: Promise<void> | undefined;
    try {
      // Always a fresh resume: a tap inside the stuck window must spend its
      // gesture on one, not be swallowed by the pending timer.
      pending = target.resume();
    } catch {
      markStuck(target.state as string);
      return;
    }
    if (resumeTimer === null) {
      resumeTimer = setTimeout(() => {
        resumeTimer = null;
        if ((target.state as string) !== "running")
          markStuck(target.state as string);
      }, RESUME_STUCK_MS);
    }
    // A late rejection from a superseded attempt must not stop playback that a
    // later one already recovered.
    void pending?.catch?.(() => {
      if ((target.state as string) !== "running")
        markStuck(target.state as string);
    });
  }

  function ensureCtx(): AudioContext {
    if (!ctx) {
      const created = new AudioContext();
      ctx = created;
      created.onstatechange = () => {
        if ((created.state as string) !== "running") {
          // Losing a running context mid-playback is never the benign
          // creation-time window: the queue is already unplayable.
          if (playing || slots.some((s) => !s.played))
            markStuck(created.state as string);
          attemptResume(created);
        } else {
          const wasStuck = stuck;
          stuck = false;
          clearResumeTimer();
          if (wasStuck) onRecoveredCb?.();
          if (!playing && slots.some((s) => !s.played)) playOutput();
        }
      };
    }
    if ((ctx.state as string) !== "running") attemptResume(ctx);
    return ctx;
  }

  function fireEnd() {
    if (endFired) return;
    endFired = true;
    onEndCb?.();
  }

  /** Schedule a single decoded buffer for gap-free playback. */
  function scheduleBuffer(buffer: AudioBuffer, index: number) {
    if (!ctx) return;
    const slot = slots[index];
    if (!slot) return;

    try {
      slot.source?.stop();
    } catch {
      /* ok */
    }

    const source = ctx.createBufferSource();
    source.buffer = buffer;
    source.connect(ctx.destination);

    source.start(nextStartTime);
    nextStartTime += buffer.duration;

    source.onended = () => {
      slot.played = true;
      if (index === slots.length - 1 && slots.every((s) => s.played)) {
        playing = false;
        fireEnd();
      }
    };

    slot.source = source;
  }

  /** Start/resume playback from the queue. */
  function playOutput(): void {
    if (playing) return;
    if (slots.length === 0) return;

    playing = true;
    endFired = false;
    const ac = ensureCtx();
    nextStartTime = ac.currentTime + 0.01; // small guard interval

    for (let i = 0; i < slots.length; i++) {
      if (!slots[i]!.played) {
        scheduleBuffer(slots[i]!.buffer, i);
      }
    }
  }

  /** Stop all output immediately (barge-in). Clears queue. */
  function stopOutput(): void {
    generation += 1;
    for (const slot of slots) {
      try {
        slot.source?.stop();
      } catch {
        /* already stopped */
      }
      slot.source = null;
      slot.played = true;
    }
    slots = [];
    nextStartTime = 0;
    playing = false;
  }

  return {
    /** True when audio is currently playing (not stopped / queue empty). */
    get isPlaying(): boolean {
      return playing;
    },

    /** Chunks currently being decoded (not yet queued). */
    get pendingDecodes(): number {
      return pendingDecodes;
    },

    /**
     * True when nothing is playing and nothing is still on its way to the
     * speaker: no decode in flight, and no decoded-but-unscheduled slot. A slot
     * pushed while stopped stays unscheduled (`source === null && !played`)
     * until `playOutput`, which the auto-play in `livePlayer.enqueueAudio` only
     * reaches in a later promise continuation — so it must count as NOT drained.
     */
    get drained(): boolean {
      // A stuck context (Safari suspended, iOS "interrupted", closed) can never
      // play what is queued — holding the mic on it strands the student.
      if (stuck) return true;
      return (
        pendingDecodes === 0 &&
        !playing &&
        !slots.some((s) => s.source === null && !s.played)
      );
    },

    /** Estimated seconds of audio remaining in the queue. */
    get bufferHealth(): number {
      return slots.reduce(
        (sum, s) => (s.played ? sum : sum + s.buffer.duration),
        0,
      );
    },

    /** Fired when the queue drains naturally (NOT on stopOutput). */
    get onEnd(): (() => void) | null {
      return onEndCb;
    },
    set onEnd(cb: (() => void) | null) {
      onEndCb = cb;
    },

    /** Fired once per stuck episode (context wedged — nothing will play). */
    get onStuck(): (() => void) | null {
      return onStuckCb;
    },
    set onStuck(cb: (() => void) | null) {
      onStuckCb = cb;
    },

    /** Fired when a stuck context comes back to `running`. */
    get onRecovered(): (() => void) | null {
      return onRecoveredCb;
    },
    set onRecovered(cb: (() => void) | null) {
      onRecoveredCb = cb;
    },

    /**
     * Enqueue a PCM16 chunk for playback.
     *
     * @param data — Raw PCM16 little-endian mono bytes (typically 24 kHz).
     * @param sampleRate — The sample rate of the PCM data (default 24_000).
     */
    async enqueueOutput(
      data: ArrayBuffer,
      sampleRate: number = OUTPUT_RATE,
    ): Promise<void> {
      const ac = ensureCtx();
      const decodeGeneration = generation;
      pendingDecodes += 1;
      try {
        // Wrap raw PCM16 in a WAV header so decodeAudioData can parse it.
        const wav = pcm16ToWav(data, sampleRate);
        const buffer = await ac.decodeAudioData(wav.slice(0));
        // Audio that landed during an interruption must never replay over the
        // student's live answer once the context comes back.
        if (decodeGeneration !== generation || stuck) return;
        const index = slots.length;
        slots.push({ buffer, source: null, played: false });

        // If already playing, schedule this new chunk immediately.
        if (playing) {
          scheduleBuffer(buffer, index);
        }
      } catch (err) {
        console.error("live.audio.decode_output_failed", {
          byteLength: data.byteLength,
          reason: err instanceof Error ? err.message : String(err),
        });
      } finally {
        // A failed decode counts as drained: it will never produce audio, so
        // holding the mic locked on it would strand the student.
        pendingDecodes -= 1;
      }
    },

    playOutput,

    stopOutput,

    /**
     * Create and resume the AudioContext. Must be called inside a user gesture
     * on Safari/iOS, which otherwise leaves a lazily-created context suspended.
     * Idempotent.
     */
    unlock(): void {
      ensureCtx();
    },

    /** Tear the context down for good (session end / reconnect). */
    close(): void {
      stopOutput();
      clearResumeTimer();
      void ctx?.close().catch(() => undefined);
      ctx = null;
      stuck = false;
    },
  };
}

export type OutputPlayer = ReturnType<typeof createOutputPlayer>;
