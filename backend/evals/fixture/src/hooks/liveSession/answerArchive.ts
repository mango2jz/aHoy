import type { createClientClient } from "@/utils/supabase/client";
import { extForMime } from "../../utils/audio/recorderMime.ts";

const BUCKET = "practice-audio";
type SupabaseClient = ReturnType<typeof createClientClient>;

export function createAnswerArchive(
  supabase: SupabaseClient,
  sessionId: string,
) {
  function answerPath(userId: string, turnUuid: string, mime: string): string {
    return `${userId}/${sessionId}/answer-${turnUuid}.${extForMime(mime)}`;
  }

  async function upload(path: string, blob: Blob, mime: string): Promise<void> {
    const { error } = await supabase.storage.from(BUCKET).upload(path, blob, {
      contentType: blob.type || mime,
      upsert: true,
    });
    if (error) throw new Error(`Storage upload failed: ${error.message}`);
  }

  return { answerPath, upload };
}

export type AnswerArchive = ReturnType<typeof createAnswerArchive>;
