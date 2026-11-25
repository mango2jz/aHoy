import {tokenize} from './speechMetrics.ts';

function wordSyllables(word: string): number {
    if (/^\d+$/.test(word)) return 1;
    if (!/\p{L}/u.test(word)) return 0;
    let count = word.match(/[aeiouy]+/g)?.length ?? 0;
    // A lone final "e" is silent ("make"), except "-le" after a consonant ("table").
    if (/[^aeiouy]e$/.test(word) && !/[^aeiouyl]le$/.test(word)) count--;
    // "-ed" is silent unless after t/d ("worked" vs "wanted").
    else if (/[^aeiouytd]ed$/.test(word)) count--;
    // "-es" is silent unless after s, x, z, ch, sh, c, g ("makes" vs "boxes").
    else if (/[^aeiouysxzcg]es$/.test(word) && !/[cs]hes$/.test(word)) count--;
    return Math.max(1, count);
}

/**
 * Syllables in a transcript, for speech rate.
 * ponytail: vowel-group heuristic, roughly ±10 % on running speech; swap for a
 * pronunciation dictionary (e.g. CMUdict) if exact counts matter. Known misses:
 * adjacent vowels said apart ("idea", "area", "create") and "business".
 */
export function countSyllables(text: string): number {
    return tokenize(text).reduce((sum, word) => sum + wordSyllables(word), 0);
}
