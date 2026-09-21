import { useState } from "react";
import { createRoot } from "react-dom/client";
import { TerminalTab } from "../../src/components/TerminalTab";
import { Terminals } from "../../src/views/Terminals";
import { useTheme } from "../../src/hooks/useTheme";
import "../../src/styles.css";

function Fixture() {
  const [visible, setVisible] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const { setTheme, setMode } = useTheme();
  return (
    <main style={{ padding: 24 }}>
      <nav>
        <button onClick={() => setVisible(false)}>Research</button>
        <TerminalTab
          projectId="alpha"
          refreshKey={String(refresh)}
          active={visible}
          onClick={() => setVisible(true)}
          onError={setError}
        />
        <button onClick={() => setRefresh((value) => value + 1)}>Refresh project</button>
        <button onClick={() => setTheme("aqua")}>Aqua</button>
        <button onClick={() => setTheme("classic")}>Classic</button>
        <button onClick={() => setMode("system")}>System</button>
        <button onClick={() => setMode("light")}>Light</button>
        <button onClick={() => setMode("dark")}>Dark</button>
      </nav>
      {error && <p role="alert">{error}</p>}
      {visible ? <Terminals projectId="alpha" /> : <h2>Research</h2>}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
