import {splitSentences} from '../lib/sentenceSplitter.ts';

/**
 * First sentence of `text`, using the same abbreviation/decimal-aware splitter
 * as TTS chunking. Returns the whole trimmed string when there is no sentence
 * terminator, and '' for empty input.
 */
export function firstSentence(text: string): string {
    return splitSentences(text)[0] ?? '';
}
