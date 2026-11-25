interface SessionState {
    status: CaptureStatus,
    statusMessage: string,
    canRetry: boolean,
    finalizing: boolean,
    finalized: boolean,
}

type CaptureStatus =
    "idle"
    | "connecting"
    | "ready"
    | "recording"
    | "speaking"
    | "processing"
    | "completed"
    | "error"
    | "secondary"

