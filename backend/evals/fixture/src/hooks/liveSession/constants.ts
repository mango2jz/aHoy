/** Keep the Live attempt below the provider's audio-only session limit. */
export const HARD_CAP_MS = 14 * 60 * 1000;
/** Closing deadline: finalize even if the goodbye generation never reports turnComplete. */
export const APPROVED_END_FALLBACK_MS = 20_000;
/** Never retain closing examiner audio indefinitely after an approved end. */
export const FAREWELL_CLEANUP_MS = 15_000;
/** Rolling window re-armed by examiner output only (never by input transcription). */
export const EXAMINER_REPLY_TIMEOUT_MS = 20_000;
/**
 * How long after opening a generation (activityEnd, greeting) the first
 * examiner output may take. A stalled answer shows up as silence here, so this
 * is short; the rolling window above applies once the reply has started.
 */
export const EXAMINER_FIRST_CONTENT_TIMEOUT_MS = 8_000;
export const LIVE_TOKEN_TIMEOUT_MS = 10_000;
export const LIVE_INSTRUCTION_TIMEOUT_MS = 5_000;
export const LIVE_HANDSHAKE_TIMEOUT_MS = 15_000;
/** Wait before retrying a setup the server refused with 1008 "already connected". */
export const RECONNECT_CONFLICT_DELAY_MS = 1_000;
/**
 * After the end gate approves, the next answer is the closing one. If the
 * student goes quiet, the partner nudges once at IDLE_NUDGE_MS and closes the
 * attempt with a standalone goodbye at IDLE_GOODBYE_MS. Both count from the
 * first mic unlock after approval.
 */
export const IDLE_NUDGE_MS = 45_000;
export const IDLE_GOODBYE_MS = 120_000;
