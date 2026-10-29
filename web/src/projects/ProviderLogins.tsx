import { CircleHelp, KeyRound, LoaderCircle, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from "react";
import {
  cancelProviderSignIn,
  providerSignInStatus,
  loadProviderLogins,
  loadSpaceUsers,
  saveProviderToken,
  signOutProvider,
  startProviderSignIn,
  verifyProviderLogin,
} from "../core/api";
import { accountLabel, signInNote, tokenNote } from "./providerLoginsModel";
import type { ProviderLoginAccount, ProviderSignInStatus } from "../core/types";
import { formatServerTimestamp } from "./ServerSettings";

const SIGN_IN_POLL_MS = 2000;
const SIGN_IN_POLL_FAILURE_LIMIT = 5;

interface Props {
  spaceKind: "personal" | "team";
  writesDisabled?: boolean;
  /** A login change invalidates provider readiness the page already shows. */
  onLoginChanged?: () => void;
}

/**
 * Settings card: sign each execution account in or out of a provider from the product.
 *
 * The login belongs to the machine account every member shares, so any signed-in
 * member may operate it; the server records who did. Nothing here ever shows a
 * credential: device sign-in shows a code and a link; token entry takes a pasted
 * token that is sent once and never read back.
 */
export function ProviderLogins({ spaceKind, writesDisabled = false, onLoginChanged }: Props) {
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
    <section className={`settings-section provider-login-settings${loadError ? " failed" : ""}`}>
      <header>
        <span>
          <KeyRound size={16} />
        </span>
        <h2>Provider logins</h2>
        {loadError ? (
          <button
            className="icon-button compact"
            type="button"
            aria-label="Retry provider logins"
            title={loadError}
            onClick={() => void refresh()}
          >
            <RotateCcw size={14} />
          </button>
        ) : null}
      </header>
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
            onChanged={async () => {
              await refresh();
              onLoginChanged?.();
            }}
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
  const [busy, setBusy] = useState<"verify" | "sign-in" | "token" | "sign-out" | "cancel" | null>(
    null,
  );
  // A failed action marks the row and offers itself again from one icon.
  const [failure, setFailure] = useState<{ message: string; retry: () => void } | null>(null);
  const [token, setToken] = useState("");
  const helpId = useId();
  const [signIn, setSignIn] = useState<ProviderSignInStatus | null>(account.sign_in);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The parent passes a new callback on every render, and the app re-renders
  // about once a second; depending on it restarted the poll before it ever fired.
  const onChangedRef = useRef(onChanged);
  useEffect(() => {
    onChangedRef.current = onChanged;
  }, [onChanged]);

  // A sign-in another member started is running too; follow it the same way.
  // A settled one is history once the server stops reporting it: the account
  // line beside it already carries the outcome, so keeping the panel would
  // contradict the state the row now shows.
  useEffect(() => {
    if (account.sign_in) {
      if (account.sign_in.login_id !== signIn?.login_id) setSignIn(account.sign_in);
    } else if (signIn && signIn.state !== "pending") {
      setSignIn(null);
    }
  }, [account.sign_in, signIn]);

  useEffect(() => {
    if (!signIn || signIn.state !== "pending") return;
    let cancelled = false;
    let failures = 0;
    const poll = async () => {
      try {
        const status = await providerSignInStatus(account.provider, signIn.login_id);
        if (cancelled) return;
        failures = 0;
        setFailure(null);
        setSignIn(status);
        if (status.state === "pending") {
          pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
        } else {
          await onChangedRef.current();
        }
      } catch (failure) {
        if (cancelled) return;
        setFailure({
          message: failure instanceof Error ? failure.message : String(failure),
          retry: () => {
            setFailure(null);
            void onChangedRef.current();
          },
        });
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
  }, [account.provider, signIn]);

  async function run(kind: NonNullable<typeof busy>, action: () => Promise<unknown>) {
    setBusy(kind);
    setFailure(null);
    try {
      await action();
      await onChanged();
    } catch (cause) {
      setFailure({
        message: cause instanceof Error ? cause.message : String(cause),
        retry: () => void run(kind, action),
      });
    } finally {
      setBusy(null);
    }
  }
  const startSignIn = () =>
    run("sign-in", async () =>
      setSignIn(await startProviderSignIn(account.provider, account.host)),
    );

  const label = account.label;
  const disabled = writesDisabled || busy !== null || signIn?.state === "pending";
  // Rechecking has nothing to recheck when the only way in is a credential the
  // member has not saved yet: offering it there gave a signed-out row two
  // buttons, one of which could only fail.
  const canVerify =
    account.token !== null || account.sign_in_methods.some((method) => method !== "token_entry");

  if (!account.managed) {
    // RCP neither signs this CLI in nor checks its login, so it claims no state.
    return (
      <article className="provider-login-account" data-provider-login="unmanaged">
        <header>
          <strong>{label}</strong>
          <span>{accountLabel(account, spaceKind)}</span>
          <LoginHelp id={helpId} label={`How ${label} signs in`}>
            RCP does not manage this login; {label} uses its own. For a model backend that needs a
            sign-in, run <code>{account.login_command}</code> as this machine account.
          </LoginHelp>
        </header>
      </article>
    );
  }

  // The row's history and token sit in its state's tooltip, not in lines under it.
  const since = account.changed_at
    ? `Since ${formatServerTimestamp(account.changed_at)}${
        account.changed_by ? ` by ${memberName(account.changed_by)}` : ""
      }${account.detail ? `: ${account.detail}` : "."}`
    : "";
  const tokenText = account.token
    ? tokenNote({ ...account.token, pasted_by: memberName(account.token.pasted_by) }, new Date())
    : "";
  // A device-code sign-in that ended in failure is the row's failure too.
  const failed =
    failure ??
    (signIn?.state === "failed"
      ? {
          message: signInNote(signIn),
          retry: () => {
            setSignIn(null);
            void startSignIn();
          },
        }
      : null);

  return (
    <article className={`provider-login-account ${account.state}${failed ? " failed" : ""}`}>
      <header>
        <strong>{label}</strong>
        <span>{accountLabel(account, spaceKind)}</span>
        <span
          className={`provider-path-state ${account.state === "signed_in" ? "ready" : "error"}`}
          title={[since, tokenText].filter(Boolean).join(" ") || undefined}
        >
          {account.state === "signed_in" ? "Signed in" : "Signed out"}
        </span>
      </header>
      {signIn?.state === "pending" ? (
        <div className="provider-login-sign-in" role="status">
          <p>{signInNote(signIn)}</p>
          {signIn.verification_url && signIn.user_code ? (
            <p className="provider-login-code">
              <a href={signIn.verification_url} target="_blank" rel="noreferrer">
                {signIn.verification_url}
              </a>
              <code>{signIn.user_code}</code>
            </p>
          ) : null}
          <button
            className="button secondary compact"
            type="button"
            disabled={writesDisabled || busy === "cancel"}
            onClick={() =>
              void run("cancel", async () =>
                setSignIn(await cancelProviderSignIn(account.provider, signIn.login_id)),
              )
            }
          >
            {busy === "cancel" ? <LoaderCircle size={14} className="spin" /> : null}
            Cancel sign-in
          </button>
        </div>
      ) : null}
      <div className="provider-login-actions">
        {account.sign_in_methods.includes("device_code") ? (
          <button
            className="button compact"
            data-provider-action="sign-in"
            type="button"
            disabled={disabled}
            onClick={() => void startSignIn()}
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
                await saveProviderToken(account.provider, account.host, pasted);
                setToken("");
              });
            }}
          >
            {account.token_instructions ? (
              <LoginHelp id={helpId} label={`How to get a ${label} token`}>
                {account.token_instructions}
              </LoginHelp>
            ) : null}
            <input
              type="password"
              autoComplete="off"
              aria-label={`${label} token for ${accountLabel(account, spaceKind)}`}
              placeholder="Paste token"
              value={token}
              disabled={disabled}
              onChange={(event) => setToken(event.target.value)}
            />
            <button className="button compact" type="submit" disabled={disabled || !token.trim()}>
              {busy === "token" ? <LoaderCircle size={14} className="spin" /> : null}
              Sign in
            </button>
          </form>
        ) : null}
        {canVerify ? (
          <button
            data-provider-action="verify"
            className="button secondary compact"
            type="button"
            disabled={disabled || token.trim() !== ""}
            onClick={() =>
              void run("verify", () => verifyProviderLogin(account.provider, account.host))
            }
          >
            {busy === "verify" ? <LoaderCircle size={14} className="spin" /> : null}
            Verify sign-in
          </button>
        ) : null}
        {account.state === "signed_in" || account.token ? (
          <button
            className="button secondary compact"
            type="button"
            disabled={disabled}
            onClick={() =>
              void run("sign-out", () => signOutProvider(account.provider, account.host))
            }
          >
            {busy === "sign-out" ? <LoaderCircle size={14} className="spin" /> : null}
            Sign out
          </button>
        ) : null}
        {failed ? (
          <button
            className="icon-button compact"
            type="button"
            aria-label="Retry"
            title={failed.message}
            disabled={writesDisabled || busy !== null}
            onClick={failed.retry}
          >
            <RotateCcw size={14} />
          </button>
        ) : null}
      </div>
    </article>
  );
}

/** How a login is done, kept in a popover its icon opens rather than a line under the row. */
function LoginHelp({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <>
      <button
        className="icon-button compact provider-login-help-button"
        type="button"
        aria-label={label}
        popoverTarget={id}
      >
        <CircleHelp size={14} />
      </button>
      <div id={id} className="provider-login-help" popover="auto" role="dialog" aria-label={label}>
        {children}
      </div>
    </>
  );
}
