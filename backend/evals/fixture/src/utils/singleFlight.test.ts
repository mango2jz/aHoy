import {describe, it, expect, vi} from '../../test-support/vitest.ts';
import {createOnceGate} from './singleFlight.ts';

describe('createOnceGate', () => {
    it('runs the task once and no-ops on subsequent calls', async () => {
        const gate = createOnceGate();
        const task = vi.fn(async () => {});
        await gate.run(task);
        await gate.run(task);
        await gate.run(task);
        expect(task).toHaveBeenCalledTimes(1);
    });

    it('hasRun flips true after the first run', async () => {
        const gate = createOnceGate();
        expect(gate.hasRun()).toBe(false);
        await gate.run(async () => {});
        expect(gate.hasRun()).toBe(true);
    });

    it('a concurrent second call while the first is in flight is a no-op', async () => {
        const gate = createOnceGate();
        const task = vi.fn(async () => {
            await new Promise((resolve) => setTimeout(resolve, 10));
        });
        const p1 = gate.run(task);
        const p2 = gate.run(task); // `started` is already true synchronously
        await Promise.all([p1, p2]);
        expect(task).toHaveBeenCalledTimes(1);
    });
});
