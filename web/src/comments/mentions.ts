import {
  bulletinMentionCandidates,
  bulletinEncodeMentions,
  bulletinDecodeMentions,
} from "../chat/bulletinModel.ts";
import type { BulletinMentionCandidate } from "../chat/bulletinModel.ts";
import type { CommentMember } from "./types.ts";

export function commentMentionCandidates(
  members: readonly CommentMember[],
  canSend: boolean,
  agentLabel = "Agent",
): BulletinMentionCandidate[] {
  const candidates = bulletinMentionCandidates(members, []);
  return canSend
    ? [{ mention: { kind: "agent", id: "target" }, label: agentLabel, token: "" }, ...candidates]
    : candidates;
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
