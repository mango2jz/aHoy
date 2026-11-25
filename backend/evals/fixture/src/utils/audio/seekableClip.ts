import {downsampleBuffer, encodeWav, WAV_TARGET_RATE} from './pcm.ts';

/**
 * MediaRecorder WebM (unknown-size Clusters, no Cues) cannot be seeked by the
 * browser's demuxer at all — scrubbing the native transport throws
 * `PIPELINE_ERROR_READ`, and without a `Duration` header the playhead never moves
 * either. Decode the clip once and serve 16 kHz mono PCM WAV instead: exact
 * duration, byte-addressable seeking, ~2 MB per minute of speech.
 *
 * Returns null when the clip cannot be decoded so the caller keeps the original.
 */
export async function toSeekableWav(blob: Blob): Promise<Blob | null> {
    const ctx = new AudioContext();
    try {
        const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
        const mono = decoded.getChannelData(0);
        const rate = Math.min(decoded.sampleRate, WAV_TARGET_RATE);
        return encodeWav([downsampleBuffer(mono, decoded.sampleRate, rate)], rate);
    } catch {
        return null;
    } finally {
        void ctx.close().catch(() => undefined);
    }
}

/** True for clips that need {@link toSeekableWav} before the native transport is usable. */
export function needsSeekableTranscode(src: string, duration: number): boolean {
    if (src.startsWith('blob:')) return false;
    return !Number.isFinite(duration) || /\.webm(\?|#|$)/i.test(src);
}
