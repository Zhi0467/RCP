import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { SignInsFold, useProviderSignIns } from "../../src/projects/ProviderLogins";
import "../../src/styles.css";

declare global {
  interface Window {
    loginChanges: number;
  }
}

window.loginChanges = 0;

// One machine card holding the Sign-ins fold; the test routes the login API.
const params = new URLSearchParams(window.location.search);
const viewer = params.get("viewer") ?? "u-me";
if (params.get("theme")) document.documentElement.dataset.theme = params.get("theme")!;

function Harness() {
  const data = useProviderSignIns(viewer);
  // Stands in for the app's once-a-second scheduler re-render.
  const rerenderMs = Number(params.get("rerender") ?? 0);
  const [, setTick] = useState(0);
  useEffect(() => {
    if (!rerenderMs) return;
    const timer = setInterval(() => setTick((tick) => tick + 1), rerenderMs);
    return () => clearInterval(timer);
  }, [rerenderMs]);
  return (
    <section className="settings-page" data-settings-level="space">
      <section className="settings-section provider-path-settings space-machine-settings">
        <div className="provider-machine-list">
          <article className="provider-machine machine-card">
            <header>
              <strong>gpu-1</strong>
            </header>
            <SignInsFold
              machineName="gpu-1"
              host={params.get("host") ?? "gpu-1.example"}
              data={data}
              viewerUserId={viewer}
              writesDisabled={false}
              focusProvider={params.get("focus")}
              onLoginChanged={() => {
                window.loginChanges += 1;
              }}
            />
          </article>
        </div>
      </section>
    </section>
  );
}

createRoot(document.getElementById("root")!).render(<Harness />);
