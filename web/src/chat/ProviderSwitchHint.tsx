/** Above a composer whose next turn changes provider: that turn starts a new provider session. */
export function ProviderSwitchHint({ provider }: { provider: string }) {
  return (
    <div className="chat-composer-hint" role="status">
      The next turn starts a fresh {provider} session.
    </div>
  );
}
