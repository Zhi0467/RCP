const KEY_STEP = 16;

/** A vertical splitter: dragging or Left/Right/Home/End sets a width within its bounds.
 * `widthAt` turns a pointer's clientX into the width it asks for. */
export function ResizeHandle({
  label,
  controls,
  className,
  width,
  minimum,
  maximum,
  widthAt,
  onResize,
}: {
  label: string;
  controls: string;
  className: string;
  width: number;
  minimum: number;
  maximum: number;
  widthAt: (clientX: number) => number | null;
  onResize: (width: number) => void;
}) {
  const resize = (next: number) => onResize(Math.min(maximum, Math.max(minimum, next)));
  const drag = (clientX: number) => {
    const next = widthAt(clientX);
    if (next !== null) resize(next);
  };
  return (
    <div
      aria-controls={controls}
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemax={maximum}
      aria-valuemin={minimum}
      aria-valuenow={Math.round(width)}
      className={`resize-handle ${className}`}
      onKeyDown={(event) => {
        if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
          event.preventDefault();
          resize(width + (event.key === "ArrowLeft" ? -KEY_STEP : KEY_STEP));
        }
        if (event.key === "Home" || event.key === "End") {
          event.preventDefault();
          resize(event.key === "Home" ? minimum : maximum);
        }
      }}
      onPointerCancel={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        drag(event.clientX);
      }}
      onPointerMove={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) drag(event.clientX);
      }}
      onPointerUp={(event) => {
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
      }}
      role="separator"
      tabIndex={0}
    />
  );
}
