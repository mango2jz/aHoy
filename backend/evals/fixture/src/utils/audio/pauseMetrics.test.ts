import {describe, expect, it} from '../../../test-support/vitest.ts';
import {computePauseMetrics} from './pauseMetrics.ts';

const SR = 16000;

function tone(sec: number): Int16Array {
    const out = new Int16Array(Math.round(sec * SR));
    for (let i = 0; i < out.length; i++) out[i] = Math.round(10000 * Math.sin((2 * Math.PI * 440 * i) / SR));
    return out;
}

/** Seeded low-level noise, so the adaptive floor has something to measure. */
function noise(sec: number): Int16Array {
    const out = new Int16Array(Math.round(sec * SR));
    let seed = 12345;
    for (let i = 0; i < out.length; i++) {
        seed = (seed * 1664525 + 1013904223) >>> 0;
        out[i] = Math.round((seed / 0xffffffff - 0.5) * 60);
    }
    return out;
}

function concat(...parts: Int16Array[]): Int16Array {
    const out = new Int16Array(parts.reduce((n, p) => n + p.length, 0));
    let o = 0;
    for (const p of parts) {
        out.set(p, o);
        o += p.length;
    }
    return out;
}

describe('computePauseMetrics', () => {
    it('counts a 0.8 s gap as a silent pause but not a long one', () => {
        const m = computePauseMetrics(concat(noise(0.5), tone(2), noise(0.8), tone(2), noise(0.5)), SR);
        expect(m.silentPauses).toBe(1);
        expect(m.longPauses).toBe(0);
        expect(m.spanSec).toBe(4.8);
    });

    it('counts a 1.2 s gap as a long pause', () => {
        const m = computePauseMetrics(concat(noise(0.5), tone(2), noise(1.2), tone(2), noise(0.5)), SR);
        expect(m.silentPauses).toBe(1);
        expect(m.longPauses).toBe(1);
    });

    it('leaves leading and trailing silence out of the pauses and the span', () => {
        const m = computePauseMetrics(concat(noise(2), tone(3), noise(3)), SR);
        expect(m.silentPauses).toBe(0);
        expect(m.longPauses).toBe(0);
        expect(m.spanSec).toBe(3);
    });

    it('does not count a 0.3 s gap as a pause', () => {
        const m = computePauseMetrics(concat(noise(0.5), tone(2), noise(0.3), tone(2), noise(0.5)), SR);
        expect(m.silentPauses).toBe(0);
        expect(m.pausesPerMin).toBe(0);
    });

    it('returns zeros for empty input', () => {
        expect(computePauseMetrics(new Int16Array(0), SR)).toEqual({
            speechRatio: 0,
            longPauses: 0,
            meanRunSec: 0,
            pausesPerMin: 0,
            silentPauses: 0,
            spanSec: 0,
        });
    });
});
