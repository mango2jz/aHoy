/**
 * The 30 prebuilt voices supported by the Gemini Live API
 * (`gemini-3.1-flash-live-preview`). The chosen voice is applied client-side at
 * `live.connect` via `speechConfig.voiceConfig.prebuiltVoiceConfig.voiceName`
 * (see `src/hooks/liveSession/geminiSession.ts`).
 *
 * `tone` is a short human label surfaced in the Settings → Practice picker; it
 * mirrors Google's published voice characteristics.
 *
 * The allowed set is validated at the service boundary (`src/schemas/profile.ts`)
 * via `z.enum(GEMINI_VOICE_NAMES)` rather than a DB CHECK constraint, so the list
 * can evolve with Google's catalogue without a migration.
 */
export interface GeminiVoice {
    /** The exact `prebuiltVoiceConfig.voiceName` value sent to the Live API. */
    name: string;
    /** Short tone descriptor shown in the picker. */
    tone: string;
}

export const GEMINI_VOICES = [
    {name: 'Zephyr', tone: 'Bright'},
    {name: 'Puck', tone: 'Upbeat'},
    {name: 'Charon', tone: 'Informative'},
    {name: 'Kore', tone: 'Firm'},
    {name: 'Fenrir', tone: 'Excitable'},
    {name: 'Leda', tone: 'Youthful'},
    {name: 'Orus', tone: 'Firm'},
    {name: 'Aoede', tone: 'Breezy'},
    {name: 'Callirrhoe', tone: 'Easy-going'},
    {name: 'Autonoe', tone: 'Bright'},
    {name: 'Enceladus', tone: 'Breathy'},
    {name: 'Iapetus', tone: 'Clear'},
    {name: 'Umbriel', tone: 'Easy-going'},
    {name: 'Algieba', tone: 'Smooth'},
    {name: 'Despina', tone: 'Smooth'},
    {name: 'Erinome', tone: 'Clear'},
    {name: 'Algenib', tone: 'Gravelly'},
    {name: 'Rasalgethi', tone: 'Informative'},
    {name: 'Laomedeia', tone: 'Upbeat'},
    {name: 'Achernar', tone: 'Soft'},
    {name: 'Alnilam', tone: 'Firm'},
    {name: 'Schedar', tone: 'Even'},
    {name: 'Gacrux', tone: 'Mature'},
    {name: 'Pulcherrima', tone: 'Forward'},
    {name: 'Achird', tone: 'Friendly'},
    {name: 'Zubenelgenubi', tone: 'Casual'},
    {name: 'Vindemiatrix', tone: 'Gentle'},
    {name: 'Sadachbia', tone: 'Lively'},
    {name: 'Sadaltager', tone: 'Knowledgeable'},
    {name: 'Sulafat', tone: 'Warm'},
] as const satisfies readonly GeminiVoice[];

export const GEMINI_VOICE_NAMES = GEMINI_VOICES.map((v) => v.name) as [string, ...string[]];

export type GeminiVoiceName = (typeof GEMINI_VOICES)[number]['name'];

export const DEFAULT_VOICE: GeminiVoiceName = 'Kore';

/** True when `value` is one of the 30 known prebuilt voice names. */
export function isGeminiVoiceName(value: string): value is GeminiVoiceName {
    return GEMINI_VOICES.some((v) => v.name === value);
}
