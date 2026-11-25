// Pure capture-error → human message mapper. No DOM mutation, no React — node-testable
// (DOMException is referenced via typeof guard so it degrades gracefully under node).

export function describeCaptureError(err: unknown): string {
  if (typeof DOMException !== "undefined" && err instanceof DOMException) {
    if (err.name === "NotAllowedError" || err.name === "SecurityError") {
      return "Microphone access is blocked — enable it in your browser settings, then try again.";
    }
    if (err.name === "NotFoundError") {
      return "No microphone was found. Connect one and try again.";
    }
  }
  return err instanceof Error ? err.message : String(err);
}
