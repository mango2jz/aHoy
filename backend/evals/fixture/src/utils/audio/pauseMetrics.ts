/**
 * Pause metrics from raw PCM of one spoken answer.
 *
 * Pure (no Node or browser APIs) so it runs in the browser on recorded PCM and
 * in the grading benchmark on decoded WAV files.
 */

export interface PauseMetrics {
    /** Share of the trimmed span with speech (0..1). */
    speechRatio: number;
    /** Interior silences of at least 1 s. */
    longPauses: number;
    /** Mean length of an uninterrupted speech run, in seconds. */
    meanRunSec: number;
    /** `silentPauses` per minute of the trimmed span. */
    pausesPerMin: number;
    /** Interior silences of at least 0.5 s. */
    silentPauses: number;
    /** Length of the answer without leading and trailing silence, in seconds. */
    spanSec: number;
}

const FRAME_SEC = 0.02;
const NOISE_PERCENTILE = 0.1;
const SPEECH_MARGIN_DB = 12;
const MIN_THRESHOLD_DB = -50;
const BRIDGE_GAP_SEC = 0.2; // interior silences shorter than this count as speech
const MIN_SPEECH_SEC = 0.1; // speech runs shorter than this count as silence
const PAUSE_SEC = 0.5;
const LONG_PAUSE_SEC = 1.0;

const round = (n: number) => Math.round(n * 1000) / 1000;

function zeros(): PauseMetrics {
    return {speechRatio: 0, longPauses: 0, meanRunSec: 0, pausesPerMin: 0, silentPauses: 0, spanSec: 0};
}

/** Frame RMS in dBFS (full scale = 32768). Digital silence maps to -100. */
function frameDb(pcm: Int16Array, frameLen: number): Float64Array {
    const count = Math.floor(pcm.length / frameLen);
    const db = new Float64Array(count);
    for (let f = 0; f < count; f++) {
        let sum = 0;
        const start = f * frameLen;
        for (let i = start; i < start + frameLen; i++) sum += pcm[i] * pcm[i];
        const rms = Math.sqrt(sum / frameLen) / 32768;
        db[f] = rms > 1e-5 ? 20 * Math.log10(rms) : -100;
    }
    return db;
}

/** Maximal runs of equal value as [start, endExclusive, value]. */
function runsOf(flags: Uint8Array): Array<[number, number, number]> {
    const runs: Array<[number, number, number]> = [];
    let start = 0;
    for (let i = 1; i <= flags.length; i++) {
        if (i === flags.length || flags[i] !== flags[start]) {
            runs.push([start, i, flags[start]]);
            start = i;
        }
    }
    return runs;
}

export function computePauseMetrics(samples: Int16Array, sampleRate: number): PauseMetrics {
    const frameLen = Math.round(sampleRate * FRAME_SEC);
    if (frameLen < 1 || samples.length < frameLen) return zeros();

    const db = frameDb(samples, frameLen);
    const sorted = Float64Array.from(db).sort();
    const floor = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * NOISE_PERCENTILE))];
    const threshold = Math.max(floor + SPEECH_MARGIN_DB, MIN_THRESHOLD_DB);

    const speech = new Uint8Array(db.length);
    for (let f = 0; f < db.length; f++) speech[f] = db[f] > threshold ? 1 : 0;

    // Hangover: bridge short interior gaps, then drop short blips.
    const bridgeFrames = Math.round(BRIDGE_GAP_SEC / FRAME_SEC);
    const blipFrames = Math.round(MIN_SPEECH_SEC / FRAME_SEC);
    const runs = runsOf(speech);
    for (let r = 1; r < runs.length - 1; r++) {
        const [s, e, v] = runs[r];
        if (v === 0 && e - s < bridgeFrames) speech.fill(1, s, e);
    }
    for (const [s, e, v] of runsOf(speech)) {
        if (v === 1 && e - s < blipFrames) speech.fill(0, s, e);
    }

    // Trim leading and trailing silence.
    const first = speech.indexOf(1);
    if (first < 0) return zeros();
    const last = speech.lastIndexOf(1);
    const span = speech.subarray(first, last + 1);

    let speechFrames = 0;
    for (const v of span) speechFrames += v;

    const pauseFrames = Math.round(PAUSE_SEC / FRAME_SEC);
    const longFrames = Math.round(LONG_PAUSE_SEC / FRAME_SEC);
    let pauses = 0;
    let longPauses = 0;
    let runCount = 1;
    for (const [s, e, v] of runsOf(span)) {
        if (v !== 0) continue;
        const len = e - s;
        if (len >= pauseFrames) {
            pauses++;
            runCount++;
        }
        if (len >= longFrames) longPauses++;
    }

    // Uninterrupted runs are separated only by silences >= PAUSE_SEC, so short
    // gaps inside a run count towards its length.
    let pauseTotalFrames = 0;
    for (const [s, e, v] of runsOf(span)) {
        if (v === 0 && e - s >= pauseFrames) pauseTotalFrames += e - s;
    }
    const spanSec = span.length * FRAME_SEC;
    const meanRunSec = (spanSec - pauseTotalFrames * FRAME_SEC) / runCount;

    return {
        speechRatio: round(speechFrames / span.length),
        longPauses,
        meanRunSec: round(meanRunSec),
        pausesPerMin: round(pauses / (spanSec / 60)),
        silentPauses: pauses,
        spanSec: round(spanSec),
    };
}
