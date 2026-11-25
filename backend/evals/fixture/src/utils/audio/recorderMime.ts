// Pure MediaRecorder mime/codec selection helpers. No DOM mutation, no React — node-testable.

/** Pick the best supported MediaRecorder mime, or null when MediaRecorder audio is unavailable. */
export function pickRecorderMime(): string | null {
  if (typeof MediaRecorder === "undefined") return null;
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4",
  ];
  return candidates.find((c) => MediaRecorder.isTypeSupported(c)) ?? null;
}

/** Map a mime (with optional `;codecs=…`) to a storage file extension. */
export function extForMime(mime: string): string {
  const base = mime.split(";")[0]!;
  if (base.includes("webm")) return "webm";
  if (base.includes("ogg")) return "ogg";
  if (base.includes("mp4")) return "mp4";
  if (base.includes("wav")) return "wav";
  return "bin";
}
