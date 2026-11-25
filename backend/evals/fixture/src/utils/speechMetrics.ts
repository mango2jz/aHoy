// Caps for FluencySpectrum dual-axis chart.
// Over-cap values clip at cap in v1; upgrade to visible overage indicator if telemetry shows frequent overcap.
export const MAX_WPM = 200;

// Filler-word rate expressed as 0..1 (i.e. 0.20 == 20%).
export const MAX_FILLER_RATE = 0.2;

const inRange = (v: unknown, min: number, max = Infinity, int = false): v is number =>
    typeof v === 'number' && Number.isFinite(v) && v >= min && v <= max && (!int || Number.isInteger(v));

/**
 * Validation for pacing arriving from the browser or read back from the DB.
 * Hand-written (the original used zod) so the fixture runs without npm install.
 * Core fields must be valid; optional pause fields fail soft and are dropped.
 */
export const SpeechMetricsSchema = {
    safeParse(raw: unknown): {success: true; data: SpeechMetrics} | {success: false} {
        const r = (raw ?? {}) as Record<string, unknown>;
        if (!inRange(r.wpm, 0) || !inRange(r.fillerRate, 0, 1) || !inRange(r.duration, 0)) return {success: false};
        const data: SpeechMetrics = {wpm: r.wpm, fillerRate: r.fillerRate, duration: r.duration};
        if (inRange(r.speechRatio, 0, 1)) data.speechRatio = r.speechRatio;
        if (inRange(r.longPauses, 0, Infinity, true)) data.longPauses = r.longPauses;
        if (inRange(r.meanRunSec, 0)) data.meanRunSec = r.meanRunSec;
        if (inRange(r.pausesPerMin, 0)) data.pausesPerMin = r.pausesPerMin;
        if (inRange(r.silentPauses, 0, Infinity, true)) data.silentPauses = r.silentPauses;
        if (inRange(r.spanSec, 0)) data.spanSec = r.spanSec;
        return {success: true, data};
    },
};

export interface SpeechMetrics {
    /** Words per minute over the student's speaking time. */
    wpm: number;
    /** Filler tokens as a fraction of all tokens (0..1). */
    fillerRate: number;
    /** Speaking time in seconds. */
    duration: number;
    /** Share of the trimmed answer with speech (0..1). */
    speechRatio?: number;
    /** Interior silences of at least 1 s. */
    longPauses?: number;
    /** Mean length of an uninterrupted speech run, in seconds. */
    meanRunSec?: number;
    /** Silent pauses per minute of the trimmed answer. */
    pausesPerMin?: number;
    /** Interior silences of at least 0.5 s. */
    silentPauses?: number;
    /** Answer length without leading and trailing silence, in seconds. */
    spanSec?: number;
}

// Single-token fillers as they appear in Gemini input transcriptions.
const FILLER_WORDS = new Set(['um', 'uh', 'umm', 'uhh', 'er', 'erm', 'ah', 'hmm', 'mm', 'mhm']);

/** Lowercased word tokens with punctuation stripped (apostrophes kept). */
export function tokenize(text: string): string[] {
    return text
        .toLowerCase()
        .split(/\s+/)
        .map((t) => t.replace(/[^\p{L}\p{N}']/gu, ''))
        .filter((t) => t.length > 0);
}

/**
 * Deterministic pacing metrics for one spoken answer. Computed from the
 * transcript and measured speaking time, never asked of the model.
 */
export function computeSpeechMetrics(transcript: string, durationSeconds: number): SpeechMetrics {
    const tokens = tokenize(transcript);
    const fillers = tokens.filter((t) => FILLER_WORDS.has(t)).length;
    // A missing or non-finite measurement is 0, never NaN: these values are
    // persisted and validated downstream.
    const duration = Number.isFinite(durationSeconds) ? Math.max(0, Math.round(durationSeconds)) : 0;
    const wpm = duration > 0 ? Math.round((tokens.length / duration) * 60) : 0;
    const fillerRate = tokens.length > 0 ? Math.round((fillers / tokens.length) * 1000) / 1000 : 0;
    return {wpm, fillerRate, duration};
}
