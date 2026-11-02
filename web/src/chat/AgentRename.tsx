import { useRef } from "react";
import { AGENT_NAME_MAX_CHARS } from "./chatWorkspace";

/** The inline name field for an agent; a blank entry keeps the stored name. */
export function AgentRename({
  title,
  onDone,
}: {
  title: string;
  onDone: (title: string | null) => void;
}) {
  // Enter, Escape and the blur that follows either end the edit once.
  const done = useRef(false);
  const finish = (value: string | null) => {
    if (done.current) return;
    done.current = true;
    onDone(value);
  };
  return (
    <form
      className="agent-row-rename"
      onSubmit={(event) => {
        event.preventDefault();
        const value = new FormData(event.currentTarget).get("title");
        const title = typeof value === "string" ? value.trim() : "";
        // The agent has one stored name; a blank entry keeps it.
        finish(title || null);
      }}
    >
      <input
        name="title"
        aria-label="Agent name"
        autoFocus
        maxLength={AGENT_NAME_MAX_CHARS}
        defaultValue={title}
        onFocus={(event) => event.currentTarget.select()}
        onBlur={(event) => event.currentTarget.form?.requestSubmit()}
        onKeyDown={(event) => {
          if (event.key === "Escape") finish(null);
        }}
      />
    </form>
  );
}
