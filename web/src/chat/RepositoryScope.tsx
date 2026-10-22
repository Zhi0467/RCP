import { Check, ChevronDown } from "lucide-react";
import { useLayoutEffect, useRef, useState } from "react";
import type { Repository } from "../core/types";
import { useLayerDismiss } from "../core/useLayerDismiss";

const VIEWPORT_GUTTER = 16;

interface Props {
  repositories: Repository[];
  projectScope: string[];
  stateRepository: string;
  selected: string[];
  onChange: (aliases: string[]) => void;
}

export function RepositoryScope({
  repositories,
  projectScope,
  stateRepository,
  selected,
  onChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const popover = useRef<HTMLDivElement>(null);
  useLayerDismiss(open, () => setOpen(false), [root], trigger);
  // Anchored to its trigger's right edge, the list shifts sideways only as far
  // as it takes to stay inside the viewport's gutter.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const node = popover.current;
      if (!node) return;
      node.style.translate = "";
      const { left, right } = node.getBoundingClientRect();
      const shift =
        left < VIEWPORT_GUTTER
          ? VIEWPORT_GUTTER - left
          : Math.min(0, window.innerWidth - VIEWPORT_GUTTER - right);
      node.style.translate = shift ? `${shift}px 0` : "";
    };
    place();
    window.addEventListener("resize", place);
    return () => window.removeEventListener("resize", place);
  }, [open]);

  const toggle = (alias: string) => {
    const next = selected.includes(alias)
      ? selected.filter((item) => item !== alias)
      : [...selected, alias];
    if (next.length) onChange(next);
  };
  return (
    <div className="scope-picker" ref={root}>
      <button
        ref={trigger}
        className="scope-trigger"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <span>
          <span className="eyebrow">Run reads</span>
          <strong>
            {selected.length === projectScope.length
              ? "All repositories"
              : `${selected.length} of ${projectScope.length} repositories`}
          </strong>
        </span>
        <ChevronDown size={16} />
      </button>
      {open && (
        <div className="scope-popover" ref={popover}>
          {repositories
            .filter((repo) => projectScope.includes(repo.alias))
            .map((repo) => (
              <button key={repo.alias} className="scope-option" onClick={() => toggle(repo.alias)}>
                <span className={`checkbox ${selected.includes(repo.alias) ? "checked" : ""}`}>
                  {selected.includes(repo.alias) && <Check size={12} />}
                </span>
                <span>
                  <strong>{repo.alias}</strong>
                  <span className="scope-option-meta mono">
                    {repo.machine} · {repo.path}
                  </span>
                </span>
                {repo.alias === stateRepository && <span className="tiny-tag">canonical</span>}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}
