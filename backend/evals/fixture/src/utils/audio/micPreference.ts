/**
 * The student's chosen microphone, kept per browser. Browsers remember the mic
 * permission but always hand back the system default device, so without this
 * the practice session silently records whatever the OS picked.
 *
 * Storage can be missing or throw (private windows, blocked site data); then
 * the choice lasts for this page only. `null` means the system default.
 */
const STORAGE_KEY = 'speaklab.micDeviceId';

const listeners = new Set<() => void>();
/** Used when storage throws, so a choice still lasts for this page. */
let memoryValue: string | null = null;

export function readMicDeviceId(): string | null {
    try {
        return window.localStorage.getItem(STORAGE_KEY) || null;
    } catch {
        return memoryValue;
    }
}

/** `null` selects the system default device. */
export function writeMicDeviceId(deviceId: string | null): void {
    memoryValue = deviceId;
    try {
        if (deviceId) window.localStorage.setItem(STORAGE_KEY, deviceId);
        else window.localStorage.removeItem(STORAGE_KEY);
    } catch {
        /* Storage unavailable: `memoryValue` keeps the choice for this page. */
    }
    listeners.forEach((listener) => listener());
}

/** `useSyncExternalStore` subscription; also follows changes from other tabs. */
export function subscribeMicDeviceId(listener: () => void): () => void {
    listeners.add(listener);
    const onStorage = (event: StorageEvent) => {
        if (event.key === STORAGE_KEY) listener();
    };
    window.addEventListener('storage', onStorage);
    return () => {
        listeners.delete(listener);
        window.removeEventListener('storage', onStorage);
    };
}
