/**
 * A one-shot gate. The FIRST `run` executes its task; every later call is a
 * no-op. Used to make the live-session finalize single-flight so the competing
 * triggers — the client-owned close, the manual End button, and the hard-cap timer —
 * can all call it, but `scoreSession` runs exactly once per attempt.
 */
export interface OnceGate {
    /** True once `run` has been invoked (whether in flight or completed). */
    hasRun(): boolean;
    /** Execute `task` only on the first call; subsequent calls resolve immediately. */
    run(task: () => Promise<void>): Promise<void>;
}

export function createOnceGate(): OnceGate {
    let started = false;
    return {
        hasRun: () => started,
        run: async (task: () => Promise<void>): Promise<void> => {
            if (started) return;
            started = true;
            await task();
        },
    };
}
