import {describe, expect, it} from '../../test-support/vitest.ts';
import {looksLikeClosing, looksLikeFarewell} from './closingDetector.ts';

describe('looksLikeClosing', () => {
    it('detects a spoken wrap-up without a tool call', () => {
        expect(
            looksLikeClosing(
                'It seems like we have a good potential plan then, focusing on an influencer partnership. Thanks for discussing this with me!',
            ),
        ).toBe(true);
        expect(looksLikeClosing("Well, I think we've had a really productive discussion today.")).toBe(true);
    });

    it('ignores replies that keep the conversation open', () => {
        expect(looksLikeClosing('Thanks for sharing that. What other options come to mind?')).toBe(false);
        expect(looksLikeClosing('Adding new items is probably the safest way.')).toBe(false);
        expect(looksLikeClosing('')).toBe(false);
    });
});

describe('looksLikeFarewell', () => {
    const SCRIPTED = 'Alright, I will take your ideas into consideration. Now you can continue your work. I need to go now. See you.';

    it('detects explicit goodbyes, including the scripted closing line', () => {
        expect(looksLikeFarewell(SCRIPTED)).toBe(true);
        expect(looksLikeClosing(SCRIPTED)).toBe(true);
        expect(looksLikeFarewell('That is an excellent idea. Goodbye!')).toBe(true);
        expect(looksLikeFarewell('Thank you for your time. See you soon.')).toBe(true);
    });

    it('does not treat wrap-up talk or ordinary sentences as a goodbye', () => {
        expect(looksLikeFarewell('Thanks for your input. I think the social media idea is the strongest.')).toBe(false);
        expect(looksLikeClosing('Thanks for your input. I think the social media idea is the strongest.')).toBe(true);
        expect(looksLikeFarewell('I see you have thought about the cost.')).toBe(false);
        expect(looksLikeFarewell('Goodbye for now, but do you have any other idea?')).toBe(false);
        expect(looksLikeFarewell('')).toBe(false);
    });
});
