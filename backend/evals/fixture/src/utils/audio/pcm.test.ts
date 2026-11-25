import { describe, expect, it } from "../../../test-support/vitest.ts";
import {
  downsampleBuffer,
  encodeWav,
  floatTo16BitPCM,
  WAV_TARGET_RATE,
} from "./pcm.ts";

const readStr = (view: DataView, pos: number, len: number) =>
  Array.from({ length: len }, (_, i) =>
    String.fromCharCode(view.getUint8(pos + i)),
  ).join("");

describe("downsampleBuffer", () => {
  it("returns the input unchanged when outRate >= inRate", () => {
    const buf = new Float32Array([0.1, 0.2, 0.3]);
    expect(downsampleBuffer(buf, 16_000, 16_000)).toBe(buf);
    expect(downsampleBuffer(buf, 16_000, 48_000)).toBe(buf);
  });

  it("decimates to ~len/ratio frames when downsampling", () => {
    const buf = new Float32Array(1000).fill(0.5);
    const out = downsampleBuffer(buf, 48_000, 16_000); // ratio 3
    expect(out.length).toBe(Math.round(1000 / 3));
    // Average of constant 0.5 stays 0.5.
    expect(out[0]).toBeCloseTo(0.5, 5);
  });
});

describe("floatTo16BitPCM", () => {
  it("clamps to ±1 and maps to full 16-bit range", () => {
    const view = new DataView(
      floatTo16BitPCM(new Float32Array([1, -1, 2, -2, 0])),
    );
    expect(view.getInt16(0, true)).toBe(0x7fff);
    expect(view.getInt16(2, true)).toBe(-0x8000);
    expect(view.getInt16(4, true)).toBe(0x7fff); // clamped from 2
    expect(view.getInt16(6, true)).toBe(-0x8000); // clamped from -2
    expect(view.getInt16(8, true)).toBe(0);
  });
});

describe("encodeWav", () => {
  it("writes a valid 44-byte header with correct fmt/data fields", async () => {
    const frames = [new Float32Array([0, 0.5, -0.5]), new Float32Array([0.25])]; // 4 samples total
    const blob = encodeWav(frames, WAV_TARGET_RATE);
    expect(blob.type).toBe("audio/wav");

    const buf = await blob.arrayBuffer();
    const view = new DataView(buf);
    const dataLen = 4 * 2; // 4 samples * 2 bytes

    expect(readStr(view, 0, 4)).toBe("RIFF");
    expect(view.getUint32(4, true)).toBe(36 + dataLen);
    expect(readStr(view, 8, 4)).toBe("WAVE");
    expect(readStr(view, 12, 4)).toBe("fmt ");
    expect(view.getUint16(20, true)).toBe(1); // PCM
    expect(view.getUint16(22, true)).toBe(1); // mono
    expect(view.getUint32(24, true)).toBe(WAV_TARGET_RATE);
    expect(view.getUint32(28, true)).toBe(WAV_TARGET_RATE * 2); // byte rate
    expect(view.getUint16(32, true)).toBe(2); // block align
    expect(view.getUint16(34, true)).toBe(16); // bits per sample
    expect(readStr(view, 36, 4)).toBe("data");
    expect(view.getUint32(40, true)).toBe(dataLen);
    expect(buf.byteLength).toBe(44 + dataLen);
  });
});
