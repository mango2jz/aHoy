/**
 * Objective conversational-flow signals for the doc-2 hybrid examiner flow.
 *
 * `deriveFlowState` is a PURE function: measurable facts (duration, word count,
 * hesitation phrases, follow-up detection) are computed in code and injected into
 * the Live model each turn, while phrasing/branch-narration stays the model's. The
 * hard end gate (`evaluateEndGate`) consumes `minDurationMet` + `followUpAnswered`.
 */
import {STANDARD_FOLLOW_UP} from '../libs/scenarioConstants.ts';
import {tokenize} from './speechMetrics.ts';

export type FlowPhase = 'greeting' | 'answering' | 'probing' | 'hints' | 'followup' | 'closing';

export interface FlowSignals {
    /** Cumulative seconds the student has spoken across the attempt. */
    studentSpeakingSecondsCumulative: number;
    /** `scenario.min_duration_seconds` (doc-2: 120). */
    minDurationSeconds: number;
    /** Verbatim transcript of the most recent student answer. */
    lastTurnTranscript: string;
    /** The examiner's most recent spoken reply (for follow-up detection). */
    lastExaminerReply: string;
    /** Number of completed student turns so far (0 = pre-first-answer / greeting). */
    turnIndex: number;
    /** Sticky: the follow-up was already asked in an earlier turn. */
    followUpAlreadyAsked: boolean;
    /** Sticky: the follow-up was already answered in an earlier turn. */
    followUpAnsweredAlready: boolean;
    /** This completed student turn answers the (already-asked) follow-up. */
    answersFollowUp: boolean;
}

export interface FlowState {
    studentSpeakingSecondsCumulative: number;
    minDurationMet: boolean;
    lastTurnWordCount: number;
    hesitationDetected: boolean;
    followUpAsked: boolean;
    followUpAnswered: boolean;
    currentPhase: FlowPhase;
}

/** Below this word count an answer is treated as too short / a Case-3 stuck signal. */
export const HESITATION_WORD_THRESHOLD = 8;

/** Fallback minimum speaking time when a scenario has no `min_duration_seconds`. */
export const DEFAULT_MIN_DURATION_SECONDS = 120;

/**
 * Speech-time credit per transcribed word: an answer counts for at most
 * `words / WORDS_PER_SECOND` seconds, never more than the mic was open.
 * B1 learners speak ~100-130 wpm, so 2.0 is a calibration knob.
 */
// ponytail: transcript-derived speech time; upgrade to RMS frame gating in the PCM worklet if it proves too coarse.
export const WORDS_PER_SECOND = 2.0;

/** Explicit "I'm stuck" phrases (doc-2 Case 3). */
export const HESITATION_REGEX =
    /\b(i\s*don'?t\s*know|i'?m\s*not\s*sure|not\s*sure|it'?s\s*(too\s*)?hard|too\s*hard|hard\s*to\s*say|i\s*can'?t|no\s*idea|don'?t\s*know\s*what\s*to\s*say|sorry)\b/i;

/** Cumulative student words after the follow-up is asked that count as answering it. */
export const FOLLOW_UP_MIN_WORDS = 15;

/** Function words ignored when matching the examiner's reply against the follow-up. */
export const STOP_WORDS = new Set(
    'the a an and or so to of in on at for is are do you think that what which have any could work'.split(' '),
);

const FOLLOW_UP_MATCH_RATIO = 0.6;

const ALTERNATIVE_MARKER = /\b(any|another|else)\b/i;

function contentWords(text: string): Set<string> {
    // ponytail: naive 4-char stem (idea/ideas, better); swap for a real stemmer if paraphrases still miss
    return new Set(tokenize(text).filter((t) => !STOP_WORDS.has(t)).map((t) => t.slice(0, 4)));
}

/**
 * Live-gate follow-up detection: the reply is a question (contains `?`) and
 * carries at least 60 % of the follow-up's content words (each counted once).
 * Rejects generic probes ("tell me more about that idea?") and statements that
 * merely echo the words.
 */
export function matchesFollowUp(reply: string, followUp: string = STANDARD_FOLLOW_UP): boolean {
    if (!reply.includes('?')) return false;
    // A follow-up asking for something new ("any other idea") must keep that marker,
    // or comparison probes ("better than the other ideas?") would count as asked.
    if (ALTERNATIVE_MARKER.test(followUp) && !ALTERNATIVE_MARKER.test(reply)) return false;
    const wanted = contentWords(followUp);
    if (wanted.size === 0) return false;
    const got = contentWords(reply);
    let hits = 0;
    for (const word of wanted) if (got.has(word)) hits += 1;
    return hits / wanted.size >= FOLLOW_UP_MATCH_RATIO;
}

/**
 * Server-recovery follow-up rule (lenient); the live gate uses `matchesFollowUp`.
 * The examiner asking for OTHER approaches/solutions (the scenario follow-up).
 * Two parts: an "alternative" cue + a "proposal/approach" noun-or-verb. Kept
 * broad because the model rephrases freely and the input is ASR of its speech.
 */
export const FOLLOWUP_REGEX =
    /\b(other|another|more|else|further|alternative|alternatives|different|differently|additional)\b[\s\S]{0,40}\b(approach|approaches|solution|solutions|idea|ideas|option|options|way|ways|strateg|consider|consideration|recommend|suggest|propos)\b/i;

/**
 * Interrogative openers — used to relax the literal "?" requirement, because
 * speech-to-text frequently drops the question mark.
 */
export const INTERROGATIVE_REGEX =
    /\b(what|how|which|why|could|would|should|do you|can you|any|are there|is there|tell me|let'?s)\b/i;

function countWords(text: string): number {
    const trimmed = text.trim();
    if (trimmed.length === 0) return 0;
    return trimmed.split(/\s+/).length;
}

/**
 * True when the examiner's reply reads as the scenario follow-up question:
 * the alternative/approach lexicon PLUS a question mark OR an interrogative
 * opener (the latter survives ASR dropping the "?").
 */
export function detectFollowUp(examinerReply: string): boolean {
    if (!FOLLOWUP_REGEX.test(examinerReply)) return false;
    return examinerReply.includes('?') || INTERROGATIVE_REGEX.test(examinerReply);
}

export function deriveFlowState(signals: FlowSignals): FlowState {
    const lastTurnWordCount = countWords(signals.lastTurnTranscript);
    const minDurationMet =
        signals.studentSpeakingSecondsCumulative >= signals.minDurationSeconds;
    const hesitationDetected =
        signals.turnIndex > 0 &&
        (lastTurnWordCount < HESITATION_WORD_THRESHOLD ||
            HESITATION_REGEX.test(signals.lastTurnTranscript));
    const followUpAsked =
        signals.followUpAlreadyAsked || matchesFollowUp(signals.lastExaminerReply);
    const followUpAnswered = signals.followUpAnsweredAlready || signals.answersFollowUp;

    let currentPhase: FlowPhase;
    if (signals.turnIndex <= 0) {
        currentPhase = 'greeting';
    } else if (hesitationDetected) {
        currentPhase = 'hints';
    } else if (followUpAnswered && minDurationMet) {
        currentPhase = 'closing';
    } else if (followUpAsked && !followUpAnswered) {
        currentPhase = 'followup';
    } else if (minDurationMet) {
        currentPhase = 'probing';
    } else {
        currentPhase = 'answering';
    }

    return {
        studentSpeakingSecondsCumulative: signals.studentSpeakingSecondsCumulative,
        minDurationMet,
        lastTurnWordCount,
        hesitationDetected,
        followUpAsked,
        followUpAnswered,
        currentPhase,
    };
}
