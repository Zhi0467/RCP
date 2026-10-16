import { FoldVertical } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AgentCompaction, AgentTask } from "../core/types";
import {
  agentStatusView,
  contextFraction,
  formatTokens,
  type AgentStatusView,
} from "./agentStatusModel";

/** The chat header's running-subagent chip, compaction chip and context ring,
 *  and the one panel any of them opens. Renders nothing the provider did not report. */
export function AgentStatusStrip({ tasks }: { tasks: AgentTask[] }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [open]);

  const view = agentStatusView(tasks);
  if (!view) return null;
  const fraction = contextFraction(view);
  const showRing = view.contextTokens != null;
  if (!showRing && !view.runningSubagents && !view.compactions.length) return null;
  const toggle = () => setOpen((value) => !value);
  const percent = fraction == null ? null : Math.round(fraction * 100);

  return (
    <div className="agent-status" ref={root}>
      {view.runningSubagents > 0 && (
        <button
          type="button"
          className="agent-status-chip subagents"
          aria-label={`${view.runningSubagents} running subagents`}
          aria-expanded={open}
          onClick={toggle}
        >
          <span className="agent-status-dot running" />
          {view.runningSubagents}
        </button>
      )}
      {view.compactions.length > 0 && (
        <button
          type="button"
          className="agent-status-chip compactions"
          aria-label={`${view.compactions.length} compactions`}
          aria-expanded={open}
          onClick={toggle}
        >
          <FoldVertical size={12} />
          {view.compactions.length}
        </button>
      )}
      {showRing && (
        <button
          type="button"
          className="agent-status-chip context"
          data-compacting={view.compacting || undefined}
          aria-label="Context"
          aria-expanded={open}
          onClick={toggle}
        >
          <ContextRing fraction={fraction ?? 0} />
          <span className="agent-status-context-full">
            {formatTokens(view.contextTokens!)}
            {view.contextWindow ? ` / ${formatTokens(view.contextWindow)}` : ""}
          </span>
          <span className="agent-status-context-short">
            {percent == null ? formatTokens(view.contextTokens!) : `${percent}%`}
          </span>
        </button>
      )}
      {open && (
        <>
          <div className="agent-status-scrim" onClick={() => setOpen(false)} />
          <AgentStatusPanel view={view} fraction={fraction} />
        </>
      )}
    </div>
  );
}

function ContextRing({ fraction }: { fraction: number }) {
  return (
    <svg className="agent-status-ring" viewBox="0 0 18 18" aria-hidden="true">
      <circle cx="9" cy="9" r="7" pathLength={100} className="track" />
      <circle
        cx="9"
        cy="9"
        r="7"
        pathLength={100}
        className="fill"
        strokeDasharray={`${fraction * 100} 100`}
        transform="rotate(-90 9 9)"
      />
    </svg>
  );
}

function AgentStatusPanel({ view, fraction }: { view: AgentStatusView; fraction: number | null }) {
  const runtime = view.task.runtime_label;
  return (
    <div className="agent-status-panel" role="dialog" aria-label="Agent status">
      {view.contextTokens != null && (
        <section>
          <h3>Context</h3>
          <div className="agent-status-big">
            {formatTokens(view.contextTokens)}
            {view.contextWindow ? <small> / {formatTokens(view.contextWindow)}</small> : null}
          </div>
          {fraction != null && (
            <div className="agent-status-bar">
              <i style={{ width: `${fraction * 100}%` }} />
              {view.autocompactTokens != null && view.contextWindow ? (
                <u
                  style={{
                    left: `${Math.min(100, (view.autocompactTokens / view.contextWindow) * 100)}%`,
                  }}
                />
              ) : null}
            </div>
          )}
          {view.autocompactTokens != null && (
            <dl className="agent-status-facts">
              <dt>Auto-compact at</dt>
              <dd>{formatTokens(view.autocompactTokens)}</dd>
            </dl>
          )}
        </section>
      )}
      {view.subagents.length > 0 && (
        <section>
          <h3>Subagents</h3>
          <ul className="agent-status-subagents">
            {view.subagents.map((subagent) => (
              <li key={subagent.id}>
                <span className={`agent-status-dot ${subagent.state}`} />
                <strong>{subagent.kind}</strong>
                {subagent.tokens != null && <span>{formatTokens(subagent.tokens)} tok</span>}
                {subagent.label && <small>{subagent.label}</small>}
              </li>
            ))}
          </ul>
        </section>
      )}
      {view.compactions.length > 0 && (
        <section>
          <h3>Compactions</h3>
          <dl className="agent-status-compactions">
            {view.compactions.map((compaction, index) => (
              <div key={`${compaction.operationId}:${index}`}>
                <dt>Turn {compaction.turn}</dt>
                <dd>{compaction.trigger}</dd>
                <dd>{compactionSpan(compaction)}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {runtime && (
        <section>
          <h3>This turn</h3>
          <dl className="agent-status-facts">
            <dt>Runtime</dt>
            <dd>{runtime}</dd>
          </dl>
        </section>
      )}
    </div>
  );
}

function compactionSpan(compaction: AgentCompaction): string {
  const before = compaction.before_tokens == null ? "" : formatTokens(compaction.before_tokens);
  const after = compaction.after_tokens == null ? "" : formatTokens(compaction.after_tokens);
  return before && after ? `${before} → ${after}` : before || after;
}

/** A dashed rule in the transcript where the turn's context was compacted. */
export function CompactionDivider({ compactions }: { compactions: AgentCompaction[] }) {
  return (
    <>
      {compactions.map((compaction, index) => (
        <div
          className="chat-compaction-divider"
          role="separator"
          aria-label="Compacted"
          key={index}
        >
          <span>
            <FoldVertical size={12} />
            <b>{compactionSpan(compaction)}</b>
          </span>
        </div>
      ))}
    </>
  );
}
