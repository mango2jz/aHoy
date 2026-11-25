// sentenceSplitter — Chunks examiner reply text into sentences for pipelined TTS.
// Uses a marker-based approach: protects abbreviations, decimal points, and ellipsis
// before splitting on sentence-ending punctuation, then restores markers.

// Unique markers (highly unlikely in Gemini text output).
const ABBR = '__ABBR__';
const DEC = '__DEC__';
const ELPS = '__ELPS__';
const BOUNDARY_MARKER = '__BOUNDARY__';

// Known abbreviations whose period should NOT trigger a sentence break.
const ABBREVIATIONS = [
  'dr', 'mr', 'ms', 'mrs', 'prof', 'sr', 'jr', 'st', 'hon', 'rev',
  'inc', 'ltd', 'corp', 'co', 'llc', 'lp',
  'ave', 'blvd', 'rd', 'sq', 'mt', 'ft',
  'jan', 'feb', 'mar', 'apr', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec',
  'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun',
  'dept', 'est', 'govt', 'tel', 'ext', 'fig', 'vs', 'approx',
  'p', 'pp', 'ch', 'sec', 'vol', 'no',
  'etc', 'al',
];

/** Merge enumeration fragments into numbered-list items. */
function mergeEnumeration(segments: string[]): string[] {
  const result: string[] = [];
  for (let i = 0; i < segments.length; i++) {
    const cur = segments[i];
    const next = i + 1 < segments.length ? segments[i + 1] : null;

    if (next && /^\d+\.\s*$/.test(cur)) {
      result.push(cur.trim() + ' ' + next);
      i++;
    } else if (next && /\d\.\s*$/.test(cur.trim())) {
      result.push(cur.trim() + ' ' + next);
      i++;
    } else {
      result.push(cur);
    }
  }
  return result;
}

/**
 * Split examiner reply text into individual sentences ready for pipelined TTS.
 *
 * Uses a marker-based protection approach:
 * 1. Replace known abbreviations, initialisms, decimals, and mid-sentence ellipsis
 *    with unique markers so their periods don't trigger splits.
 * 2. Split the remaining text on . ! ? followed by whitespace.
 * 3. Restore markers to their original visual form.
 *
 * **Known limitation:** When a protected abbreviation falls at a sentence boundary
 * the period serves as both abbreviation marker and sentence end — the splitter
 * protects the abbreviation and does NOT split at that point.
 *
 * @param text - The examiner reply string.
 * @returns An array of sentence strings, trimmed and non-empty.
 */
export function splitSentences(text: string): string[] {
  if (!text?.trim()) return [];

  const normal = text.replace(/\s+/g, ' ').trim();
  let s = normal;

  // --- Inline regexes (fresh each call — avoids lastIndex state issues) ---

  // 1. Handle ellipsis.
  s = s.replace(
    /\.\.\.\s+(?=[A-Z])/g,
    (m: string) => m.replace(/\.\.\./g, BOUNDARY_MARKER),
  );
  s = s.replace(/\.\.\./g, ELPS);
  s = s.replace(new RegExp(BOUNDARY_MARKER, 'g'), '...');
  // 2. Protect initialisms (U.S., U.S.A., U.K., etc.)
  s = s.replace(
    /\b([A-Z]\.){2,}(?=\s|$)/g,
    (m: string) => m.replace(/\./g, ABBR),
  );

  // 3. Protect Latin dotted abbreviations (e.g., i.e.)
  s = s.replace(
    /\b(?:e\.g|i\.e)\.(?=\s|$)/gi,
    (m: string) => m.replace(/\./g, ABBR),
  );

  // 4. Protect known abbreviations.
  const abbrPattern = '\\b(?:' + ABBREVIATIONS.join('|') + ')\\.(?=\\s|$)';
  const abbrRe = new RegExp(abbrPattern, 'gi');
  s = s.replace(abbrRe, (m: string) => m.replace('.', ABBR));

  // 5. Protect decimal numbers.
  s = s.replace(/(\d)\.(\d)/g, (_m: string, a: string, b: string) => a + DEC + b);

  // 6. Split on . ! ? optionally followed by closing " ' ) and then whitespace.
  const parts = s.split(/(?<=[.!?]['")]*)\s+/);

  // 7. Restore markers.
  const restored = parts
    .map((p: string) =>
      p
        .replace(new RegExp(ELPS, 'g'), '...')
        .replace(new RegExp(ABBR, 'g'), '.')
        .replace(new RegExp(DEC, 'g'), '.')
        .trim(),
    )
    .filter((p: string) => p.length > 0);

  // 8. Merge enumeration fragments.
  return mergeEnumeration(restored);
}
