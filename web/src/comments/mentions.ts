import {
  bulletinMentionCandidates,
  bulletinEncodeMentions,
  bulletinDecodeMentions,
} from "../chat/bulletinModel.ts";
import type { BulletinMentionCandidate } from "../chat/bulletinModel.ts";
import type { CommentMember } from "./types.ts";

export function commentMentionCandidates(
  members: readonly CommentMember[],
): BulletinMentionCandidate[] {
  return bulletinMentionCandidates(members, []);
}

export function encodeCommentMentions(
  text: string,
  candidates: readonly BulletinMentionCandidate[],
) {
  return bulletinEncodeMentions(
    text,
    candidates.filter((candidate) => candidate.mention.kind === "member"),
  );
}

export function decodeCommentMentions(
  text: string,
  candidates: readonly BulletinMentionCandidate[],
) {
  return bulletinDecodeMentions(
    text,
    candidates.filter((candidate) => candidate.mention.kind === "member"),
  );
}
