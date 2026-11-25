import {describe, expect, it} from '../../test-support/vitest.ts';
import {cleanExaminerReply} from './cleanExaminerReply.ts';

describe('cleanExaminerReply', () => {
    it('strips a leading FLOW_STATE: object form', () => {
        expect(cleanExaminerReply('FLOW_STATE: {phase=answering}. That was a good answer.')).toBe(
            'That was a good answer.',
        );
    });

    it('strips a leading FLOW_STATE: key=value form', () => {
        expect(cleanExaminerReply('FLOW_STATE: minDurationMet=true, phase=wrapUp. Hello student.')).toBe(
            'Hello student.',
        );
    });

    it('strips a leading pipe control line ending in phase=answering', () => {
        const input =
            '|minDurationMet=true|hesitationDetected=false|followUpAsked=false|phase=answering\nThank you, please continue.';
        expect(cleanExaminerReply(input)).toBe('Thank you, please continue.');
    });

    it('strips a leading pipe control line ending in phase=primary', () => {
        const input = '|minDurationMet=false|hesitationDetected=false|followUpAsked=false|phase=primary\nLet us begin.';
        expect(cleanExaminerReply(input)).toBe('Let us begin.');
    });

    it('strips a pipe control line preceded by whitespace', () => {
        expect(cleanExaminerReply('  |phase=answering\nHello.')).toBe('Hello.');
    });

    it('strips the internal marker and the final-answer steering sentence', () => {
        const input =
            "[INTERNAL CONTEXT — DO NOT READ ALOUD] This is the student's last answer. React briefly, then say one warm goodbye sentence. Do not ask another question. Great talking with you.";
        expect(cleanExaminerReply(input)).toBe('Great talking with you.');
    });

    it('strips the current final-answer and goodbye cue wording', () => {
        expect(
            cleanExaminerReply(
                "This is the student's last answer. React briefly, then say your goodbye. Do not ask another question. Thanks, see you.",
            ),
        ).toBe('Thanks, see you.');
        expect(cleanExaminerReply('The student has finished. Say your goodbye and stop. See you.')).toBe('See you.');
        expect(cleanExaminerReply('The student has finished. Say one warm goodbye sentence and stop. Bye.')).toBe('Bye.');
    });

    it('strips the final-answer and goodbye cues that carry a closing line', () => {
        expect(
            cleanExaminerReply(
                'This is the student\'s last answer. React briefly, then say exactly: "I need to go now. See you." Do not ask another question. Good point.',
            ),
        ).toBe('Good point.');
        expect(cleanExaminerReply('The student has finished. Say exactly: “I’ll go now. See you.” and stop. See you.')).toBe(
            'See you.',
        );
    });

    it.each([
        "Begin the session now. The student's name is Hiếu. Greet the student.",
        'Begin the session now: greet the student.',
        'Begin the session now: greet the student and ask your opening question.',
        'Begin the session now: greet the student briefly and invite them to talk about a topic they choose.',
    ])('strips a leaked start cue: %s', (cue) => {
        expect(cleanExaminerReply(`${cue} Hi Hieu. Are you free now?`)).toBe('Hi Hieu. Are you free now?');
    });

    it('strips a leaked reply nudge', () => {
        expect(
            cleanExaminerReply(
                'The student has finished answering. Reply to what the student just said. That sounds like a good plan.',
            ),
        ).toBe('That sounds like a good plan.');
    });

    it('leaves the scripted opening untouched', () => {
        const opening = "Hi Hieu. Are you free now? I want to talk to you about our restaurant's plan.";
        expect(cleanExaminerReply(opening)).toBe(opening);
    });

    it('leaves plain text untouched', () => {
        expect(cleanExaminerReply('Hello student, great job today.')).toBe('Hello student, great job today.');
    });

    it('does not strip a mid-sentence pipe', () => {
        expect(cleanExaminerReply('Use either a|b in your answer.')).toBe('Use either a|b in your answer.');
    });

    it('returns an empty string for empty input', () => {
        expect(cleanExaminerReply('')).toBe('');
    });
});
