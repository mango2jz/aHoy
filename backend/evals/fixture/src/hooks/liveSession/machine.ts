import { useReducer } from "react";
import type { CaptureStatus } from "./types.ts";

export interface SessionMachineState {
  status: CaptureStatus;
  message: string;
  canRetry: boolean;
  finalizing: boolean;
  finalized: boolean;
}

export type SessionEvent =
  | { type: "STATUS"; status: CaptureStatus; message?: string }
  | { type: "FINALIZING" }
  | { type: "FINALIZED" }
  | { type: "FINALIZE_FAILED"; message: string };

export const initialSessionState: SessionMachineState = {
  status: "idle",
  message: "",
  canRetry: false,
  finalizing: false,
  finalized: false,
};

export function sessionMachineReducer(
  state: SessionMachineState,
  event: SessionEvent,
): SessionMachineState {
  if (
    state.finalized &&
    event.type === "STATUS" &&
    event.status !== "completed"
  )
    return state;
  switch (event.type) {
    case "STATUS":
      if (
        state.finalizing &&
        ["idle", "ready", "recording", "speaking"].includes(event.status)
      )
        return state;
      return {
        ...state,
        status: event.status,
        message: event.message ?? "",
        canRetry: event.status === "error" ? state.canRetry : false,
      };
    case "FINALIZING":
      return {
        ...state,
        status: "processing",
        message: "Scoring your session…",
        finalizing: true,
        canRetry: false,
      };
    case "FINALIZED":
      return {
        ...state,
        status: "completed",
        message: "Session ended — see your feedback.",
        finalizing: false,
        finalized: true,
        canRetry: false,
      };
    case "FINALIZE_FAILED":
      return {
        ...state,
        status: "error",
        message: event.message,
        finalizing: false,
        canRetry: true,
      };
  }
}

export function useSessionMachine() {
  return useReducer(sessionMachineReducer, initialSessionState);
}
