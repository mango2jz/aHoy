import type { CaptureOptions, CaptureStatus } from "./types.ts";

export interface CommittedRuntime {
  onTurn?: CaptureOptions["onTurn"];
  onError?: CaptureOptions["onError"];
  getTranscript?: CaptureOptions["getTranscript"];
  onFinalized?: CaptureOptions["onFinalized"];
  getStatus(): CaptureStatus;
}

/**
 * Keeps long-lived transport callbacks on committed React values. Calling
 * `commit` from an effect avoids mutating lifecycle state during render.
 */
export function createCommittedRuntime(): {
  current(): CommittedRuntime;
  commit(
    next: Omit<CommittedRuntime, "getStatus"> & {
      getStatus: () => CaptureStatus;
    },
  ): void;
} {
  let value: CommittedRuntime = { getStatus: () => "idle" };
  return {
    current: () => value,
    commit: (next) => {
      value = next;
    },
  };
}
