const FLIGHT_MS = 360;

/**
 * A parked card's copy travels to the pane button. With reduced motion the
 * copy only fades where the card stood.
 */
export function flyToPane(
  card: Element | null,
  button: Element | null,
  reducedMotion: boolean,
): void {
  if (!card || !button) return;
  const from = card.getBoundingClientRect();
  const to = button.getBoundingClientRect();
  const ghost = document.createElement("div");
  ghost.className = "task-flight";
  ghost.setAttribute("aria-hidden", "true");
  ghost.textContent = card.getAttribute("aria-label");
  Object.assign(ghost.style, {
    left: `${from.left}px`,
    top: `${from.top}px`,
    width: `${from.width}px`,
  });
  document.body.append(ghost);
  const dx = to.left + to.width / 2 - (from.left + from.width / 2);
  const dy = to.top + to.height / 2 - (from.top + ghost.offsetHeight / 2);
  const flight = ghost.animate(
    reducedMotion
      ? [{ opacity: 1 }, { opacity: 0 }]
      : [
          { opacity: 1, transform: "translate(0, 0) scale(1)" },
          { opacity: 0.2, transform: `translate(${dx}px, ${dy}px) scale(0.08)` },
        ],
    { duration: FLIGHT_MS, easing: "cubic-bezier(0.4, 0, 0.2, 1)" },
  );
  const land = () => {
    ghost.remove();
    if (!reducedMotion)
      button.animate(
        [{ transform: "scale(1)" }, { transform: "scale(1.18)" }, { transform: "scale(1)" }],
        { duration: 220, easing: "ease-out" },
      );
  };
  flight.finished.then(land, land);
}
