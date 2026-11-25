import { describe, expect, it } from "../../../../test-support/vitest.ts";
import { resolveErrorPrimaryAction } from "../primaryAction.ts";

describe("resolveErrorPrimaryAction", () => {
  it("retries finalization when the error is retryable", () => {
    expect(resolveErrorPrimaryAction("error", true)).toBe("endSession");
  });

  it("starts a new recording for capture errors", () => {
    expect(resolveErrorPrimaryAction("error", false)).toBe("startRecording");
  });

  it.each(["ready", "recording", "speaking"] as const)(
    "starts recording for non-error status %s even when retryable",
    (status) => {
      expect(resolveErrorPrimaryAction(status, true)).toBe("startRecording");
    },
  );
});
