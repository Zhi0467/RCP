import type { LoopCheckout } from "../core/types";

/** Run cards keep the paths in the tooltip; overlap lists show them inline. */
export function ExperimentCheckout({
  checkout,
  detailed = false,
}: {
  checkout?: Pick<LoopCheckout, "kind" | "repository_paths"> | null;
  detailed?: boolean;
}) {
  const location = checkout?.repository_paths.join(" · ") || undefined;
  return (
    <span
      className="experiment-checkout"
      data-checkout-kind={checkout?.kind ?? "unknown"}
      title={location}
    >
      Checkout: {checkout?.kind ?? "unknown"}
      {detailed && location ? ` · ${location}` : null}
    </span>
  );
}
