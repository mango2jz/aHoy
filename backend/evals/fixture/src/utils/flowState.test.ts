import {describe, it, expect} from '../../test-support/vitest.ts';
import {deriveFlowState, detectFollowUp, matchesFollowUp, type FlowSignals} from './flowState.ts';
import {STANDARD_FOLLOW_UP} from '../libs/scenarioConstants.ts';

const base: FlowSignals = {
    studentSpeakingSecondsCumulative: 0,
    minDurationSeconds: 120,
    lastTurnTranscript:
        'We could partner with local Indian restaurants to source authentic spices and recipes',
    lastExaminerReply: '',
    turnIndex: 1,
    followUpAlreadyAsked: false,
    followUpAnsweredAlready: false,
    answersFollowUp: false,
};

describe('deriveFlowState — minDurationMet boundary', () => {
    it('is false at 119s (< min)', () => {
        expect(deriveFlowState({...base, studentSpeakingSecondsCumulative: 119}).minDurationMet).toBe(false);
    });
    it('is true at exactly 120s (>= min)', () => {
        expect(deriveFlowState({...base, studentSpeakingSecondsCumulative: 120}).minDurationMet).toBe(true);
    });
});

describe('deriveFlowState — hesitationDetected', () => {
    it('flags a short answer', () => {
        expect(deriveFlowState({...base, lastTurnTranscript: 'I think so'}).hesitationDetected).toBe(true);
    });

    it('flags explicit stuck phrases (even when long enough)', () => {
        const phrases = [
            "I don't know what to say really, this is quite a lot of words",
            "It's hard to explain this in any more detail than that honestly",
            'Sorry, I am not sure how to continue with this particular question',
        ];
        for (const t of phrases) {
            expect(deriveFlowState({...base, lastTurnTranscript: t}).hesitationDetected).toBe(true);
        }
    });

    it('does not flag a substantial confident answer', () => {
        expect(deriveFlowState(base).hesitationDetected).toBe(false);
    });

    it('never flags hesitation on the greeting turn (turnIndex 0)', () => {
        expect(deriveFlowState({...base, turnIndex: 0, lastTurnTranscript: ''}).hesitationDetected).toBe(false);
    });
});

describe('detectFollowUp', () => {
    it('detects an "other approaches" question', () => {
        expect(detectFollowUp('So, what other approaches would you consider here?')).toBe(true);
    });
    it('detects rephrased follow-ups even without a question mark (ASR drops "?")', () => {
        expect(detectFollowUp('What other strategies would you recommend')).toBe(true);
        expect(detectFollowUp('Are there any alternative solutions you would suggest')).toBe(true);
        expect(detectFollowUp('How else could you approach this')).toBe(true);
    });
    it('ignores a statement (alternative lexicon but no interrogative / "?")', () => {
        expect(detectFollowUp('There are other approaches to consider.')).toBe(false);
    });
    it('does not fire on Case-2 probing (no alternative/approach lexicon)', () => {
        expect(detectFollowUp('Can you explain that in more detail?')).toBe(false);
        expect(detectFollowUp('Could you strengthen your argument?')).toBe(false);
    });
});

describe('matchesFollowUp', () => {
    it('matches the verbatim standard follow-up', () => {
        expect(matchesFollowUp(STANDARD_FOLLOW_UP)).toBe(true);
    });
    it('matches an inflected paraphrase via 4-char stems', () => {
        expect(matchesFollowUp('Do you have any other ideas that might work better?')).toBe(true);
    });
    it.each([
        'Why do you think that would work?',
        'Tell me more about that idea?',
        'What about working with an influencer?',
        'Which idea is better, the first or the second?',
        'Which idea do you think is better for the tourists?',
        'Can you tell me more about that idea?',
        'Is working with an influencer better than the other ideas?',
        'Which of these ideas is better than the others?',
        'Is that idea better than the other ones?',
        // Looser paraphrase: deliberately not matched; the two-cue force covers it.
        'Could there be a better idea?',
    ])('rejects an ordinary probe: %s', (probe) => {
        expect(matchesFollowUp(probe)).toBe(false);
    });
    it('rejects the follow-up without its question mark', () => {
        expect(matchesFollowUp(STANDARD_FOLLOW_UP.replace('?', ''))).toBe(false);
    });
    it('rejects a statement that echoes the words (no "?")', () => {
        expect(matchesFollowUp('I think other ideas could work better than these.')).toBe(false);
    });
    it('counts repeated content words once', () => {
        expect(matchesFollowUp('Other other other ideas?')).toBe(false);
    });
});

describe('deriveFlowState — followUpAsked', () => {
    it('flips true when the examiner asks the follow-up', () => {
        expect(
            deriveFlowState({...base, lastExaminerReply: 'Do you have any other ideas that might work better?'}).followUpAsked,
        ).toBe(true);
    });
    it('stays true once asked (sticky)', () => {
        expect(
            deriveFlowState({...base, followUpAlreadyAsked: true, lastExaminerReply: ''}).followUpAsked,
        ).toBe(true);
    });
});

describe('deriveFlowState — currentPhase', () => {
    it('greeting at turn 0', () => {
        expect(deriveFlowState({...base, turnIndex: 0}).currentPhase).toBe('greeting');
    });
    it('hints when hesitant', () => {
        expect(deriveFlowState({...base, lastTurnTranscript: 'I dunno'}).currentPhase).toBe('hints');
    });
    it('answering when below min duration and confident', () => {
        expect(deriveFlowState({...base, studentSpeakingSecondsCumulative: 60}).currentPhase).toBe('answering');
    });
    it('probing when min met, confident, no follow-up yet', () => {
        expect(deriveFlowState({...base, studentSpeakingSecondsCumulative: 130}).currentPhase).toBe('probing');
    });
    it('followup when min met and follow-up asked but not yet answered', () => {
        expect(
            deriveFlowState({...base, studentSpeakingSecondsCumulative: 130, followUpAlreadyAsked: true})
                .currentPhase,
        ).toBe('followup');
    });
    it('closing when min met and follow-up asked and answered', () => {
        expect(
            deriveFlowState({
                ...base,
                studentSpeakingSecondsCumulative: 130,
                followUpAlreadyAsked: true,
                followUpAnsweredAlready: true,
            }).currentPhase,
        ).toBe('closing');
    });
});

describe('deriveFlowState — followUpAnswered', () => {
    it('is false when neither sticky nor this-turn signal is set', () => {
        expect(deriveFlowState(base).followUpAnswered).toBe(false);
    });
    it('flips true when this completed turn answers the follow-up', () => {
        expect(deriveFlowState({...base, answersFollowUp: true}).followUpAnswered).toBe(true);
    });
    it('stays true once answered (sticky)', () => {
        expect(
            deriveFlowState({...base, followUpAnsweredAlready: true, answersFollowUp: false}).followUpAnswered,
        ).toBe(true);
    });
});
