import type { LoopCheckout } from "../core/types";

/** Run cards keep host and paths in the tooltip; overlap lists show them inline. */
export function ExperimentCheckout({
  checkout,
  detailed = false,
}: {
  checkout?: Pick<LoopCheckout, "kind" | "execution_host" | "repository_paths"> | null;
  detailed?: boolean;
}) {
  const location = checkout
    ? [
        checkout.execution_host === "" ? "local" : (checkout.execution_host ?? "Unknown host"),
        ...checkout.repository_paths,
      ].join(" · ")
    : undefined;
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
