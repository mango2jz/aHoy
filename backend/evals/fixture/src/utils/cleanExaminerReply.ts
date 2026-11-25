/**
 * Leading `|key=value|key=value|…` control line the live examiner sometimes emits
 * verbatim (e.g. `|minDurationMet=true|hesitationDetected=false|phase=answering`).
 * There is no closing pipe, so the line is matched up to the first newline.
 */
const CONTROL_LINE_PATTERN = /^\|(?:\w+=[^|\n]*\|?)+\n?/;

/** `FLOW_STATE: {…}` / `FLOW_STATE: key=value, key=value.` leaked into the reply text. */
const FLOW_STATE_PATTERN = /\bFLOW_STATE:\s*(?:\{[^}]*\}\.?|(?:\w+=\w+(?:[&,]\s*)?)+\.?)\s*/g;

/**
 * The `[INTERNAL CONTEXT — DO NOT READ ALOUD]` marker and the client's
 * final-answer and goodbye cues, in case the model reads them out anyway.
 * Both the current and the previous cue wording are matched, since a session
 * in flight during a deploy can echo either. The em dash is matched loosely
 * (`—`/`-`) because the transcriber varies it.
 */
const INTERNAL_MARKER_PATTERN = /\[INTERNAL CONTEXT\s*[—-]\s*DO NOT READ ALOUD\]\s*/gi;
const FINAL_ANSWER_STEER_PATTERN =
    /This is the student['’]s last answer\.\s*React briefly, then say (?:(?:your goodbye|one warm goodbye sentence)\.|exactly:\s*["“][^"“”]*["”])\s*Do not ask another question\.\s*|The student has finished\.\s*Say (?:your goodbye|one warm goodbye sentence|exactly:\s*["“][^"“”]*["”]) and stop\.\s*/gi;
/** Every start-cue form: with any name, without one, the old opener and the fallback. */
const START_CUE_PATTERN =
    /Begin the session now(?:\.\s*The student['’]s name is [\p{L}\p{M}'’-]+\.\s*Greet the student\.|:\s*greet the student(?: and ask your opening question| briefly and invite them to talk about a topic they choose)?\.)\s*/giu;
/** The client's reply nudge, sent when an answer drew no output. */
const REPLY_NUDGE_PATTERN = /The student has finished answering\.\s*Reply to what the student just said\.\s*/gi;

/** Remove internal flow context if it is accidentally transcribed by the model. */
export function cleanExaminerReply(text: string): string {
    return text
        .trimStart()
        .replace(CONTROL_LINE_PATTERN, '')
        .replace(FLOW_STATE_PATTERN, '')
        .replace(INTERNAL_MARKER_PATTERN, '')
        .replace(FINAL_ANSWER_STEER_PATTERN, '')
        .replace(START_CUE_PATTERN, '')
        .replace(REPLY_NUDGE_PATTERN, '')
        .trim();
}
