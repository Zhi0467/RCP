import "/src/styles.css";
import React from "react";
import { createRoot } from "react-dom/client";
import { UpdateNotice, ReleaseCheckRow } from "/src/desktop/UpdateNotice.tsx";
import { useRecheckOnRelease, useUpdateNotice } from "/src/desktop/useUpdateNotice.ts";
import { desktopBuildIdentity } from "/src/core/desktopRuntime.ts";
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
        onDismiss={() => {}}
      />
      <ReleaseCheckRow notice={notice} />
    </>
  );
}
const root = createRoot(document.getElementById("root"));
window.remount = () => root.render(<App key={Math.random()} />);
root.render(<App />);
