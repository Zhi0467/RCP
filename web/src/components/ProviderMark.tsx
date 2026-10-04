import type { CSSProperties } from "react";

// Monochrome marks from Simple Icons (CC0), served from web/public/providers.
const PROVIDER_LOGOS: Record<string, string> = {
  claude: "/providers/claude.svg",
  codex: "/providers/codex.svg",
  opencode: "/providers/opencode.svg",
};

export function hasProviderLogo(provider: string): boolean {
  return provider in PROVIDER_LOGOS;
}

/** A provider's logo when RCP ships one, otherwise its label as text. */
export function ProviderMark({ provider, label }: { provider: string; label: string }) {
  const logo = PROVIDER_LOGOS[provider];
  if (!logo) return <>{label}</>;
  return (
    <span
      className="provider-mark"
      data-provider={provider}
      role="img"
      aria-label={label}
      title={label}
      style={{ "--provider-logo": `url("${logo}")` } as CSSProperties}
    />
  );
}
