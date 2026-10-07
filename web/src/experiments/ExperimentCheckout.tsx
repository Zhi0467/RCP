import type { LoopCheckout } from "../core/types";

export function ExperimentCheckout({
  checkout,
}: {
  checkout?: Pick<LoopCheckout, "kind" | "execution_host" | "repository_paths"> | null;
}) {
  return (
    <span
      className="experiment-checkout"
      data-checkout-kind={checkout?.kind ?? "unknown"}
      title={
        checkout
          ? [
              checkout.execution_host === ""
                ? "local"
                : (checkout.execution_host ?? "Unknown host"),
              ...checkout.repository_paths,
            ].join(" · ")
          : undefined
      }
    >
      Checkout: {checkout?.kind ?? "unknown"}
    </span>
  );
}
