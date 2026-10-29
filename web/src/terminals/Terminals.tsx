import { useEffect, useRef, useState } from "react";
import { TriangleAlert, RefreshCw, SquareTerminal, X } from "lucide-react";
import { api } from "../core/api";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime } from "../core/useResource";
import { useProjectResource } from "../projects/projectResources";
import { terminalsProfile, type TerminalResources } from "./terminalResources";
import { TerminalPane } from "./TerminalPane";
import type { TerminalRepository, TerminalSession } from "../core/types";

export function Terminals({ projectId }: { projectId: string }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/terminals`;
  const { store } = useRefreshRuntime();
  const profile = terminalsProfile(projectId);
  const resource = useProjectResource(profile);
  const resourceId = resourceKeyId(profile.key);
  const repositories = resource.value?.repositories ?? [];
  const sessions = resource.value?.sessions ?? [];
  const setSessions = (update: (current: TerminalSession[]) => TerminalSession[]) => {
    const current = store.view<TerminalResources>(resourceId).value;
    store.write(resourceId, {
      repositories: current?.repositories ?? [],
      sessions: update(current?.sessions ?? []),
    });
  };
  const activeSnapshot = useRef<TerminalSession | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const loading = resource.loading;
  const [error, setError] = useState<string | null>(null);
  const [endedReason, setEndedReason] = useState<string | null>(null);
  const loadError = resource.error ? String(resource.error) : null;
  useEffect(() => {
    setSelected((id) => id ?? resource.value?.sessions[0]?.session_id ?? null);
  }, [resource.value]);

  const open = async (repository: TerminalRepository) => {
    setPending(true);
    setError(null);
    setEndedReason(null);
    try {
      const session = await api<TerminalSession>(base, {
        method: "POST",
        body: JSON.stringify({ repository_id: repository.repository_id }),
      });
      setSessions((current) => [
        ...current.filter((item) => item.session_id !== session.session_id),
        session,
      ]);
      setSelected(session.session_id);
      store.invalidate(resourceId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(false);
    }
  };
  const end = async (session: TerminalSession) => {
    setPending(true);
    setError(null);
    try {
      await api(`${base}/${encodeURIComponent(session.session_id)}`, { method: "DELETE" });
      if (activeSnapshot.current?.session_id === session.session_id) {
        activeSnapshot.current = null;
        setSelected(null);
      }
      setSessions((current) => current.filter((item) => item.session_id !== session.session_id));
      store.invalidate(resourceId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(false);
    }
  };
  const refreshProbes = async () => {
    setPending(true);
    setError(null);
    try {
      await api(`${base}/probe`, { method: "POST" });
      store.invalidate(resourceId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setPending(false);
    }
  };
  const liveActive = sessions.find((session) => session.session_id === selected);
  if (liveActive) activeSnapshot.current = liveActive;
  const active =
    liveActive ?? (activeSnapshot.current?.session_id === selected ? activeSnapshot.current : null);
  const available = repositories.filter(
    (repo) => !sessions.some((session) => session.repository_id === repo.repository_id),
  );

  return (
    <section
      className="view-panel terminals-view"
      aria-label="Terminals"
      aria-busy={loading || undefined}
      style={error || loadError ? { border: "1px solid var(--danger, #b42318)" } : undefined}
    >
      <div className="view-heading">
        <h2>Terminals</h2>
        <button
          className="button compact secondary"
          aria-label="Refresh terminals"
          title={error || loadError || "Refresh terminals"}
          disabled={(loading && !loadError) || pending}
          onClick={() => void refreshProbes()}
        >
          <RefreshCw size={14} className={loading && !loadError ? "spin" : undefined} />
        </button>
      </div>
      {endedReason && <p role="status">{endedReason}</p>}
      <div className={`terminals-layout${sessions.length || active ? " has-sessions" : ""}`}>
        <aside className="terminal-rail" aria-label="Terminal sessions and repositories">
          {sessions.map((session) => (
            <div
              className={`terminal-session-row${active?.session_id === session.session_id ? " selected" : ""}`}
              key={session.session_id}
            >
              <button
                className="terminal-session-select"
                aria-pressed={active?.session_id === session.session_id}
                onClick={() => setSelected(session.session_id)}
              >
                <span className="terminal-repository-name">
                  <SquareTerminal size={16} />
                  {session.repository_id}
                </span>
                <span className="terminal-session-state">
                  {session.state === "live" ? "Live" : "Idle"}
                </span>
                {session.running_work.length > 0 && (
                  <span
                    className="terminal-work-mark"
                    title={session.running_work.map((turn) => turn.title).join("; ")}
                  >
                    Work running
                  </span>
                )}
              </button>
              <button
                className="icon-button"
                aria-label={`End ${session.repository_id} terminal`}
                disabled={pending}
                onClick={() => void end(session)}
              >
                <X size={14} />
              </button>
            </div>
          ))}
          {available.map((repo) => (
            <button
              className="button secondary terminal-repository"
              key={repo.repository_id}
              disabled={!repo.eligible || pending}
              onClick={() => void open(repo)}
            >
              <span className="terminal-repository-name">
                <SquareTerminal size={16} />
                {repo.repository_id}
              </span>
              <code>{repo.path}</code>
              {!repo.eligible && (
                <span className="terminal-unavailable">
                  {repo.unavailable_reason || repo.reason}
                </span>
              )}
            </button>
          ))}
        </aside>
        {active && (
          <div className="terminal-active">
            <div className="terminal-active-heading">
              <strong>{active.repository_id}</strong>
              <code>{active.path}</code>
            </div>
            {active.containment === "cooperative" && (
              <div className="terminal-protection-warning" role="alert">
                <TriangleAlert size={20} aria-hidden="true" />
                <strong>{active.protection_notice}</strong>
              </div>
            )}
            {active.running_work.length > 0 && (
              <div className="terminal-work-strip" role="status">
                Work running: {active.running_work.map((turn) => turn.title).join(" · ")}
              </div>
            )}
            <TerminalPane
              key={active.session_id}
              socketPath={`${base}/${encodeURIComponent(active.session_id)}/ws`}
              sessionMissing={!liveActive}
              onEnded={(reason) => {
                activeSnapshot.current = null;
                setSelected(null);
                setEndedReason(reason || null);
                setSessions((current) =>
                  current.filter((session) => session.session_id !== active.session_id),
                );
                store.invalidate(resourceId);
              }}
            />
          </div>
        )}
      </div>
    </section>
  );
}
