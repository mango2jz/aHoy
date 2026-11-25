// MediaRecorder streams WebM with an unknown-size Segment and no `Duration` in
// the `Info` element, so browsers report `duration === Infinity` and the native
// `<audio controls>` transport cannot draw a progress bar or move the playhead.
// Rewriting the `Info` element with a `Duration` fixes that without re-encoding.

const ID_EBML = 0x1a45dfa3;
const ID_SEGMENT = 0x18538067;
const ID_INFO = 0x1549a966;
const ID_TIMECODE_SCALE = 0x2ad7b1;
const ID_DURATION = 0x4489;
const ID_CLUSTER = 0x1f43b675;

/** Matroska default: one timecode tick = 1 ms. */
const DEFAULT_TIMECODE_SCALE_NS = 1_000_000;

interface Element {
    id: number;
    /** Offset of the first id byte. */
    start: number;
    /** Offset of the first data byte. */
    dataStart: number;
    /** Data byte length; `null` when the size is the "unknown" reserved value. */
    size: number | null;
    /** Offset one past the last data byte (only meaningful when `size` is known). */
    end: number;
}

function readVintLength(first: number): number {
    let mask = 0x80;
    let length = 1;
    while (length <= 8 && !(first & mask)) {
        mask >>= 1;
        length += 1;
    }
    return length;
}

function readId(bytes: Uint8Array, offset: number): {id: number; length: number} | null {
    if (offset >= bytes.length) return null;
    const length = readVintLength(bytes[offset]);
    if (length > 4 || offset + length > bytes.length) return null;
    let id = 0;
    for (let i = 0; i < length; i++) id = id * 256 + bytes[offset + i];
    return {id, length};
}

function readSize(bytes: Uint8Array, offset: number): {size: number | null; length: number} | null {
    if (offset >= bytes.length) return null;
    const length = readVintLength(bytes[offset]);
    if (length > 8 || offset + length > bytes.length) return null;
    let value = bytes[offset] & (0xff >> length);
    let allOnes = value === (0xff >> length);
    for (let i = 1; i < length; i++) {
        const byte = bytes[offset + i];
        if (byte !== 0xff) allOnes = false;
        value = value * 256 + byte;
    }
    return {size: allOnes ? null : value, length};
}

function readElement(bytes: Uint8Array, offset: number): Element | null {
    const id = readId(bytes, offset);
    if (!id) return null;
    const size = readSize(bytes, offset + id.length);
    if (!size) return null;
    const dataStart = offset + id.length + size.length;
    return {
        id: id.id,
        start: offset,
        dataStart,
        size: size.size,
        end: size.size === null ? bytes.length : dataStart + size.size,
    };
}

function readUint(bytes: Uint8Array, element: Element): number {
    let value = 0;
    for (let i = element.dataStart; i < element.end; i++) value = value * 256 + bytes[i];
    return value;
}

function readFloat(bytes: Uint8Array, element: Element): number | null {
    const view = new DataView(bytes.buffer, bytes.byteOffset + element.dataStart, element.size ?? 0);
    if (element.size === 4) return view.getFloat32(0);
    if (element.size === 8) return view.getFloat64(0);
    return null;
}

function encodeSize(size: number, length: number): Uint8Array {
    const out = new Uint8Array(length);
    let rest = size;
    for (let i = length - 1; i >= 0; i--) {
        out[i] = rest % 256;
        rest = Math.floor(rest / 256);
    }
    out[0] |= 0x80 >> (length - 1);
    return out;
}

function durationElement(ticks: number): Uint8Array {
    const out = new Uint8Array(2 + 1 + 8);
    out[0] = 0x44;
    out[1] = 0x89;
    out[2] = 0x88; // 1-byte size vint for 8 data bytes
    new DataView(out.buffer).setFloat64(3, ticks);
    return out;
}

/**
 * Returns a copy of `bytes` whose Segment › Info carries `Duration` = `durationMs`.
 * Returns `null` when the input is not a WebM we can safely patch (or already has
 * a finite duration), so the caller keeps the original.
 */
export function patchWebmDurationBytes(bytes: Uint8Array, durationMs: number): Uint8Array | null {
    if (!Number.isFinite(durationMs) || durationMs <= 0) return null;

    const header = readElement(bytes, 0);
    if (!header || header.id !== ID_EBML || header.size === null) return null;

    const segment = readElement(bytes, header.end);
    if (!segment || segment.id !== ID_SEGMENT) return null;

    let offset = segment.dataStart;
    let info: Element | null = null;
    while (offset < bytes.length) {
        const element = readElement(bytes, offset);
        if (!element || element.size === null) return null;
        if (element.id === ID_INFO) {
            info = element;
            break;
        }
        if (element.id === ID_CLUSTER) return null;
        offset = element.end;
    }
    if (!info || info.size === null) return null;

    let timecodeScaleNs = DEFAULT_TIMECODE_SCALE_NS;
    let existing: Element | null = null;
    offset = info.dataStart;
    while (offset < info.end) {
        const child = readElement(bytes, offset);
        if (!child || child.size === null || child.end > info.end) return null;
        if (child.id === ID_TIMECODE_SCALE) timecodeScaleNs = readUint(bytes, child) || timecodeScaleNs;
        if (child.id === ID_DURATION) existing = child;
        offset = child.end;
    }

    const ticks = (durationMs * 1_000_000) / timecodeScaleNs;

    if (existing) {
        const current = readFloat(bytes, existing);
        if (current !== null && Number.isFinite(current) && current > 0) return null;
        if (existing.size !== 4 && existing.size !== 8) return null;
        const patched = bytes.slice();
        const view = new DataView(patched.buffer, patched.byteOffset + existing.dataStart, existing.size);
        if (existing.size === 8) view.setFloat64(0, ticks);
        else view.setFloat32(0, ticks);
        return patched;
    }

    const extra = durationElement(ticks);
    const infoSize = info.size + extra.length;
    const infoId = readId(bytes, info.start);
    const segmentId = readId(bytes, segment.start);
    if (!infoId || !segmentId) return null;
    const idBytes = bytes.subarray(info.start, info.start + infoId.length);
    const sizeBytes = encodeSize(infoSize, 8);

    // A known-size Segment must grow by the same delta; re-encoding in place only
    // works when the new size still fits the existing vint width.
    let segmentPrefix = bytes.subarray(0, segment.dataStart);
    if (segment.size !== null) {
        const delta = idBytes.length + sizeBytes.length + infoSize - (info.end - info.start);
        const sizeStart = segment.start + segmentId.length;
        const sizeLength = segment.dataStart - sizeStart;
        const newSize = segment.size + delta;
        if (newSize >= 2 ** (7 * sizeLength) - 1) return null;
        const prefix = bytes.slice(0, segment.dataStart);
        prefix.set(encodeSize(newSize, sizeLength), sizeStart);
        segmentPrefix = prefix;
    }

    const out = new Uint8Array(
        segmentPrefix.length +
            (info.start - segment.dataStart) +
            idBytes.length +
            sizeBytes.length +
            infoSize +
            (bytes.length - info.end),
    );
    let cursor = 0;
    const push = (chunk: Uint8Array) => {
        out.set(chunk, cursor);
        cursor += chunk.length;
    };
    push(segmentPrefix);
    push(bytes.subarray(segment.dataStart, info.start));
    push(idBytes);
    push(sizeBytes);
    push(bytes.subarray(info.dataStart, info.end));
    push(extra);
    push(bytes.subarray(info.end));
    return out;
}

/** Blob wrapper over {@link patchWebmDurationBytes}; non-WebM blobs pass through. */
export async function patchWebmDuration(blob: Blob, durationMs: number): Promise<Blob> {
    if (!/webm|matroska/i.test(blob.type)) return blob;
    const bytes = new Uint8Array(await blob.arrayBuffer());
    const patched = patchWebmDurationBytes(bytes, durationMs);
    return patched ? new Blob([patched.slice().buffer], {type: blob.type}) : blob;
}

