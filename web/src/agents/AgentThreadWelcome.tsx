import type { ThreadWelcome } from "./threadWelcome";

/** An empty thread's welcome; a starter fills the composer and never sends. */
export function AgentThreadWelcome({
  welcome,
  onPrompt,
}: {
  welcome: ThreadWelcome;
  onPrompt: (text: string) => void;
}) {
  return (
    <div className="thread-welcome">
      {welcome.lines.length > 0 && (
        <ul>
          {welcome.lines.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      )}
      <div className="thread-welcome-prompts">
        {welcome.prompts.map((prompt) => (
          <button
            key={prompt}
            type="button"
            className="button compact secondary"
            onClick={() => onPrompt(prompt)}
          >
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
