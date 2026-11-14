export interface CommentRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface CommentElement {
  path: string;
  label: string;
  text: string;
  region?: CommentRect | null;
}

interface AnchorFields {
  comment: string;
  where?: string | null;
  drawn_version?: string | null;
}

export type CommentAnchor = AnchorFields &
  (
    | { kind: "whole" }
    | { kind: "text"; text: string; surrounding_text: string }
    | {
        kind: "box";
        rect: CommentRect;
        viewport: { width: number; height: number };
        elements: CommentElement[] | null;
        labels?: string;
      }
    | { kind: "element"; path: string; label: string; text: string }
    | { kind: "pdf"; page: number; rect: CommentRect | null; text: string }
  );

export interface CommentTarget {
  kind: "artifact" | "paper" | "reply";
  id: string;
}

export interface CommentMember {
  user_id: string;
  display_name: string;
}

export interface CommentPost {
  post_id: string;
  thread_id: string;
  author: CommentMember;
  text: string;
  created_at: string;
  edited_at?: string | null;
  mentions?: { id: string; display_name: string }[];
}

export interface CommentThread {
  thread_id: string;
  project_id: string;
  target_kind: CommentTarget["kind"];
  target_id: string;
  graph_target: string;
  drawn_version: string | null;
  anchor: CommentAnchor;
  state: "open" | "resolved";
  created_by: string;
  created_at: string;
  closed_by: string | null;
  closed_at: string | null;
  sent_operation_id: string | null;
  send_key?: string | null;
  can_send: boolean;
  posts: CommentPost[];
}

export interface CommentThreadPage {
  threads: CommentThread[];
}

export interface CommentMention {
  thread_id: string;
  post_id: string;
  user_id: string;
  created_at: string;
  thread: Omit<CommentThread, "can_send">;
  target_kind: CommentTarget["kind"];
  target_id: string;
  graph_target: string;
}
