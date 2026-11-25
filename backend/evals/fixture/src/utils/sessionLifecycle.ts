/**
 * Pure helpers for practice-session lifecycle decisions (staleness + concurrency
 * cap). Kept side-effect-free so the count/staleness/auto-recycle logic is unit
 * testable without a Supabase mock; the DB writes live in practiceServices.
 */

/** Max concurrent ACTIVE sessions a single user may hold. Hard cap, not MVP. */
export const MAX_ACTIVE_SESSIONS = 3;

/** An active session is considered abandoned after this idle age. */
export const STALE_SESSION_MS = 2 * 60 * 60 * 1000; // 2 hours

/** True when an active session started long enough ago to be treated as stale. */
export function isSessionStale(startedAt: string | null, now: number): boolean {
    if (!startedAt) return false;
    const started = Date.parse(startedAt);
    if (Number.isNaN(started)) return false;
    return now - started >= STALE_SESSION_MS;
}

interface ActiveSessionRef {
    id: string;
    started_at: string | null;
}

/**
 * Given the user's current active (non-stale) sessions oldest-first and the cap,
 * return the ids of the oldest sessions to auto-abandon so that creating ONE new
 * session keeps the total within the cap. Returns [] when there is already room.
 * This is the graceful "recycle oldest" policy — never a hard lockout.
 */
export function selectSessionsToAbandon(active: ActiveSessionRef[], cap: number = MAX_ACTIVE_SESSIONS): string[] {
    if (cap < 1) return active.map((s) => s.id);
    if (active.length < cap) return [];
    const removeCount = active.length - cap + 1;
    return active.slice(0, removeCount).map((s) => s.id);
}
