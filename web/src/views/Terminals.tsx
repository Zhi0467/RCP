import { useEffect, useRef, useState } from "react";
import { AlertTriangle, RefreshCw, TerminalSquare, X } from "lucide-react";
import { api } from "../api";
import { TerminalPane } from "../components/TerminalPane";
import type { TerminalRepository, TerminalSession } from "../types";

export function Terminals({ projectId }: { projectId: string }) {
  const base = `/api/projects/${encodeURIComponent(projectId)}/terminals`;
  const [repositories, setRepositories] = useState<TerminalRepository[]>([]);
  const [sessions, setSessions] = useState<TerminalSession[]>([]);
  const activeSnapshot = useRef<TerminalSession | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [endedReason, setEndedReason] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const [repos, current] = await Promise.all([
          api<TerminalRepository[]>(`${base}/repositories`, { signal: controller.signal }),
          api<TerminalSession[]>(base, { signal: controller.signal }),
        ]);
        if (controller.signal.aborted) return;
        setRepositories(repos);
        setSessions(current);
        // Keep the active socket mounted until it can deliver its final reason.
        // The open-session rail still removes the missing session immediately.
        setSelected((id) => id ?? current[0]?.session_id ?? null);
        setLoadError(null);
      } catch (cause) {
        if (!controller.signal.aborted)
          setLoadError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          timer = setTimeout(() => void load(), 5000);
        }
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [base, refresh]);

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
      setRefresh((value) => value + 1);
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
      setRefresh((value) => value + 1);
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
      setRefresh((value) => value + 1);
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
    <section className="view-panel terminals-view" aria-label="Terminals">
      <div className="view-heading">
        <h2>Terminals</h2>
        <button
          className="button compact secondary"
          aria-label="Refresh terminals"
          disabled={loading || pending}
          onClick={() => void refreshProbes()}
        >
          <RefreshCw size={14} />
        </button>
      </div>
      {(error || loadError) && <p role="alert">{error || loadError}</p>}
      {endedReason && <p role="status">{endedReason}</p>}
      {loading && <p role="status">Loading terminals…</p>}
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
                  <TerminalSquare size={16} />
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
                <TerminalSquare size={16} />
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
                <AlertTriangle size={20} aria-hidden="true" />
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
                setEndedReason(reason);
                setSessions((current) =>
                  current.filter((session) => session.session_id !== active.session_id),
                );
                setRefresh((value) => value + 1);
              }}
            />
          </div>
        )}
      </div>
    </section>
  );
}
