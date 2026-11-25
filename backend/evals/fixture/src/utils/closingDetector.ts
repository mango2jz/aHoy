/**
 * Does the partner's reply read as a goodbye? Before the end gate approves, the
 * client never closes on the model's words: a closing-sounding reply makes the
 * next answer carry a cue telling the partner to keep the conversation going.
 * Once the gate has approved, an explicit farewell ends the session.
 */

/** Explicit leave-taking. Strict on purpose: a match can end an approved session. */
const FAREWELL_PATTERNS: RegExp[] = [
    /\b(?:good ?bye|bye for now|see you (?:next time|soon|later))\b/i,
    // Bare "See you." only at the very end: "I see you have thought about it" must not match.
    /\bsee you[.!]*\s*$/i,
    /\bI (?:need|have|'ve got) to go\b/i,
];

/** Wrap-up talk that often precedes a goodbye but is also a normal way to react to an answer. */
const WRAP_UP_PATTERNS: RegExp[] = [
    /\bthanks? (?:you )?for (?:discussing|sharing|talking|your (?:time|thoughts|ideas|input))\b/i,
    /\b(?:that'?s|that is) (?:all|it) for (?:today|now)\b/i,
    /\b(?:we'?ve had|we have had|this has been|it'?s been|that was) a (?:really |very )?(?:good|great|productive|useful|helpful|nice) (?:discussion|conversation|chat|talk)\b/i,
    /\b(?:let'?s|we'?ll) (?:wrap|wrap it) up\b/i,
    /\bgood luck\b/i,
];

function matches(reply: string, patterns: RegExp[]): boolean {
    const text = reply.trim();
    if (!text) return false;
    // A goodbye followed by a question is still an open conversation.
    if (/\?\s*$/.test(text)) return false;
    return patterns.some((re) => re.test(text));
}

/** The reply says goodbye in so many words. */
export function looksLikeFarewell(reply: string): boolean {
    return matches(reply, FAREWELL_PATTERNS);
}

/** The reply sounds like the conversation is being closed (farewell or wrap-up talk). */
export function looksLikeClosing(reply: string): boolean {
    return matches(reply, [...FAREWELL_PATTERNS, ...WRAP_UP_PATTERNS]);
}
