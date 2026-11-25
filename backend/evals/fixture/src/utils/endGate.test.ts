import {describe, it, expect} from '../../test-support/vitest.ts';
import {END_GATE_TOLERANCE_SECONDS, derivePersistedEndGate, evaluateEndGate} from './endGate.ts';

describe('evaluateEndGate — truth table', () => {
    it('approves only when minDurationMet AND followUpAnswered', () => {
        expect(evaluateEndGate({minDurationMet: true, followUpAnswered: true})).toEqual({result: 'approved'});
    });

    it('continues when the minimum duration is not met', () => {
        const r = evaluateEndGate({minDurationMet: false, followUpAnswered: true});
        expect(r.result).toBe('continue');
        expect(r.reason).toContain('minimum speaking duration');
    });

    it('continues when the follow-up has not been answered', () => {
        const r = evaluateEndGate({minDurationMet: true, followUpAnswered: false});
        expect(r.result).toBe('continue');
        expect(r.reason).toContain('follow-up');
    });

    it('continues when neither condition holds', () => {
        expect(evaluateEndGate({minDurationMet: false, followUpAnswered: false}).result).toBe('continue');
    });
});

describe('derivePersistedEndGate', () => {
    // 120 wpm credits exactly one spoken second per mic-open second (WORDS_PER_SECOND = 2).
    const answer = (duration: number) => ({role: 'user', speech_metrics: {wpm: 120, fillerRate: 0, duration}});
    const window = {startedAt: null, endedAt: null, floorSeconds: 120};

    it('passes at the floor minus the tolerance', () => {
        expect(END_GATE_TOLERANCE_SECONDS).toBe(10);
        expect(derivePersistedEndGate([answer(60), answer(50)], window).eligible).toBe(true);
    });

    it('fails one second below the tolerance', () => {
        expect(derivePersistedEndGate([answer(60), answer(49)], window).eligible).toBe(false);
    });

    it('fails with a single answer however long', () => {
        expect(derivePersistedEndGate([answer(600)], window).eligible).toBe(false);
    });

    it('does not require the follow-up to be detected', () => {
        const rows = [{role: 'ai', speech_metrics: null}, answer(60), {role: 'ai', speech_metrics: null}, answer(60)];
        expect(derivePersistedEndGate(rows, window).eligible).toBe(true);
    });

    it('falls back to wall clock when no turn carries pacing', () => {
        const rows = [answer(0), answer(0)].map((row) => ({...row, speech_metrics: null}));
        const started = '2026-01-01T00:00:00.000Z';
        expect(derivePersistedEndGate(rows, {...window, startedAt: started, endedAt: '2026-01-01T00:01:50.000Z'}).eligible).toBe(true);
        expect(derivePersistedEndGate(rows, {...window, startedAt: started, endedAt: '2026-01-01T00:01:49.000Z'}).eligible).toBe(false);
    });
});
