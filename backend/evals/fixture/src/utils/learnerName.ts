/**
 * Given name for the live greeting: the last whitespace-separated token of
 * `profiles.full_name` (Vietnamese order puts the given name last), reduced to
 * letters, combining marks, apostrophes and hyphens, at most 40 characters.
 * The result goes into text the model reads, so it can never carry
 * punctuation or a sentence. Null when no letter is left.
 */
export function toLearnerName(fullName: string | null | undefined): string | null {
    const last = (fullName ?? '').trim().split(/\s+/).at(-1) ?? '';
    const name = last.replace(/[^\p{L}\p{M}'’-]/gu, '').slice(0, 40);
    return /\p{L}/u.test(name) ? name : null;
}
