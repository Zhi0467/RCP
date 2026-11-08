import "/src/styles.css";
import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { UpdateNotice, ReleaseCheckRow } from "/src/desktop/UpdateNotice.tsx";
import { useRecheckOnRelease, useUpdateNotice } from "/src/desktop/useUpdateNotice.ts";
import { desktopBuildIdentity } from "/src/core/desktopRuntime.ts";
import { useDesktopShell } from "/src/desktop/useDesktopShell.ts";
window.visibility = new EventTarget();
window.visibility.visibilityState = "visible";
window.identity = { kind: "prebuilt", version: "0.4.2", checkout: null };
window.copied = [];
window.updaterEnabled = false;
window.rechecks = [];
const recheck = () => window.rechecks.push(Date.now());
Object.defineProperty(navigator, "clipboard", {
  value: { writeText: async (value) => window.copied.push(value) },
});
window.readIdentity = desktopBuildIdentity;
function App() {
  const [notice] = useUpdateNotice(true, undefined, window.visibility);
  useRecheckOnRelease(notice, window.offered, recheck);
  return (
    <>
      <UpdateNotice
        notice={notice}
        identity={window.identity}
        desktop={!!window.identity}
        updaterEnabled={window.updaterEnabled}
        update={null}
        error={null}
        activeWork={false}
        expanded={false}
        applying={false}
        onExpand={() => {}}
        onApply={() => {}}
      />
      <ReleaseCheckRow notice={notice} />
    </>
  );
}

function NativeApp() {
  const shell = useDesktopShell(true);
  const [activeWork, setActiveWork] = useState(false);
  window.refreshNativeUpdate = async () => {
    await shell.refreshDesktopUpdate();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  };
  window.setNativeActiveWork = setActiveWork;
  useEffect(() => {
    void shell.refreshDesktopUpdate();
  }, [shell.refreshDesktopUpdate]);
  return (
    <UpdateNotice
      notice={null}
      identity={shell.buildIdentity}
      desktop
      updaterEnabled={shell.updaterEnabled}
      update={shell.desktopUpdate}
      error={shell.updateError}
      activeWork={activeWork}
      expanded={shell.updateExpanded}
      applying={shell.updateApplying}
      onExpand={shell.expandUpdate}
      onApply={() =>
        void shell.applyUpdate(activeWork, async () => ({
          ok: true,
          activeAgentTasks: activeWork ? 1 : 0,
        }))
      }
    />
  );
}

const root = createRoot(document.getElementById("root"));
window.remount = () => root.render(<App key={Math.random()} />);
root.render(new URLSearchParams(window.location.search).has("native") ? <NativeApp /> : <App />);
