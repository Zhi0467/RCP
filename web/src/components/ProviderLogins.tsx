import { KeyRound, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  providerSignInStatus,
  loadProviderLogins,
  loadSpaceUsers,
  saveProviderToken,
  signOutProvider,
  startProviderSignIn,
  verifyProviderLogin,
} from "../api";
import { accountLabel, resumedNote, signInNote, tokenNote } from "../providerLogins";
import type { ProviderLoginAccount, ProviderSignInStatus } from "../types";
import { formatServerTimestamp } from "./ServerSettings";

const SIGN_IN_POLL_MS = 2000;
const SIGN_IN_POLL_FAILURE_LIMIT = 5;

interface Props {
  spaceKind: "personal" | "team";
  writesDisabled?: boolean;
}

/**
 * Settings card: sign each execution account in or out of a provider from the product.
 *
 * The login belongs to the machine account every member shares, so any signed-in
 * member may operate it; the server records who did. Nothing here ever shows a
 * credential: device sign-in shows a code and a link; token entry takes a pasted
 * token that is sent once and never read back.
 */
export function ProviderLogins({ spaceKind, writesDisabled = false }: Props) {
  const [accounts, setAccounts] = useState<ProviderLoginAccount[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const [loaded, users] = await Promise.all([loadProviderLogins(), loadSpaceUsers()]);
      setAccounts(loaded);
      setNames(
        Object.fromEntries(
          users
            .filter((user) => user.display_name)
            .map((user) => [user.user_id, user.display_name!]),
        ),
      );
      setLoadError(null);
    } catch (failure) {
      setLoadError(failure instanceof Error ? failure.message : String(failure));
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <section className="settings-section provider-login-settings">
      <header>
        <span>
          <KeyRound size={16} />
        </span>
        <h2>Provider logins</h2>
      </header>
      <p className="provider-login-intro">
        One login per provider per machine account, shared by every member and verified by one
        authenticated request. Available sign-in methods are shown for each account.
      </p>
      {loadError ? <p role="alert">{loadError}</p> : null}
      {accounts === null && !loadError ? <p className="provider-login-intro">Loading…</p> : null}
      {accounts?.length === 0 ? (
        <p className="provider-login-intro">No provider accounts are configured yet.</p>
      ) : null}
      <div className="provider-login-list">
        {accounts?.map((account) => (
          <ProviderLoginRow
            key={`${account.provider}:${account.host}`}
            account={account}
            spaceKind={spaceKind}
            writesDisabled={writesDisabled}
            memberName={(id) => names[id] ?? "a member"}
            onChanged={refresh}
          />
        ))}
      </div>
    </section>
  );
}

export function ProviderLoginRow({
  account,
  spaceKind,
  writesDisabled,
  memberName,
  onChanged,
}: {
  account: ProviderLoginAccount;
  spaceKind: "personal" | "team";
  writesDisabled: boolean;
  memberName: (id: string) => string;
  onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<"verify" | "sign-in" | "token" | "sign-out" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [signIn, setSignIn] = useState<ProviderSignInStatus | null>(account.sign_in);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // A sign-in another member started is running too; follow it the same way.
  useEffect(() => {
    if (account.sign_in && account.sign_in.login_id !== signIn?.login_id)
      setSignIn(account.sign_in);
  }, [account.sign_in, signIn?.login_id]);

  useEffect(() => {
    if (!signIn || signIn.state !== "pending") return;
    let cancelled = false;
    let failures = 0;
    const poll = async () => {
      try {
        const status = await providerSignInStatus(account.provider, signIn.login_id);
        if (cancelled) return;
        failures = 0;
        setSignIn(status);
        if (status.state === "pending") {
          pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
        } else {
          await onChanged();
        }
      } catch (failure) {
        if (cancelled) return;
        setError(failure instanceof Error ? failure.message : String(failure));
        // A transient status failure must not leave the row disabled forever:
        // keep following the sign-in, and give the row back after repeated failures.
        failures += 1;
        if (failures < SIGN_IN_POLL_FAILURE_LIMIT) {
          pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
        } else {
          setSignIn(null);
        }
      }
    };
    pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
    return () => {
      cancelled = true;
      if (pollTimer.current) clearTimeout(pollTimer.current);
    };
  }, [account.provider, onChanged, signIn]);

  async function run(kind: NonNullable<typeof busy>, action: () => Promise<string | null>) {
    setBusy(kind);
    setError(null);
    setMessage(null);
    try {
      const note = await action();
      if (note) setMessage(note);
      await onChanged();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setBusy(null);
    }
  }

  const label = account.label;
  const disabled = writesDisabled || busy !== null || signIn?.state === "pending";

  return (
    <article className={`provider-login-account ${account.state}`}>
      <header>
        <strong>{label}</strong>
        <span>{accountLabel(account, spaceKind)}</span>
        <span
          className={`provider-path-state ${account.state === "signed_in" ? "ready" : "error"}`}
        >
          {account.state === "signed_in" ? "Signed in" : "Signed out"}
        </span>
      </header>
      <p className="provider-login-detail">
        {account.changed_at ? (
          <>
            {account.state === "signed_in" ? "Signed in" : "Signed out"} since{" "}
            <time dateTime={account.changed_at}>{formatServerTimestamp(account.changed_at)}</time>
            {account.changed_by ? ` by ${memberName(account.changed_by)}` : ""}
            {account.detail ? `: ${account.detail}` : "."}
          </>
        ) : (
          "No login change has been recorded."
        )}
      </p>
      {account.token ? (
        <p className="provider-login-detail">
          {tokenNote(
            { ...account.token, pasted_by: memberName(account.token.pasted_by) },
            new Date(),
          )}
        </p>
      ) : null}
      {signIn ? (
        <div className="provider-login-sign-in" role="status">
          <p>{signInNote(signIn)}</p>
          {signIn.state === "pending" && signIn.verification_url && signIn.user_code ? (
            <p className="provider-login-code">
              <a href={signIn.verification_url} target="_blank" rel="noreferrer">
                {signIn.verification_url}
              </a>
              <code>{signIn.user_code}</code>
            </p>
          ) : null}
        </div>
      ) : null}
      <div className="provider-login-actions">
        {account.sign_in_methods.includes("device_code") ? (
          <button
            className="button compact"
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("sign-in", async () => {
                setSignIn(await startProviderSignIn(account.provider, account.host));
                return null;
              })
            }
          >
            {busy === "sign-in" ? <LoaderCircle size={14} className="spin" /> : null}
            Sign in with device code
          </button>
        ) : null}
        {account.sign_in_methods.includes("token_entry") ? (
          <form
            className="provider-login-token"
            onSubmit={(event) => {
              event.preventDefault();
              const pasted = token.trim();
              if (!pasted) return;
              void run("token", async () => {
                const result = await saveProviderToken(account.provider, account.host, pasted);
                setToken("");
                return resumedNote(result.resumed);
              });
            }}
          >
            <input
              type="password"
              autoComplete="off"
              aria-label={`${label} token for ${accountLabel(account, spaceKind)}`}
              placeholder={account.token_instructions ?? "Paste token"}
              value={token}
              disabled={disabled}
              onChange={(event) => setToken(event.target.value)}
            />
            <button className="button compact" type="submit" disabled={disabled || !token.trim()}>
              {busy === "token" ? <LoaderCircle size={14} className="spin" /> : null}
              Save token
            </button>
          </form>
        ) : null}
        <button
          className="button secondary compact"
          type="button"
          disabled={disabled}
          onClick={() =>
            void run("verify", async () => {
              const result = await verifyProviderLogin(account.provider, account.host);
              return resumedNote(result.resumed);
            })
          }
        >
          {busy === "verify" ? <LoaderCircle size={14} className="spin" /> : null}
          Verify sign-in
        </button>
        {account.state === "signed_in" || account.token ? (
          <button
            className="button secondary compact"
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("sign-out", async () => {
                await signOutProvider(account.provider, account.host);
                return `${label} is signed out on this account; new turns wait for a sign-in.`;
              })
            }
          >
            {busy === "sign-out" ? <LoaderCircle size={14} className="spin" /> : null}
            Sign out
          </button>
        ) : null}
      </div>
      {message ? <p className="provider-login-detail">{message}</p> : null}
      {error ? <p role="alert">{error}</p> : null}
    </article>
  );
}
