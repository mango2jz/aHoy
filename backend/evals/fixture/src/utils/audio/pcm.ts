// Pure PCM/WAV codec helpers — salvaged from the old useLiveCapture WAV fallback path. Used when a
// browser lacks MediaRecorder audio support: raw Float32 PCM frames are downsampled, packed to
// 16-bit, and wrapped as a WAV Blob. No DOM, no React — unit-testable under vitest's node env.

/** Target sample rate for the WAV fallback (16 kHz mono). */
export const WAV_TARGET_RATE = 16_000;

/** Average-decimate a Float32 PCM buffer from `inRate` down to `outRate`. */
export function downsampleBuffer(
  buffer: Float32Array,
  inRate: number,
  outRate: number,
): Float32Array {
  if (outRate >= inRate) return buffer;
  const ratio = inRate / outRate;
  const newLen = Math.round(buffer.length / ratio);
  const result = new Float32Array(newLen);
  let offsetResult = 0;
  let offsetBuffer = 0;
  while (offsetResult < newLen) {
    const nextOffsetBuffer = Math.round((offsetResult + 1) * ratio);
    let accum = 0;
    let count = 0;
    for (let i = offsetBuffer; i < nextOffsetBuffer && i < buffer.length; i++) {
      accum += buffer[i]!;
      count++;
    }
    result[offsetResult] = count > 0 ? accum / count : 0;
    offsetResult++;
    offsetBuffer = nextOffsetBuffer;
  }
  return result;
}

/** Convert a Float32 PCM buffer to little-endian 16-bit PCM bytes. */
export function floatTo16BitPCM(input: Float32Array): ArrayBuffer {
  const buffer = new ArrayBuffer(input.length * 2);
  const view = new DataView(buffer);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]!));
    view.setInt16(i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buffer;
}

/** Wrap concatenated Float32 mono frames as a 16-bit PCM WAV Blob at `sampleRate`. */
export function encodeWav(frames: Float32Array[], sampleRate: number): Blob {
  let total = 0;
  for (const f of frames) total += f.length;
  const merged = new Float32Array(total);
  let offset = 0;
  for (const f of frames) {
    merged.set(f, offset);
    offset += f.length;
  }
  const pcm = floatTo16BitPCM(merged);
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  const writeStr = (pos: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(pos + i, s.charCodeAt(i));
  };
  const dataLen = pcm.byteLength;
  writeStr(0, "RIFF");
  view.setUint32(4, 36 + dataLen, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byte rate (mono, 16-bit)
  view.setUint16(32, 2, true); // block align
  view.setUint16(34, 16, true); // bits per sample
  writeStr(36, "data");
  view.setUint32(40, dataLen, true);
  return new Blob([header, pcm], { type: "audio/wav" });
}
