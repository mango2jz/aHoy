import { describe, expect, it } from "../../../test-support/vitest.ts";
import { createFlowGateController } from "./flowGate.ts";
import { STANDARD_FOLLOW_UP } from "../../libs/scenarioConstants.ts";

const EIGHT_WORDS = "We could also add a bike-share program downtown.";
const TWENTY_WORDS =
  "I would improve the bus route and add a cycle lane because it is cheaper and faster for everyone in town.";

describe("createFlowGateController", () => {
  it("accumulateSpeakingTime returns mic-open seconds without crediting speech", () => {
    const flow = createFlowGateController();
    flow.configure({ minDurationSeconds: 2 });
    flow.recordTurnStart(1_000);
    expect(flow.accumulateSpeakingTime(2_600)).toBe(2);
    const state = flow.updateFlowState({
      lastTurnTranscript: "",
      lastExaminerReply: "",
    });

    expect(state.minDurationMet).toBe(false);
    expect(flow.snapshot().cumulativeStudentSeconds).toBe(0);
  });

  it("credits min(mic-open, words / WORDS_PER_SECOND), zero when no transcription arrived", () => {
    const zero = createFlowGateController();
    zero.creditSpeech(0, 60);
    expect(zero.snapshot().cumulativeStudentSeconds).toBe(0);

    const sixty = createFlowGateController();
    sixty.creditSpeech(60, 60);
    expect(sixty.snapshot().cumulativeStudentSeconds).toBe(30);

    const unknown = createFlowGateController();
    unknown.creditSpeech(undefined, 60);
    expect(unknown.snapshot().cumulativeStudentSeconds).toBe(0);
  });

  it("minDurationMet reads the credited total", () => {
    const flow = createFlowGateController();
    flow.configure({ minDurationSeconds: 30 });
    flow.creditSpeech(60, 60);
    expect(
      flow.updateFlowState({ lastTurnTranscript: TWENTY_WORDS, lastExaminerReply: "" })
        .minDurationMet,
    ).toBe(true);
  });

  it("does not mark a generic probe as the follow-up", () => {
    const flow = createFlowGateController();
    const state = flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: "Can you tell me more about that idea?",
    });
    expect(state.followUpAsked).toBe(false);
  });

  it("needs FOLLOW_UP_MIN_WORDS across turns after the follow-up is asked", () => {
    const flow = createFlowGateController();
    flow.configure({ minDurationSeconds: 1 });
    flow.creditSpeech(10, 5);
    // Asked this turn: this turn's transcript predates the question.
    const asked = flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: STANDARD_FOLLOW_UP,
    });
    expect(asked.followUpAsked).toBe(true);
    expect(asked.followUpAnswered).toBe(false);

    const first = flow.updateFlowState({
      lastTurnTranscript: EIGHT_WORDS,
      lastExaminerReply: "",
    });
    expect(first.followUpAnswered).toBe(false);
    expect(flow.evaluateEndGate()).toEqual({
      result: "continue",
      reason: expect.stringContaining("follow-up"),
    });

    const second = flow.updateFlowState({
      lastTurnTranscript: EIGHT_WORDS,
      lastExaminerReply: "",
    });
    expect(second.followUpAnswered).toBe(true);
    expect(flow.evaluateEndGate()).toEqual({ result: "approved" });
  });

  it("two follow-up cues force asked, but the same call's transcript does not answer it", () => {
    const flow = createFlowGateController();
    flow.recordFollowUpCue();
    flow.recordFollowUpCue();
    expect(flow.snapshot().followUpCueCount).toBe(2);

    const state = flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: "That sounds expensive. Who would pay for it?",
    });
    expect(state.followUpAsked).toBe(true);
    expect(state.followUpAnswered).toBe(false);
  });

  it("one follow-up cue alone does not force asked", () => {
    const flow = createFlowGateController();
    flow.recordFollowUpCue();
    expect(
      flow.updateFlowState({ lastTurnTranscript: TWENTY_WORDS, lastExaminerReply: "Okay." })
        .followUpAsked,
    ).toBe(false);
  });

  it("configure() after a follow-up was asked but before it was answered does not flip followUpAnswered", () => {
    const flow = createFlowGateController();
    flow.configure({ minDurationSeconds: 1 });
    flow.creditSpeech(10, 5);
    flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: STANDARD_FOLLOW_UP,
    });

    // configure() (e.g. a reconnect) must never advance followUpAnswered.
    flow.configure({ minDurationSeconds: 1 });

    expect(flow.snapshot().flowState.followUpAnswered).toBe(false);
    expect(flow.evaluateEndGate()).toEqual({
      result: "continue",
      reason: expect.stringContaining("follow-up"),
    });
  });

  it("followUpAnswered is sticky across a later configure() once it has flipped true", () => {
    const flow = createFlowGateController();
    flow.configure({ minDurationSeconds: 1 });
    flow.creditSpeech(10, 5);
    flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: STANDARD_FOLLOW_UP,
    });
    flow.updateFlowState({
      lastTurnTranscript: TWENTY_WORDS,
      lastExaminerReply: "",
    });

    flow.configure({ minDurationSeconds: 1 });

    expect(flow.snapshot().flowState.followUpAnswered).toBe(true);
    expect(flow.evaluateEndGate()).toEqual({ result: "approved" });
  });
});
