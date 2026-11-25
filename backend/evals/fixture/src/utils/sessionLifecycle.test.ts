import {describe, it, expect} from '../../test-support/vitest.ts';
import {
    MAX_ACTIVE_SESSIONS,
    STALE_SESSION_MS,
    isSessionStale,
    selectSessionsToAbandon,
} from './sessionLifecycle.ts';

const NOW = Date.parse('2026-07-01T12:00:00.000Z');

describe('isSessionStale', () => {
    it('is false for a recent session', () => {
        const recent = new Date(NOW - 5 * 60 * 1000).toISOString();
        expect(isSessionStale(recent, NOW)).toBe(false);
    });

    it('is true once older than the stale window', () => {
        const old = new Date(NOW - STALE_SESSION_MS - 1000).toISOString();
        expect(isSessionStale(old, NOW)).toBe(true);
    });

    it('is true exactly at the boundary', () => {
        const boundary = new Date(NOW - STALE_SESSION_MS).toISOString();
        expect(isSessionStale(boundary, NOW)).toBe(true);
    });

    it('is false for null or unparseable timestamps', () => {
        expect(isSessionStale(null, NOW)).toBe(false);
        expect(isSessionStale('not-a-date', NOW)).toBe(false);
    });
});

describe('selectSessionsToAbandon', () => {
    const mk = (...ids: string[]) => ids.map((id) => ({id, started_at: null}));

    it('returns [] when under the cap', () => {
        expect(selectSessionsToAbandon(mk('a', 'b'), 3)).toEqual([]);
    });

    it('abandons the single oldest when exactly at the cap', () => {
        // oldest-first input; one slot must free up so the new session fits.
        expect(selectSessionsToAbandon(mk('a', 'b', 'c'), 3)).toEqual(['a']);
    });

    it('abandons enough oldest sessions when over the cap', () => {
        expect(selectSessionsToAbandon(mk('a', 'b', 'c', 'd', 'e'), 3)).toEqual(['a', 'b', 'c']);
    });

    it('uses the default cap when none supplied', () => {
        const active = mk('a', 'b', 'c');
        expect(selectSessionsToAbandon(active)).toEqual(
            selectSessionsToAbandon(active, MAX_ACTIVE_SESSIONS),
        );
    });

    it('abandons everything when the cap is below 1', () => {
        expect(selectSessionsToAbandon(mk('a', 'b'), 0)).toEqual(['a', 'b']);
    });
});
