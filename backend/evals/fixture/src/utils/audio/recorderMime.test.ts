import { describe, expect, it } from "../../../test-support/vitest.ts";
import { extForMime, pickRecorderMime } from "./recorderMime.ts";

describe("extForMime", () => {
  it.each([
    ["audio/webm;codecs=opus", "webm"],
    ["audio/webm", "webm"],
    ["audio/ogg;codecs=opus", "ogg"],
    ["audio/ogg", "ogg"],
    ["audio/mp4", "mp4"],
    ["audio/wav", "wav"],
    ["audio/flac", "bin"],
    ["", "bin"],
  ])("maps %s → %s", (mime, ext) => {
    expect(extForMime(mime)).toBe(ext);
  });
});

describe("pickRecorderMime", () => {
  it("returns null when MediaRecorder is unavailable (node env)", () => {
    expect(pickRecorderMime()).toBeNull();
  });
});
