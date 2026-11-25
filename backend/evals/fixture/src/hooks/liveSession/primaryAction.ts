import type { CaptureStatus } from "./types.ts";

export type ErrorPrimaryAction = "endSession" | "startRecording";

/** Selects the primary action for a capture error. */
export function resolveErrorPrimaryAction(
  status: CaptureStatus,
  canRetry: boolean,
): ErrorPrimaryAction {
  return status === "error" && canRetry ? "endSession" : "startRecording";
}
