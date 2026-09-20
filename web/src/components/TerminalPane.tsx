import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";

export function TerminalPane({ socketPath }: { socketPath: string }) {
  const container = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState("Connecting…");
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    if (!container.current) return;
    const element = container.current;
    // Read the tokens fresh each time: the theme is seeded at construction and
    // reapplied on a theme or mode change.
    const readTheme = () => {
      const styles = getComputedStyle(element);
      const color = (token: string) => styles.getPropertyValue(token).trim();
      return {
        background: color("--sheet"),
        foreground: color("--walnut"),
        cursor: color("--walnut"),
        cursorAccent: color("--sheet"),
        selectionBackground: color("--rose-paper"),
      };
    };
    const terminal = new Terminal({
      fontFamily: getComputedStyle(element).getPropertyValue("--mono"),
      fontSize: 14,
      cursorBlink: true,
      scrollback: 2000,
      allowProposedApi: false,
      // Seeded here because a theme assigned in the same tick as open() is
      // overwritten by the renderer's own initialization.
      theme: readTheme(),
    });
    const fit = new FitAddon();
    terminal.loadAddon(fit);
    terminal.open(element);
    const url = new URL(socketPath, window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(url);
    socket.binaryType = "arraybuffer";
    setStatus("Connecting…");
    let ended = false;
    const resize = () => {
      if (!element.clientWidth || !element.clientHeight) return;
      fit.fit();
      if (socket.readyState === WebSocket.OPEN)
        socket.send(JSON.stringify({ type: "resize", cols: terminal.cols, rows: terminal.rows }));
    };
    const updateTheme = () => {
      terminal.options.theme = readTheme();
    };
    const themeObserver = new MutationObserver(updateTheme);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-color-mode"],
    });
    const observer = new ResizeObserver(resize);
    observer.observe(element);
    const input = terminal.onData((data) => {
      if (socket.readyState !== WebSocket.OPEN) return;
      // Keep pasted Unicode intact, within the transport's 16 KiB input bound.
      for (const chunk of data.match(/[\s\S]{1,4096}/gu) ?? []) {
        socket.send(JSON.stringify({ type: "input", data: chunk }));
      }
    });
    socket.onopen = () => {
      setStatus("");
      resize();
      terminal.focus();
    };
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) {
        terminal.write(new Uint8Array(event.data));
      } else {
        const message = JSON.parse(event.data) as { type: string; reason?: string };
        if (message.type === "ended") {
          ended = true;
          setStatus(message.reason || "Session ended");
        }
      }
    };
    socket.onerror = () => setStatus("Terminal connection failed");
    socket.onclose = (event) => {
      if (!ended) setStatus(event.reason || "Terminal disconnected");
    };
    return () => {
      socket.onopen = socket.onmessage = socket.onerror = socket.onclose = null;
      socket.close();
      observer.disconnect();
      themeObserver.disconnect();
      input.dispose();
      terminal.dispose();
    };
  }, [socketPath, retry]);

  return (
    <div className="terminal-pane">
      {status && (
        <div className="terminal-connection" role="status">
          {status}
          {status !== "Connecting…" && (
            <button
              className="button compact secondary"
              onClick={() => setRetry((value) => value + 1)}
            >
              Reconnect
            </button>
          )}
        </div>
      )}
      <div ref={container} className="terminal-emulator" aria-label="Repository terminal" />
    </div>
  );
}
