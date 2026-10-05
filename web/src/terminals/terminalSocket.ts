/** Opens a same-origin terminal session WebSocket that delivers PTY output as bytes. */
export function openTerminalSocket(socketPath: string): WebSocket {
  const url = new URL(socketPath, window.location.href);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const socket = new WebSocket(url);
  socket.binaryType = "arraybuffer";
  return socket;
}
