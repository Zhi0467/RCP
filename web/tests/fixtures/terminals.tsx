import { useState } from "react";
import { createRoot } from "react-dom/client";
import { Terminals } from "../../src/views/Terminals";
import { useTheme } from "../../src/hooks/useTheme";
import "../../src/styles.css";

function Fixture() {
  const [visible, setVisible] = useState(true);
  const { setTheme, setMode } = useTheme();
  return (
    <main style={{ padding: 24 }}>
      <nav>
        <button onClick={() => setVisible(false)}>Research</button>
        <button onClick={() => setVisible(true)}>Terminals</button>
        <button onClick={() => setTheme("aqua")}>Aqua</button>
        <button onClick={() => setTheme("classic")}>Classic</button>
        <button onClick={() => setMode("system")}>System</button>
        <button onClick={() => setMode("light")}>Light</button>
        <button onClick={() => setMode("dark")}>Dark</button>
      </nav>
      {visible ? <Terminals projectId="alpha" /> : <h2>Research</h2>}
    </main>
  );
}
createRoot(document.getElementById("root")!).render(<Fixture />);
