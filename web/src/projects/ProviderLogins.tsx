import {
  ArrowLeftRight,
  ArrowUpDown,
  Ellipsis,
  LoaderCircle,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  addProviderLogin,
  cancelProviderSignIn,
  loadProviderLoginSettings,
  loadProviderLogins,
  providerSignInStatus,
  removeProviderLogin,
  saveProviderLoginSetting,
  saveProviderToken,
  signOutProvider,
  startProviderSignIn,
  verifyProviderLogin,
} from "../core/api";
import { errorMessage } from "../core/errors";
import { Fold } from "../core/Fold";
import {
  cardState,
  loginKey,
  membersSignedIn,
  showsDeviceCode,
  signInsForHost,
  switchedSetting,
  trayLayout,
  type ProviderSignIns,
  type Tray,
} from "./providerLoginsModel";
import type {
  ProviderLoginAccount,
  ProviderLoginSettings,
  ProviderLoginSettingValue,
  ProviderSignInStatus,
} from "../core/types";
import { formatServerTimestamp } from "./ServerSettings";

const SIGN_IN_POLL_MS = 2000;
const SIGN_IN_POLL_FAILURE_LIMIT = 5;
/** How often the list refreshes while another member's sign-in runs. */
const OTHERS_SIGN_IN_REFRESH_MS = 5000;

/** One recoverable failure: a red edge and one icon that offers the action again. */
type Failure = { message: string; retry: () => void };

export interface SignInsData {
  rows: ProviderLoginAccount[] | null;
  settings: ProviderLoginSettings | null;
  error: string | null;
  refresh: () => Promise<void>;
  /** Writes the setting at once; the drawn value moves first and returns on failure. */
  saveSetting: (provider: string, host: string, value: ProviderLoginSettingValue) => Promise<void>;
}

/**
 * Every login and the viewer's settings, loaded once for all machine cards.
 *
 * The login belongs to its machine, so the Settings page asks once and each
 * card draws the rows for its own host. Nothing here ever holds a credential.
 */
export function useProviderSignIns(viewerUserId: string | null): SignInsData {
  const [rows, setRows] = useState<ProviderLoginAccount[] | null>(null);
  const [settings, setSettings] = useState<ProviderLoginSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    try {
      const [loaded, saved] = await Promise.all([
        loadProviderLogins(),
        loadProviderLoginSettings(),
      ]);
      setRows(loaded);
      setSettings(saved);
      setError(null);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, []);
  useEffect(() => {
    void refresh();
  }, [refresh]);
  // A starter follows their own attempt; everyone else sees its end on the next
  // list. A removal finishes on the server, so a draining row is followed too.
  const followList = Boolean(
    rows?.some(
      (row) =>
        row.draining_since ||
        (row.sign_in?.state === "pending" && row.sign_in.started_by !== viewerUserId),
    ),
  );
  useEffect(() => {
    if (!followList) return;
    const timer = setInterval(() => void refresh(), OTHERS_SIGN_IN_REFRESH_MS);
    return () => clearInterval(timer);
  }, [followList, refresh]);

  // Writes for one provider and host run one after another; the last one to
  // settle decides what is shown, and a failure reloads the stored value.
  const settingWrites = useRef(new Map<string, Promise<unknown>>());
  const saveSetting = useCallback(
    async (provider: string, host: string, value: ProviderLoginSettingValue) => {
      const replace = (setting: ProviderLoginSettingValue) =>
        setSettings((current) =>
          current
            ? {
                ...current,
                settings: current.settings.map((item) =>
                  item.provider === provider && item.host === host ? { ...item, setting } : item,
                ),
              }
            : current,
        );
      const key = `${provider}:${host}`;
      const write = (settingWrites.current.get(key) ?? Promise.resolve())
        .catch(() => undefined)
        .then(() => saveProviderLoginSetting(provider, host, value));
      settingWrites.current.set(key, write);
      replace(value);
      try {
        const saved = await write;
        if (settingWrites.current.get(key) === write) replace(saved.setting);
      } catch (failure) {
        if (settingWrites.current.get(key) === write) void refresh();
        throw failure;
      }
    },
    [refresh],
  );
  return { rows, settings, error, refresh, saveSetting };
}

interface FoldProps {
  machineName: string;
  host: string;
  data: SignInsData;
  viewerUserId: string | null;
  writesDisabled: boolean;
  /** Open this fold at the named provider's panel, as a notice asks. */
  focusProvider?: string | null;
  onLoginChanged?: () => void;
}

/** A machine card's "Sign-ins" fold: one panel per provider on this host. */
export function SignInsFold({
  machineName,
  host,
  data,
  viewerUserId,
  writesDisabled,
  focusProvider = null,
  onLoginChanged,
}: FoldProps) {
  const entries = data.rows ? signInsForHost(data.rows, host, viewerUserId) : [];
  const { refresh } = data;
  const changed = useCallback(async () => {
    await refresh();
    onLoginChanged?.();
  }, [refresh, onLoginChanged]);
  return (
    <Fold
      className={`machine-sign-ins${data.error ? " failed" : ""}`}
      title="Sign-ins"
      data-machine-fold="sign-ins"
    >
      {data.error ? (
        <button
          className="icon-button compact"
          type="button"
          aria-label="Retry"
          title={data.error}
          onClick={() => void refresh()}
        >
          <RotateCcw size={14} />
        </button>
      ) : null}
      <div className="sign-ins-panels" aria-busy={data.rows === null ? true : undefined}>
        {entries.map((entry) => (
          <ProviderPanel
            key={entry.provider}
            entry={entry}
            host={host}
            machineName={machineName}
            data={data}
            viewerUserId={viewerUserId}
            writesDisabled={writesDisabled}
            focused={focusProvider === entry.provider}
            onChanged={changed}
          />
        ))}
      </div>
    </Fold>
  );
}

function ProviderPanel({
  entry,
  host,
  machineName,
  data,
  viewerUserId,
  writesDisabled,
  focused,
  onChanged,
}: {
  entry: ProviderSignIns;
  host: string;
  machineName: string;
  data: SignInsData;
  viewerUserId: string | null;
  writesDisabled: boolean;
  focused: boolean;
  onChanged: () => Promise<void>;
}) {
  const panel = useRef<HTMLElement>(null);
  // The login whose token entry is open; ＋ on Claude opens it on the new login.
  const [entering, setEntering] = useState<string | null>(null);
  const [adding, setAdding] = useState<Tray | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const label = entry.label;
  const rows = data.rows ?? [];

  useEffect(() => {
    const element = panel.current;
    if (!focused || !element) return;
    const fold = element.closest("details");
    if (fold) fold.open = true;
    element.scrollIntoView({ block: "center" });
  }, [focused]);

  const card = (row: ProviderLoginAccount, props: Partial<CardProps> = {}) => (
    <SignInCard
      key={loginKey(row)}
      row={row}
      viewerUserId={viewerUserId}
      writesDisabled={writesDisabled}
      entering={entering === row.login_id}
      onEntering={(open) => setEntering(open ? row.login_id : null)}
      onChanged={onChanged}
      {...props}
    />
  );
  const sharedCards = entry.shared.map((row) =>
    row.login_id === ""
      ? card(row, {
          name: "Machine sign-in",
          nameHover: `This machine's own ${label} login.`,
          actions: entry.managed,
        })
      : card(row, {
          name: row.added_by_display_name ?? "",
          nameHover: row.added_at
            ? `Added by ${row.added_by_display_name ?? ""} on ${formatServerTimestamp(row.added_at)}.`
            : undefined,
          removable: true,
        }),
  );

  // RCP neither signs this CLI in nor checks its login: only its machine card shows.
  if (!entry.managed) {
    return (
      <section ref={panel} className="sign-ins-panel" data-sign-ins-provider={entry.provider}>
        <header>
          <strong>{label}</strong>
        </header>
        <div className="sign-ins-cards">{sharedCards.slice(0, 1)}</div>
      </section>
    );
  }

  async function add(tray: Tray) {
    setAdding(tray);
    setFailure(null);
    try {
      const added = await addProviderLogin(entry.provider, host, tray);
      const methods = rows.find((row) => row.provider === entry.provider)?.sign_in_methods ?? [];
      if (!added.sign_in && methods.includes("token_entry")) setEntering(added.login_id);
      await onChanged();
    } catch (cause) {
      setFailure({ message: errorMessage(cause), retry: () => void add(tray) });
    } finally {
      setAdding(null);
    }
  }
  const addSlot = (tray: Tray, hover: string) => (
    <button
      className="sign-in-add"
      type="button"
      data-sign-in-add={tray}
      aria-label={hover}
      title={hover}
      disabled={writesDisabled || adding !== null}
      onClick={() => void add(tray)}
    >
      {adding === tray ? <LoaderCircle size={14} className="spin" /> : <Plus size={14} />}
    </button>
  );
  const sharedTray = (props: Omit<TrayProps, "tray" | "title" | "hover" | "children">) => (
    <SignInTray
      key="shared"
      tray="shared"
      title={data.settings?.personal ? machineName : `Shared on ${machineName}`}
      hover="Any member can use, add or remove these."
      {...props}
    >
      {sharedCards}
      {addSlot("shared", `Add a shared ${label} sign-in`)}
    </SignInTray>
  );
  const failed = failure ? (
    <button
      className="icon-button compact"
      type="button"
      aria-label="Retry"
      title={failure.message}
      disabled={writesDisabled}
      onClick={failure.retry}
    >
      <RotateCcw size={14} />
    </button>
  ) : null;

  // A personal space owns every sign-in: one shared tray, nothing to choose.
  if (data.settings?.personal) {
    return (
      <section
        ref={panel}
        className={`sign-ins-panel${failure ? " failed" : ""}`}
        data-sign-ins-provider={entry.provider}
      >
        <header>
          <strong>{label}</strong>
          {failed}
        </header>
        <div className="sign-ins-trays">{sharedTray({ on: true, switchable: false })}</div>
      </section>
    );
  }

  const stored = data.settings?.settings.find(
    (item) => item.provider === entry.provider && item.host === host,
  );
  const setting = stored?.setting ?? "own";
  const layout = trayLayout(setting);
  const settingsReady = Boolean(stored) && !writesDisabled;
  const write = (value: ProviderLoginSettingValue) => {
    setFailure(null);
    data
      .saveSetting(entry.provider, host, value)
      .catch((cause: unknown) =>
        setFailure({ message: errorMessage(cause), retry: () => write(value) }),
      );
  };
  const toggle = (tray: Tray, on: boolean) => {
    const next = switchedSetting(setting, tray, on);
    if (next && next !== setting) write(next);
  };
  const trayProps = (tray: Tray) => ({
    on: layout.on[tray],
    switchable: true,
    locked: layout.locked === tray,
    switchDisabled: !settingsReady,
    onToggle: (on: boolean) => toggle(tray, on),
  });
  const trays: Record<Tray, ReactNode> = {
    own: (
      <SignInTray
        key="own"
        tray="own"
        title="Yours"
        hover={`Your own ${label} account on ${machineName}. Only you can change it.`}
        {...trayProps("own")}
      >
        {entry.own ? card(entry.own) : addSlot("own", `Add your own ${label} sign-in`)}
      </SignInTray>
    ),
    shared: sharedTray(trayProps("shared")),
  };
  const swapHover = layout.swap ? "Swap the order" : "Turn both on to choose the order";
  const members = membersSignedIn(entry.members, data.settings?.other_member_count ?? 0);
  return (
    <section
      ref={panel}
      className={`sign-ins-panel${failure ? " failed" : ""}`}
      data-sign-ins-provider={entry.provider}
      data-setting={setting}
    >
      <header>
        <strong>{label}</strong>
        {failed}
      </header>
      <div className="sign-ins-trays">
        {trays[layout.order[0]]}
        <button
          className="sign-ins-swap"
          type="button"
          data-sign-ins-swap=""
          aria-label={swapHover}
          title={swapHover}
          aria-disabled={!layout.swap || !settingsReady || undefined}
          onClick={() => {
            if (layout.swap && settingsReady) write(layout.swap);
          }}
        >
          <ArrowLeftRight size={15} className="sign-ins-swap-wide" aria-hidden="true" />
          <ArrowUpDown size={15} className="sign-ins-swap-narrow" aria-hidden="true" />
        </button>
        {trays[layout.order[1]]}
      </div>
      <details
        className="sign-ins-members"
        title={`Other members' own sign-ins. Signed in: ${members.signedIn} of ${members.of}.`}
      >
        <summary>
          <strong>Members</strong>
          <span>
            <span className="sign-ins-members-mark" aria-hidden="true">
              ▸
            </span>{" "}
            {members.signedIn} of {members.of}
          </span>
        </summary>
        <div className="sign-ins-cards">
          {entry.members.map((row) =>
            card(row, { name: row.owner_display_name ?? "", actions: false }),
          )}
        </div>
      </details>
    </section>
  );
}

interface TrayProps {
  tray: Tray;
  title: string;
  hover: string;
  on: boolean;
  switchable: boolean;
  locked?: boolean;
  switchDisabled?: boolean;
  onToggle?: (on: boolean) => void;
  children: ReactNode;
}

function SignInTray({
  tray,
  title,
  hover,
  on,
  switchable,
  locked = false,
  switchDisabled = false,
  onToggle,
  children,
}: TrayProps) {
  const switchHover = locked ? "One group must stay on" : on ? "Used" : "Not used";
  return (
    <section className={`sign-ins-tray${on ? "" : " off"}`} data-tray={tray}>
      <header>
        <strong title={hover}>{title}</strong>
        {switchable ? (
          <input
            className="sign-ins-switch"
            type="checkbox"
            role="switch"
            data-tray-switch={tray}
            aria-label={title}
            title={switchHover}
            checked={on}
            disabled={switchDisabled}
            aria-disabled={locked || undefined}
            onChange={(event) => {
              if (!locked) onToggle?.(event.target.checked);
            }}
          />
        ) : null}
      </header>
      <div className="sign-ins-cards">{children}</div>
    </section>
  );
}

interface CardProps {
  row: ProviderLoginAccount;
  viewerUserId: string | null;
  writesDisabled: boolean;
  /** Shown before the state on shared and members' cards; the Yours card has none. */
  name?: string;
  nameHover?: string;
  /** Members' cards and an unmanaged CLI offer nothing. */
  actions?: boolean;
  removable?: boolean;
  entering: boolean;
  onEntering: (open: boolean) => void;
  onChanged: () => Promise<void>;
}

/** One login: its name, the one state that wins, and its actions behind ⋯. */
export function SignInCard({
  row,
  viewerUserId,
  writesDisabled,
  name,
  nameHover,
  actions = true,
  removable = false,
  entering,
  onEntering,
  onChanged,
}: CardProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [menu, setMenu] = useState<"closed" | "open" | "confirm-remove">("closed");
  const [token, setToken] = useState("");
  const [signIn, setSignIn] = useState<ProviderSignInStatus | null>(row.sign_in);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // The parent passes a new callback on every render, and the app re-renders
  // about once a second; depending on it restarted the poll before it ever fired.
  const onChangedRef = useRef(onChanged);
  useEffect(() => {
    onChangedRef.current = onChanged;
  }, [onChanged]);

  // A settled attempt is history once the list stops reporting it: the card's
  // state already carries the outcome.
  // Someone else's attempt is only known through the list, so mirror it as is.
  useEffect(() => {
    const attempt = row.sign_in ?? signIn;
    if (!viewerUserId || attempt?.started_by !== viewerUserId) {
      if (row.sign_in !== signIn) setSignIn(row.sign_in);
    } else if (row.sign_in) {
      if (row.sign_in.attempt_id !== signIn?.attempt_id) setSignIn(row.sign_in);
    } else if (signIn && signIn.state !== "pending") {
      setSignIn(null);
    }
  }, [row.sign_in, signIn, viewerUserId]);

  // Only the starter may read an attempt; everyone else follows the list.
  const starter = Boolean(viewerUserId && signIn?.started_by === viewerUserId);
  useEffect(() => {
    if (!signIn || signIn.state !== "pending" || !starter) return;
    let cancelled = false;
    let failures = 0;
    const poll = async () => {
      try {
        const status = await providerSignInStatus(row.provider, signIn.attempt_id);
        if (cancelled) return;
        failures = 0;
        setFailure(null);
        setSignIn(status);
        if (status.state === "pending") {
          pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
        } else {
          await onChangedRef.current();
        }
      } catch (cause) {
        if (cancelled) return;
        setFailure({
          message: errorMessage(cause),
          retry: () => {
            setFailure(null);
            void onChangedRef.current();
          },
        });
        // A transient status failure must not leave the card disabled forever.
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
  }, [row.provider, signIn, starter]);

  async function run(kind: string, action: () => Promise<unknown>) {
    setBusy(kind);
    setFailure(null);
    setMenu("closed");
    try {
      await action();
      await onChanged();
    } catch (cause) {
      setFailure({ message: errorMessage(cause), retry: () => void run(kind, action) });
    } finally {
      setBusy(null);
    }
  }
  const { provider, host, login_id: loginId } = row;
  const startSignIn = () => {
    if (row.sign_in_methods.includes("device_code")) {
      void run("sign-in", async () =>
        setSignIn(await startProviderSignIn(provider, host, loginId)),
      );
    } else if (row.sign_in_methods.includes("token_entry")) {
      onEntering(true);
    }
  };

  const shown = { ...row, sign_in: signIn };
  const state = cardState(shown);
  const disabled = writesDisabled || busy !== null;
  // A failed device sign-in is the card's failure too.
  const failed =
    failure ??
    (signIn?.state === "failed" && starter
      ? {
          message: signIn.detail ?? "",
          retry: () => {
            setSignIn(null);
            startSignIn();
          },
        }
      : null);
  const canVerify =
    row.token !== null || row.sign_in_methods.some((method) => method !== "token_entry");
  const items = [
    canVerify && {
      id: "verify",
      label: "Check sign-in",
      act: () => void run("verify", () => verifyProviderLogin(provider, host, loginId)),
    },
    (row.state === "signed_in" || row.token) && {
      id: "sign-out",
      label: "Sign out",
      act: () => void run("sign-out", () => signOutProvider(provider, host, loginId)),
    },
    removable && {
      id: "remove",
      label: "Remove from shared",
      act: () => setMenu("confirm-remove"),
    },
  ].filter((item): item is { id: string; label: string; act: () => void } => Boolean(item));
  // A running sign-in or a removal has nothing else to offer until it ends.
  const hasMenu =
    actions && row.managed && state !== "draining" && state !== "signing_in" && items.length > 0;

  let body: ReactNode;
  if (state === "draining") {
    body = (
      <span className="sign-in-state" title={`${row.bound_running} running`}>
        Being removed
      </span>
    );
  } else if (state === "signing_in" && signIn) {
    body = showsDeviceCode(signIn, viewerUserId) ? (
      <span className="sign-in-device" role="status">
        <a href={signIn.verification_url!} target="_blank" rel="noreferrer">
          {signIn.verification_url}
        </a>
        <code>{signIn.user_code}</code>
        <button
          className="icon-button compact"
          type="button"
          data-sign-in-action="cancel"
          aria-label="Cancel sign-in"
          title="Cancel sign-in"
          disabled={disabled}
          onClick={() =>
            void run("cancel", async () =>
              setSignIn(await cancelProviderSignIn(provider, signIn.attempt_id)),
            )
          }
        >
          {busy === "cancel" ? <LoaderCircle size={14} className="spin" /> : <X size={14} />}
        </button>
      </span>
    ) : starter ? (
      <LoaderCircle size={14} className="spin sign-in-state" aria-label="Signing in" />
    ) : (
      <span className="sign-in-state">
        {signIn.started_by_display_name ?? "A member"} is signing in
      </span>
    );
  } else if (state === "signed_out") {
    body =
      entering && actions ? (
        <form
          className="sign-in-token"
          onSubmit={(event) => {
            event.preventDefault();
            const pasted = token.trim();
            if (!pasted) return;
            void run("token", async () => {
              await saveProviderToken(provider, host, loginId, pasted);
              setToken("");
              onEntering(false);
            });
          }}
        >
          <input
            type="password"
            autoComplete="off"
            autoFocus
            aria-label={`${row.label} token`}
            title={row.token_instructions ?? undefined}
            value={token}
            disabled={disabled}
            onChange={(event) => setToken(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") onEntering(false);
            }}
          />
          <button
            className="icon-button compact"
            type="submit"
            aria-label="Save token"
            title="Save token"
            disabled={disabled || !token.trim()}
          >
            {busy === "token" ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
          </button>
        </form>
      ) : actions && row.managed ? (
        <button
          className="button compact"
          type="button"
          data-sign-in-action="sign-in"
          disabled={disabled}
          onClick={startSignIn}
        >
          {busy === "sign-in" ? <LoaderCircle size={14} className="spin" /> : null}
          Sign in
        </button>
      ) : null;
  } else if (state === "limited") {
    body = (
      <span
        className="sign-in-state"
        title={`Hit a usage limit ${formatServerTimestamp(row.limited_at)}. Used when nothing else is free.`}
      >
        Usage limit
      </span>
    );
  } else {
    body = row.managed ? (
      <span
        className="sign-in-state"
        title={`Signed in · checked ${formatServerTimestamp(row.token?.verified_at ?? row.changed_at)}`}
      >
        Ready
      </span>
    ) : null;
  }

  return (
    <article
      className={`sign-in-card${failed ? " failed" : ""}`}
      data-card-state={state}
      data-login-id={row.login_id}
    >
      {name ? (
        <span className="sign-in-name" title={nameHover}>
          {name}
        </span>
      ) : null}
      {body}
      {failed ? (
        <button
          className="icon-button compact"
          type="button"
          aria-label="Retry"
          title={failed.message || undefined}
          disabled={writesDisabled || busy !== null}
          onClick={failed.retry}
        >
          <RotateCcw size={14} />
        </button>
      ) : null}
      {hasMenu ? (
        <span
          className="sign-in-more"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) setMenu("closed");
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") setMenu("closed");
          }}
        >
          <button
            className="icon-button compact"
            type="button"
            data-sign-in-action="more"
            aria-label="More"
            title="More"
            aria-haspopup="menu"
            aria-expanded={menu !== "closed"}
            disabled={disabled}
            onClick={() => setMenu(menu === "closed" ? "open" : "closed")}
          >
            {busy && busy !== "sign-in" && busy !== "token" && busy !== "cancel" ? (
              <LoaderCircle size={14} className="spin" />
            ) : (
              <Ellipsis size={14} />
            )}
          </button>
          {menu === "open" ? (
            <span className="sign-in-menu" role="menu">
              {items.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  role="menuitem"
                  data-sign-in-action={item.id}
                  onClick={item.act}
                >
                  {item.label}
                </button>
              ))}
            </span>
          ) : menu === "confirm-remove" ? (
            <span className="sign-in-menu" role="menu">
              <button
                type="button"
                role="menuitem"
                className="danger"
                data-sign-in-action="confirm-remove"
                autoFocus
                onClick={() =>
                  void run("remove", () => removeProviderLogin(provider, host, loginId))
                }
              >
                <Trash2 size={14} aria-hidden="true" />
                Remove from shared
                <span className="tag" data-bound-running={row.bound_running}>
                  {row.bound_running} running
                </span>
              </button>
            </span>
          ) : null}
        </span>
      ) : null}
    </article>
  );
}
