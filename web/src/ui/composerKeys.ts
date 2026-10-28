/** Whether Return sends a composer's message. On a touch phone it types a newline,
 * so a message can span lines and only the send button sends. */
export function returnSends(event: { key: string; shiftKey: boolean }): boolean {
  return (
    event.key === "Enter" && !event.shiftKey && !window.matchMedia("(pointer: coarse)").matches
  );
}
