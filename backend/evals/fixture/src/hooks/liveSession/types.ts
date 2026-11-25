// Types for the Gemini Live WebSocket practice session.

import type {SessionFeedbackPayload} from "@/types/feedback";

// ---------------------------------------------------------------------------
// Session status
// ---------------------------------------------------------------------------

/** States of the live Gemini WebSocket session. */
export type LiveSessionStatus =
  | "disconnected"     // Initial / after clean end
  | "connecting"       // WS handshake in progress
  | "connected"        // WS open, awaiting audio activity
  | "speaking"         // Mic active, sending audio to WS
  | "listening"        // AI response arriving, audio playing
  | "interrupted"      // Barge-in during AI playback
  | "ended"            // Session cleanly ended via user action
  | "error";           // Fatal error (WS drop, auth failure, etc.)


// ---------------------------------------------------------------------------
// Shared practice status (used by UI components)
// ---------------------------------------------------------------------------

/** Status shared across the live capture flow and its session finalizer. */
export type CaptureStatus =
  | "idle"
  | "connecting"
  | "ready"
  | "recording"
  | "processing"
  | "speaking"
  | "completed"
  | "error"
  | "secondary";

export interface CaptureResult {
  turnUuid: string;
  userTurnIndex: number;
  aiTurnIndex: number;
  examinerAudioUrl: string | null;
  studentTranscript: string;
  examinerReply: string;
  /** True when the examiner's reply naturally concludes the conversation (farewell, goodbye). */
  conversationComplete?: boolean;
}

export interface CaptureOptions {
  sessionId: string;
  scenarioId: string;
  onTurn?: (result: CaptureResult) => void;
  onError?: (message: string) => void;
  /**
   * Supplies the FULL reducer-derived transcript at finalize time (never the
   * capped history). May return '' to force a DB rebuild in `scoreSession`.
   */
  getTranscript?: () => string;
  /**
   * Called once when the attempt has ended. `payload` carries the authoritative
   * feedback, or is null when the attempt ended early and was not scored.
   */
  onFinalized?: (payload: SessionFeedbackPayload | null, info: {warning?: string}) => void;
  /**
   * True for a completed/abandoned session loaded read-only. Must be
   * mount-stable — the strategy is constructed once, so a later change has no
   * effect.
   */
  readOnly?: boolean;
}

export interface CaptureHandle {
  status: CaptureStatus;
  statusMessage: string;
  isSecondary: boolean;
  isBusy: boolean;
  elapsedMs: number;
  canRetry: boolean;
  /** Connect to the Gemini Live API. */
  connect: () => Promise<void>;
  startRecording: () => Promise<void>;
  stopRecording: () => Promise<void>;
  cancelRecording: () => void;
  /**
   * Single-flight finalize: end the attempt and produce the authoritative
   * feedback. The first of {client-owned close, manual button, hard cap} wins;
   * the rest are no-ops. Safe to call from the manual End-session button.
   */
  endSession: () => Promise<void>;
  /** True while the finalize round-trip is in flight (drives button loading). */
  finalizing: boolean;
  /** True once the attempt has been finalized (button disabled / no re-entry). */
  finalized: boolean;
}

// ---------------------------------------------------------------------------
// Session errors
// ---------------------------------------------------------------------------

export interface LiveSessionError {
  /** Machine-readable code (e.g. "ws_disconnect", "auth_failed", "gemini_error"). */
  code: string;
  /** Human-readable description. */
  message: string;
  /** When true, the session cannot continue. */
  fatal: boolean;
  /** Timestamp (Date.now()) when the error occurred. */
  timestamp: number;
  /** Optional diagnostic context. */
  context?: Record<string, unknown>;
}
