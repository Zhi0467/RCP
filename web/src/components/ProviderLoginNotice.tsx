import { useState } from "react";
import { verifyProviderLogin } from "../api";
import { signedOutNote } from "../providerLogins";
import type { ProviderLoginState } from "../types";
import { formatServerTimestamp } from "./ServerSettings";

interface Props {
  states: ProviderLoginState[];
  onVerified?: () => void;
}

export function ProviderLoginNotice({ states, onVerified }: Props) {
  return (
    <>
      {states
        .filter((state) => state.state === "signed_out")
        .map((state) => (
          <AccountNotice
            key={`${state.provider}:${state.host}:${state.generation}:${state.changed_at}`}
            state={state}
            onVerified={onVerified}
          />
        ))}
    </>
  );
}

function AccountNotice({
  state,
  onVerified,
}: {
  state: ProviderLoginState;
  onVerified?: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [verified, setVerified] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (verified) return null;
  async function verify() {
    setPending(true);
    setError(null);
    try {
      await verifyProviderLogin(state.provider, state.host);
      setVerified(true);
      onVerified?.();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setPending(false);
    }
  }
  return (
    <div className="provider-login-notice" role="status">
      <p>
        {signedOutNote(state)} Sign it in from Settings, Provider logins; parked work resumes once
        the login is verified.
      </p>
      {state.changed_at ? (
        <p className="provider-login-notice-since">
          Signed out since{" "}
          <time dateTime={state.changed_at}>{formatServerTimestamp(state.changed_at)}</time>.
        </p>
      ) : null}
      <button
        className="button secondary compact"
        type="button"
        disabled={pending}
        onClick={() => void verify()}
      >
        {pending ? "Checking…" : "Already signed in? Check again"}
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
