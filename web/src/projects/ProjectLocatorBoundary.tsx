import { useState } from "react";
import { api } from "../api";

/** A CLI locator is intent, never permission to register a project. */
export function ProjectLocatorBoundary() {
  const locator = new URLSearchParams(window.location.search).get("project-locator") ?? "";
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const finish = (id?: string) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("project-locator");
    url.hash = id ? `/projects/${encodeURIComponent(id)}` : "";
    window.location.replace(url.toString());
  };
  const confirm = async () => {
    setBusy(true);
    try {
      const project = await api<{ id: string }>("/api/projects", {
        method: "POST",
        body: JSON.stringify({ locator }),
      });
      finish(project.id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy(false);
    }
  };
  return (
    <main className="team-login-boundary">
      <section className="team-login-card">
        <div className="team-login-card-body">
          <h1>Open this project?</h1>
          <p>{locator}</p>
          {error && (
            <p role="alert" className="team-login-error">
              {error}
            </p>
          )}
          <button
            className="button primary"
            disabled={busy || !locator}
            onClick={() => void confirm()}
          >
            Open project
          </button>
          <button className="button secondary" disabled={busy} onClick={() => finish()}>
            Cancel
          </button>
        </div>
      </section>
    </main>
  );
}
