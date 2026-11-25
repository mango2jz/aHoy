/**
 * Small lifetime gate for asynchronous work which may settle after teardown.
 * A deferred callback is only allowed to run for the epoch that scheduled it.
 */
export interface DeferredLifetimeCoordinator {
  advance(): void;
  defer(callback: () => void): void;
}

export function createDeferredLifetimeCoordinator(
  schedule: (callback: () => void) => unknown = queueMicrotask,
): DeferredLifetimeCoordinator {
  let currentEpoch = 0;
  const pending = new Set<() => void>();

  return {
    advance: () => {
      currentEpoch += 1;
    },
    defer(callback) {
      const expectedEpoch = currentEpoch;
      const guarded = () => {
        pending.delete(guarded);
        if (expectedEpoch === currentEpoch) callback();
      };
      pending.add(guarded);
      schedule(guarded);
    },
  };
}

export function enterDeferredLifetime(
  coordinator: DeferredLifetimeCoordinator,
  dispose: () => void,
): () => void {
  coordinator.advance();
  return () => coordinator.defer(dispose);
}
