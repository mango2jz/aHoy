export const BACKGROUND_MODEL = 'gemini-3.5-flash-lite';
export const ANALYSIS_MODEL = 'gemini-3.8-flash';
export const COMPANION_MODEL = 'gemini-3.8-flash';

export const EMBEDDING_MODEL = 'gemini-embedding-001';
export const EMBEDDING_DIMENSIONS = 768;

// Prebuilt examiner voice — spike-confirmed 'Kore' (natural, clear). The model also supports 200+
// inline audio tags + phonetic guides for later per-utterance control if needed.
export const TTS_VOICE = 'Kore';
// Single-word pronunciation clips (feedback pronunciation tab), cached in the `word-audio` bucket.
export const TTS_MODEL = 'gemini-3.8-flash-lite-tts';

// B6 Live API — ephemeral-token WebSocket conversion.
// LIVE_MODEL: multimodal model used via the Gemini Live API WebSocket
//   (BidiGenerateContent). Handles bidirectional audio streaming with no
//   separate TTS calls — audio is generated as part of the streaming response.
export const LIVE_MODEL = 'gemini-3.8-live';
