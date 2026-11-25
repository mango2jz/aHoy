import {
  deriveFlowState,
  FOLLOW_UP_MIN_WORDS,
  matchesFollowUp,
  WORDS_PER_SECOND,
  type FlowState,
} from "../../utils/flowState.ts";
import { evaluateEndGate, type EndGateResult } from "../../utils/endGate.ts";
import { tokenize } from "../../utils/speechMetrics.ts";

export interface FlowGateSnapshot {
  cumulativeStudentSeconds: number;
  turnIndex: number;
  minDurationSeconds: number;
  followUpCueCount: number;
  flowState: FlowState;
}

export interface FlowGateController {
  configure(input: { minDurationSeconds: number }): void;
  recordTurnStart(now?: number): void;
  /** Returns this turn's mic-open seconds; does not credit speech time. */
  accumulateSpeakingTime(now?: number): number;
  /** Credits transcript-derived speech time once per answer. */
  creditSpeech(wordCount: number | undefined, micOpenSeconds: number): void;
  /** The follow-up cue was sent; two cues force `followUpAsked` on the next update. */
  recordFollowUpCue(): void;
  updateFlowState(input: {
    lastTurnTranscript: string;
    lastExaminerReply: string;
  }): FlowState;
  evaluateEndGate(): EndGateResult;
  snapshot(): Readonly<FlowGateSnapshot>;
}

const initialFlowState: FlowState = {
  studentSpeakingSecondsCumulative: 0,
  minDurationMet: false,
  lastTurnWordCount: 0,
  hesitationDetected: false,
  followUpAsked: false,
  followUpAnswered: false,
  currentPhase: "greeting",
};

/** Pure-runtime flow policy; React owns only this controller's lifetime. */
export function createFlowGateController(): FlowGateController {
  let cumulativeStudentSeconds = 0;
  let turnIndex = 0;
  let minDurationSeconds = 120;
  let turnStartedAt: number | null = null;
  let followUpCueCount = 0;
  let wordsSinceFollowUp = 0;
  let flowState = initialFlowState;

  return {
    configure({ minDurationSeconds: next }): void {
      minDurationSeconds = next;
      flowState = deriveFlowState({
        studentSpeakingSecondsCumulative: cumulativeStudentSeconds,
        minDurationSeconds,
        lastTurnTranscript: "",
        lastExaminerReply: "",
        turnIndex,
        followUpAlreadyAsked: flowState.followUpAsked,
        followUpAnsweredAlready: flowState.followUpAnswered,
        answersFollowUp: false,
      });
    },
    recordTurnStart(now = performance.now()): void {
      turnStartedAt = now;
    },
    accumulateSpeakingTime(now = performance.now()): number {
      const turnMs = turnStartedAt === null ? 0 : now - turnStartedAt;
      turnStartedAt = null;
      return Math.max(0, Math.round(turnMs / 1000));
    },
    creditSpeech(wordCount, micOpenSeconds): void {
      // No input transcription = Gemini heard silence: credit 0 s, never mic-open time.
      cumulativeStudentSeconds += Math.min(micOpenSeconds, (wordCount ?? 0) / WORDS_PER_SECOND);
    },
    recordFollowUpCue(): void {
      followUpCueCount += 1;
    },
    updateFlowState({ lastTurnTranscript, lastExaminerReply }): FlowState {
      turnIndex += 1;
      const wasAsked = flowState.followUpAsked;
      const followUpAsked =
        wasAsked ||
        matchesFollowUp(lastExaminerReply) ||
        followUpCueCount >= 2;
      // Only answers after the turn that asked count: a transcript arriving in
      // the same call as the question was spoken before it.
      if (wasAsked && !flowState.followUpAnswered) {
        wordsSinceFollowUp += tokenize(lastTurnTranscript).length;
      }
      flowState = deriveFlowState({
        studentSpeakingSecondsCumulative: cumulativeStudentSeconds,
        minDurationSeconds,
        lastTurnTranscript,
        lastExaminerReply,
        turnIndex,
        followUpAlreadyAsked: followUpAsked,
        followUpAnsweredAlready: flowState.followUpAnswered,
        answersFollowUp: wasAsked && wordsSinceFollowUp >= FOLLOW_UP_MIN_WORDS,
      });
      return flowState;
    },
    evaluateEndGate(): EndGateResult {
      return evaluateEndGate({
        minDurationMet: flowState.minDurationMet,
        followUpAnswered: flowState.followUpAnswered,
      });
    },
    snapshot(): Readonly<FlowGateSnapshot> {
      return {
        cumulativeStudentSeconds,
        turnIndex,
        minDurationSeconds,
        followUpCueCount,
        flowState,
      };
    },
  };
}
