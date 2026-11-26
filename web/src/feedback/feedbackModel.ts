export const FEEDBACK_REPOSITORY = "https://github.com/Zhi0467/RCP";
export const FEEDBACK_MAX_CHARS = 4000;
const TITLE_MAX_CHARS = 72;

export interface FeedbackContext {
  version: string;
  build?: number | null;
  space_kind: "personal" | "team";
}

/** A prefilled new-issue link: the first line titles it, the app version closes it. */
export function feedbackIssueUrl(text: string, context: FeedbackContext | null): string {
  const message = text.trim().slice(0, FEEDBACK_MAX_CHARS);
  const firstLine = message.split("\n", 1)[0].trim();
  const title =
    firstLine.length > TITLE_MAX_CHARS
      ? `${firstLine.slice(0, TITLE_MAX_CHARS - 1).trimEnd()}…`
      : firstLine;
  const about = context
    ? `\n\n---\nRCP ${context.version}${context.build ? ` build ${context.build}` : ""}, ${context.space_kind} space`
    : "";
  const query = new URLSearchParams({ title, body: message + about });
  return `${FEEDBACK_REPOSITORY}/issues/new?${query.toString()}`;
}
