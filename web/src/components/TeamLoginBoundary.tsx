import { LoaderCircle } from "lucide-react";
import { useState } from "react";
import { ApiError } from "../api";
import { initialPairingCode } from "../pairingLink";

type SignInMode = "pair" | "token";

interface Props {
  spaceName: string | null;
  onAuthenticate: (token: string) => Promise<void>;
  onPair: (code: string, label: string) => Promise<void>;
  initialMode?: SignInMode;
  initialCode?: string | null;
}

export function TeamLoginBoundary({
  spaceName,
  onAuthenticate,
  onPair,
  initialMode = "pair",
  initialCode = initialPairingCode,
}: Props) {
  const [mode, setMode] = useState<SignInMode>(initialCode ? "pair" : initialMode);
  const [token, setToken] = useState("");
  const [code, setCode] = useState(initialCode ?? "");
  const [label, setLabel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const resolvedSpaceName = spaceName?.trim() || "Team space";

  const switchMode = (next: SignInMode) => {
    setMode(next);
    setError(null);
  };

  const authenticate = async () => {
    const submittedToken = token.trim();
    if (!submittedToken || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onAuthenticate(submittedToken);
      setToken("");
    } catch (caught) {
      setToken("");
      setError(teamLoginFailureMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const pair = async () => {
    const submittedCode = code.trim();
    const submittedLabel = label.trim();
    if (!submittedCode || !submittedLabel || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await onPair(submittedCode, submittedLabel);
      setCode("");
      setLabel("");
    } catch (caught) {
      setError(teamPairingFailureMessage(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main className="team-login-boundary">
      <section
        className="team-login-card"
        aria-labelledby="team-login-title"
        data-team-login={mode === "pair" ? "device-pairing" : "credential-slip"}
      >
        <header>
          <span className="team-login-mark" aria-hidden="true">
            RCP
          </span>
          <span className="team-login-space-kind">Team space</span>
        </header>
        <div className="team-login-card-body">
          {mode === "pair" ? (
            <>
              <h1 id="team-login-title">Connect this device to {resolvedSpaceName}</h1>
              <p className="team-login-hint">
                On a device that is already signed in, open your profile, choose{" "}
                <strong>Connect a device</strong> under Devices, and enter the code it shows here.
              </p>
              <form
                autoComplete="off"
                onSubmit={(event) => {
                  event.preventDefault();
                  void pair();
                }}
              >
                <label htmlFor="team-login-code">Device code</label>
                <input
                  id="team-login-code"
                  name="device-code"
                  type="text"
                  inputMode="text"
                  autoComplete="one-time-code"
                  autoCapitalize="characters"
                  autoCorrect="off"
                  spellCheck={false}
                  placeholder="ABCD-EFGHJK"
                  value={code}
                  onChange={(event) => {
                    setCode(event.target.value);
                    setError(null);
                  }}
                  autoFocus
                />
                <label htmlFor="team-login-device-name">Name this device</label>
                <input
                  id="team-login-device-name"
                  name="device-name"
                  type="text"
                  autoComplete="off"
                  autoCorrect="off"
                  maxLength={80}
                  placeholder="Ada's iPhone"
                  value={label}
                  onChange={(event) => {
                    setLabel(event.target.value);
                    setError(null);
                  }}
                />
                {error && (
                  <p className="team-login-error" role="alert">
                    {error}
                  </p>
                )}
                <button
                  className="button primary"
                  type="submit"
                  disabled={!code.trim() || !label.trim() || submitting}
                >
                  {submitting ? <LoaderCircle className="spin" size={14} /> : null}
                  {submitting ? "Connecting" : "Connect this device"}
                </button>
              </form>
              <button
                className="team-login-switch"
                type="button"
                onClick={() => switchMode("token")}
              >
                Sign in with a team token instead
              </button>
            </>
          ) : (
            <>
              <h1 id="team-login-title">Sign in to {resolvedSpaceName}</h1>
              <form
                autoComplete="off"
                onSubmit={(event) => {
                  event.preventDefault();
                  void authenticate();
                }}
              >
                <label htmlFor="team-login-token">Personal team token</label>
                <input
                  id="team-login-token"
                  name="team-token"
                  type="password"
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  value={token}
                  onChange={(event) => {
                    setToken(event.target.value);
                    setError(null);
                  }}
                  autoFocus
                />
                {error && (
                  <p className="team-login-error" role="alert">
                    {error}
                  </p>
                )}
                <button
                  className="button primary"
                  type="submit"
                  disabled={!token.trim() || submitting}
                >
                  {submitting ? <LoaderCircle className="spin" size={14} /> : null}
                  {submitting ? "Signing in" : "Sign in"}
                </button>
              </form>
              <button
                className="team-login-switch"
                type="button"
                onClick={() => switchMode("pair")}
              >
                Connect with a device code instead
              </button>
            </>
          )}
        </div>
      </section>
    </main>
  );
}

export function teamLoginFailureMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "That team token was not accepted. Paste a current token and try again.";
  }
  if (error instanceof ApiError && error.status === 429) {
    return "Too many attempts were made with that token. Wait, then try a current token.";
  }
  return "RCP could not sign in to this team space. Check the connection and try again.";
}

export function teamPairingFailureMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 401) {
    return "That device code was not accepted. Check it and try again.";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "That device code was already used. Get a new one from a signed-in device.";
  }
  if (error instanceof ApiError && error.status === 410) {
    return "That device code expired. Get a new one from a signed-in device.";
  }
  if (error instanceof ApiError && error.status === 429) {
    return "Too many attempts were made with that code. Get a new one from a signed-in device.";
  }
  return "RCP could not connect this device. Check the connection and try again.";
}
