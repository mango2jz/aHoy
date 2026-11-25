const ENUMERATOR_REGEX =
    /option\s+(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s*[:\-–—]\s*/gi;

export function splitOptionBullets(text: string): {lead: string; options: string[]; tail: string} | null {
    const matches = [...text.matchAll(ENUMERATOR_REGEX)];
    if (matches.length < 2) return null;

    const lead = text.slice(0, matches[0].index).trim();
    const options: string[] = [];

    for (let i = 0; i < matches.length; i++) {
        const start = matches[i].index + matches[i][0].length;
        const end = i + 1 < matches.length ? matches[i + 1].index : text.length;
        options.push(text.slice(start, end).trim());
    }

    let tail = '';
    const lastOption = options[options.length - 1];
    const sentenceEnd = lastOption.match(/[.!?](\s+|$)/);
    if (sentenceEnd) {
        const splitAt = sentenceEnd.index! + sentenceEnd[0].length;
        tail = lastOption.slice(splitAt).trim();
        options[options.length - 1] = lastOption.slice(0, sentenceEnd.index! + 1).trim();
    }

    return {lead, options, tail};
}
