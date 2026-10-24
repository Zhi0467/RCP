import type { ComponentPropsWithoutRef, ReactNode } from "react";

type Props = Omit<ComponentPropsWithoutRef<"details">, "title"> & {
  title: ReactNode;
  /** A short state shown while folded: a count, a badge, or a status word. */
  status?: ReactNode;
};

/** A section that starts folded behind a +/− summary holding its title and state. */
export function Fold({ title, status, className, children, ...rest }: Props) {
  return (
    <details className={className ? `fold ${className}` : "fold"} {...rest}>
      <summary>
        <strong>{title}</strong>
        {status !== undefined && status !== null && <span className="fold-status">{status}</span>}
      </summary>
      {children}
    </details>
  );
}
