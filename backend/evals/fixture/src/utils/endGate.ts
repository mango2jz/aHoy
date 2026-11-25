/**
 * The objective, model-independent end gate for a live practice attempt: a test
 * must not end before the student has spoken for the minimum duration and has
 * answered the scenario follow-up. This pure function is the single source of
 * truth for that decision; session end is client-owned.
 */
import {WORDS_PER_SECOND} from './flowState.ts';
import {SpeechMetricsSchema} from './speechMetrics.ts';

export interface EndGateInput {
    /** Cumulative student speaking time has reached `scenario.min_duration_seconds`. */
    minDurationMet: boolean;
    /** The learner has answered the scenario follow-up question. */
    followUpAnswered: boolean;
}

export interface EndGateResult {
    result: 'approved' | 'continue';
    /** Present only when `result === 'continue'` — fed back to the model to steer it. */
    reason?: string;
}

/** Approve the end only when BOTH objective conditions hold; otherwise continue. */
export function evaluateEndGate({minDurationMet, followUpAnswered}: EndGateInput): EndGateResult {
    if (minDurationMet && followUpAnswered) return {result: 'approved'};

    const missing: string[] = [];
    if (!minDurationMet) missing.push('the minimum speaking duration is not met');
    if (!followUpAnswered) missing.push('the learner has not answered the follow-up question yet');

    return {
        result: 'continue',
        reason: `Do not end yet — ${missing.join(' and ')}.`,
    };
}

/** Slack under the scenario floor that absorbs speaking-time rounding. */
export const END_GATE_TOLERANCE_SECONDS = 10;

/** One persisted, non-partial `conversation_memory` row, as far as the gate cares. */
export interface PersistedGateTurn {
    role: string | null;
    speech_metrics: unknown;
}

export interface PersistedEndGate {
    answers: number;
    /** Null when no turn carries pacing (wall clock decided instead). */
    spokenSeconds: number | null;
    elapsedSeconds: number;
    eligible: boolean;
}

/**
 * The end gate re-derived on the server from what persisted, for when the live
 * in-memory gate is gone (recovery) or cannot be trusted (a client claiming
 * `completed`). Basics only: at least two answers and enough speaking time, with
 * END_GATE_TOLERANCE_SECONDS of slack. The follow-up rule stays with the client
 * flow gate. Speaking time mirrors the live gate: words spoken / WORDS_PER_SECOND,
 * capped at mic-open time. Turns written before `speech_metrics` existed have
 * none, so wall clock (a lower bound) stands in.
 */
export function derivePersistedEndGate(
    rows: PersistedGateTurn[],
    {startedAt, endedAt, floorSeconds}: {startedAt: string | null; endedAt: string | null; floorSeconds: number},
): PersistedEndGate {
    const answers = rows.filter((row) => row.role === 'user').length;
    const elapsedSeconds =
        startedAt && endedAt ? Math.max(0, Math.round((Date.parse(endedAt) - Date.parse(startedAt)) / 1000)) : 0;
    let measured = false;
    const spoken = rows.reduce((total, row) => {
        const parsed = SpeechMetricsSchema.safeParse(row.speech_metrics);
        if (!parsed.success) return total;
        measured = true;
        const {wpm, duration} = parsed.data;
        return total + Math.min(duration, (wpm * duration) / 60 / WORDS_PER_SECOND);
    }, 0);
    const required = floorSeconds - END_GATE_TOLERANCE_SECONDS;
    const longEnough = measured ? spoken >= required : elapsedSeconds >= required;
    return {
        answers,
        spokenSeconds: measured ? spoken : null,
        elapsedSeconds,
        eligible: answers >= 2 && longEnough,
    };
}
