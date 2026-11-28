import {
  ArrowLeftRight,
  ArrowUpDown,
  CircleHelp,
  ExternalLink,
  Info,
  LoaderCircle,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
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
  submitProviderSignInCode,
  verifyProviderLogin,
} from "../core/api";
import { errorMessage } from "../core/errors";
import { Fold } from "../core/Fold";
import {
  cardState,
  loginKey,
  membersSignedIn,
  showsDeviceCode,
  showsPasteCode,
  signInsForMachine,
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
import { OverflowMenu } from "../ui/OverflowMenu";
import { askedLabel } from "../agents/agentThreadModel";

const SIGN_IN_POLL_MS = 2000;
const SIGN_IN_POLL_FAILURE_LIMIT = 5;
/** How often the list refreshes while another member's sign-in runs. */
const OTHERS_SIGN_IN_REFRESH_MS = 5000;
const SIGN_INS_HELP =
  "Personal sign-ins are yours: only your agents use them. Shared sign-ins on this machine are open to every member's agents. Your agents try tray 1 first and use tray 2 when 1 has no usable sign-in; ⇄ swaps the order. A shared card names the member who signed it in. Members lists who has a personal sign-in.";

/** One recoverable failure: a red edge and one icon that offers the action again. */
type Failure = { message: string; retry: () => void };

export interface SignInsData {
  rows: ProviderLoginAccount[] | null;
  settings: ProviderLoginSettings | null;
  error: string | null;
  refresh: () => Promise<void>;
  /** Writes the setting at once; the drawn value moves first and returns on failure. */
  saveSetting: (
    provider: string,
    machineId: string,
    value: ProviderLoginSettingValue,
  ) => Promise<void>;
}

/**
 * Every login and the viewer's settings, loaded once for all machine cards.
 *
 * The login belongs to its machine, so the Settings page asks once and each
 * card draws the rows for its own machine. Nothing here ever holds a credential.
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

  // Writes for one provider and machine run one after another; the last one to
  // settle decides what is shown, and a failure reloads the stored value.
  const settingWrites = useRef(new Map<string, Promise<unknown>>());
  const saveSetting = useCallback(
    async (provider: string, machineId: string, value: ProviderLoginSettingValue) => {
      const replace = (setting: ProviderLoginSettingValue) =>
        setSettings((current) =>
          current
            ? {
                ...current,
                settings: current.settings.map((item) =>
                  item.provider === provider && item.machine_id === machineId
                    ? { ...item, setting }
                    : item,
                ),
              }
            : current,
        );
      const key = `${provider}:${machineId}`;
      const write = (settingWrites.current.get(key) ?? Promise.resolve())
        .catch(() => undefined)
        .then(() => saveProviderLoginSetting(provider, machineId, value));
      settingWrites.current.set(key, write);
      replace(value);
      try {
        const saved = await write;
        if (settingWrites.current.get(key) === write) replace(saved.setting);
      } catch (failure) {
        // The stored value is back on screen before the failure shows.
        if (settingWrites.current.get(key) === write) await refresh();
        throw failure;
      }
    },
    [refresh],
  );
  return { rows, settings, error, refresh, saveSetting };
}

interface FoldProps {
  machineName: string;
  machineId: string;
  data: SignInsData;
  viewerUserId: string | null;
  writesDisabled: boolean;
  /** Open this fold at the named provider's panel, as a notice asks. */
  focusProvider?: string | null;
  onLoginChanged?: () => void;
}

/** A machine card's "Sign-ins" fold: one panel per provider on this machine. */
export function SignInsFold({
  machineName,
  machineId,
  data,
  viewerUserId,
  writesDisabled,
  focusProvider = null,
  onLoginChanged,
}: FoldProps) {
  const entries = data.rows ? signInsForMachine(data.rows, machineId, viewerUserId) : [];
  const { refresh } = data;
  const changed = useCallback(async () => {
    await refresh();
    onLoginChanged?.();
  }, [refresh, onLoginChanged]);
  return (
    <Fold
      className={`machine-sign-ins${data.error ? " failed" : ""}`}
      title={
        <span className="sign-ins-title">
          Sign-ins
          <CircleHelp size={13} className="sign-ins-help" role="img" aria-label={SIGN_INS_HELP}>
            <title>{SIGN_INS_HELP}</title>
          </CircleHelp>
        </span>
      }
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
            machineId={machineId}
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
  machineId,
  machineName,
  data,
  viewerUserId,
  writesDisabled,
  focused,
  onChanged,
}: {
  entry: ProviderSignIns;
  machineId: string;
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
          name: row.signed_in_by_display_name ?? "Shared sign-in",
          nameHover: row.signed_in_by_display_name
            ? `Signed in by ${row.signed_in_by_display_name}. The built-in ${label} login on ${machineName}.`
            : `The built-in ${label} login on ${machineName}.`,
          actions: entry.managed,
        })
      : card(row, {
          name: row.signed_in_by_display_name ?? row.added_by_display_name ?? "",
          nameHover: row.added_at
            ? `${row.signed_in_by_display_name ? `Signed in by ${row.signed_in_by_display_name}. ` : ""}Added by ${row.added_by_display_name ?? ""} on ${formatServerTimestamp(row.added_at)}.`
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
      const added = await addProviderLogin(entry.provider, machineId, tray);
      const methods = rows.find((row) => row.provider === entry.provider)?.sign_in_methods ?? [];
      if (!added.sign_in && methods.includes("token_entry")) setEntering(added.login_id);
      await onChanged();
    } catch (cause) {
      // The login may exist even though its sign-in did not start; show it so
      // its own card offers Sign in, instead of adding another one.
      const reload = () => {
        setFailure(null);
        void onChanged();
      };
      setFailure({ message: errorMessage(cause), retry: reload });
      void onChanged();
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

  const stored = data.settings?.settings.find(
    (item) => item.provider === entry.provider && item.machine_id === machineId,
  );
  const setting = stored?.setting ?? "own_then_shared";
  const ownFirst = setting === "own_then_shared";
  const settingsReady = Boolean(stored) && !writesDisabled;
  const write = (value: ProviderLoginSettingValue) => {
    setFailure(null);
    data
      .saveSetting(entry.provider, machineId, value)
      // The setting decides which login a launch uses, so readiness follows it.
      .then(onChanged)
      .catch((cause: unknown) =>
        setFailure({ message: errorMessage(cause), retry: () => write(value) }),
      );
  };
  const ownTray = (
    <SignInTray
      key="own"
      tray="own"
      rank={ownFirst ? 1 : 2}
      title="Personal sign-ins"
      hover="Only you can use these sign-ins."
    >
      {entry.own ? card(entry.own) : addSlot("own", `Add your own ${label} sign-in`)}
    </SignInTray>
  );
  const sharedTray = (
    <SignInTray
      key="shared"
      tray="shared"
      rank={ownFirst ? 2 : 1}
      title={`Shared on ${machineName}`}
      hover="Any member can use, add or remove these."
    >
      {sharedCards}
      {addSlot("shared", `Add a shared ${label} sign-in`)}
    </SignInTray>
  );
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
        {ownFirst ? ownTray : sharedTray}
        <button
          className="sign-ins-swap"
          type="button"
          data-sign-ins-swap=""
          aria-label="Swap sign-in order"
          title={ownFirst ? "Swap: try shared first" : "Swap: try personal first"}
          disabled={!settingsReady}
          onClick={() => write(ownFirst ? "shared_then_own" : "own_then_shared")}
        >
          <ArrowLeftRight size={15} className="sign-ins-swap-wide" aria-hidden="true" />
          <ArrowUpDown size={15} className="sign-ins-swap-narrow" aria-hidden="true" />
        </button>
        {ownFirst ? sharedTray : ownTray}
      </div>
      {members.of > 0 ? (
        <Fold
          className="sign-ins-members"
          title={<span title="Other members' own sign-ins.">Members</span>}
          status={`${members.signedIn} of ${members.of}`}
        >
          <div className="sign-ins-cards">
            {entry.members.map((row) =>
              card(row, { name: row.owner_display_name ?? "", actions: false }),
            )}
          </div>
        </Fold>
      ) : null}
    </section>
  );
}

interface TrayProps {
  tray: Tray;
  rank: 1 | 2;
  title: string;
  hover: string;
  children: ReactNode;
}

function SignInTray({ tray, rank, title, hover, children }: TrayProps) {
  return (
    <section className="sign-ins-tray" data-tray={tray} data-rank={rank}>
      <header>
        <span
          className="sign-ins-rank"
          title={rank === 1 ? "Tried first" : "Tried when the first has no usable sign-in"}
        >
          {rank}
        </span>
        <strong title={hover}>{title}</strong>
      </header>
      <div className="sign-ins-cards">{children}</div>
    </section>
  );
}

interface CardProps {
  row: ProviderLoginAccount;
  viewerUserId: string | null;
  writesDisabled: boolean;
  /** Shown before the state on shared and members' cards; the personal card has none. */
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
        // A restart settles every attempt, so an unknown id is final, not transient.
        const gone = cause instanceof ApiError && cause.status === 404;
        if (!gone) {
          setFailure({
            message: errorMessage(cause),
            retry: () => {
              setFailure(null);
              void onChangedRef.current();
            },
          });
        }
        // A transient status failure must not leave the card disabled forever.
        failures += 1;
        if (!gone && failures < SIGN_IN_POLL_FAILURE_LIMIT) {
          pollTimer.current = setTimeout(() => void poll(), SIGN_IN_POLL_MS);
        } else {
          // Re-read the list first, or its stale row hands the attempt back.
          await onChangedRef.current();
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
    try {
      await action();
      await onChanged();
    } catch (cause) {
      setFailure({ message: errorMessage(cause), retry: () => void run(kind, action) });
      // Another member may have changed this login; nothing else re-reads an idle list.
      void onChanged();
    } finally {
      setBusy(null);
    }
  }
  const { provider, machine_id: machineId, login_id: loginId } = row;
  const startSignIn = () => {
    if (row.sign_in_methods.some((method) => method === "device_code" || method === "paste_code")) {
      void run("sign-in", async () =>
        setSignIn(await startProviderSignIn(provider, machineId, loginId)),
      );
    } else if (row.sign_in_methods.includes("token_entry")) {
      onEntering(true);
    }
  };

  async function submitCode() {
    const code = token.trim();
    if (!code || !signIn || busy !== null) return;
    setToken("");
    setBusy("code");
    setFailure(null);
    try {
      setSignIn(await submitProviderSignInCode(provider, signIn.attempt_id, code));
    } catch (cause) {
      setFailure({ message: errorMessage(cause), retry: () => setFailure(null) });
    } finally {
      setBusy(null);
    }
  }

  const shown = { ...row, sign_in: signIn };
  const state = cardState(shown);
  const disabled = writesDisabled || busy !== null;
  // A failed device sign-in is the card's failure too, until the login signs in.
  const failed =
    failure ??
    (signIn?.state === "failed" && starter && row.state !== "signed_in"
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
      act: () => void run("verify", () => verifyProviderLogin(provider, machineId, loginId)),
    },
    (row.state === "signed_in" || row.token) && {
      id: "sign-out",
      label: "Sign out",
      act: () => void run("sign-out", () => signOutProvider(provider, machineId, loginId)),
    },
  ].filter((item) => !!item);
  // A running sign-in, token entry or a removal has nothing else to offer until it ends.
  const hasMenu =
    actions &&
    row.managed &&
    !entering &&
    state !== "draining" &&
    state !== "signing_in" &&
    (items.length > 0 || removable);

  const cancelSignIn = signIn ? (
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
  ) : null;

  let body: ReactNode;
  if (state === "draining") {
    body = (
      <span className="sign-in-state" title={`${row.bound_running} running`}>
        Being removed
      </span>
    );
  } else if (state === "signing_in" && signIn) {
    body = showsPasteCode(signIn, viewerUserId) ? (
      <form
        className="sign-in-token sign-in-device"
        onSubmit={(event) => {
          event.preventDefault();
          void submitCode();
        }}
      >
        <a
          className="button compact"
          href={signIn.verification_url!}
          target="_blank"
          rel="noreferrer"
          title={signIn.verification_url!}
          aria-label="Open sign-in"
        >
          <ExternalLink size={14} />
          <span className="sign-in-open-label">Open sign-in</span>
        </a>
        <input
          type="password"
          autoComplete="off"
          autoFocus
          aria-label={`${row.label} code`}
          placeholder="Paste the code"
          value={token}
          disabled={disabled}
          onChange={(event) => setToken(event.target.value)}
        />
        <button
          className="icon-button compact"
          type="submit"
          aria-label="Submit code"
          title="Submit code"
          disabled={disabled || !token.trim()}
        >
          {busy === "code" ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
        </button>
        {cancelSignIn}
      </form>
    ) : showsDeviceCode(signIn, viewerUserId) ? (
      <span className="sign-in-device" role="status">
        <a
          className="button compact"
          href={signIn.verification_url!}
          target="_blank"
          rel="noreferrer"
          title={signIn.verification_url!}
          aria-label="Open sign-in"
        >
          <ExternalLink size={14} />
          <span className="sign-in-open-label">Open sign-in</span>
        </a>
        <code>{signIn.user_code}</code>
        {cancelSignIn}
      </span>
    ) : starter ? (
      <span className="sign-in-device" role="status">
        <LoaderCircle size={14} className="spin sign-in-state" aria-label="Signing in" />
        {cancelSignIn}
      </span>
    ) : (
      <>
        <span className="sign-in-state">
          {signIn.started_by_display_name ?? "A member"} is signing in
        </span>
        {actions && cancelSignIn}
      </>
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
              await saveProviderToken(provider, machineId, loginId, pasted);
              setToken("");
              onEntering(false);
            });
          }}
        >
          <span className="sign-in-token-field">
            <input
              type="password"
              autoComplete="off"
              autoFocus
              aria-label={`${row.label} token`}
              placeholder="Paste the token"
              value={token}
              disabled={disabled}
              onChange={(event) => setToken(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") onEntering(false);
              }}
            />
            {row.token_instructions ? (
              <Info
                size={14}
                className="sign-in-token-info"
                role="img"
                aria-label={row.token_instructions}
              >
                <title>{row.token_instructions}</title>
              </Info>
            ) : null}
          </span>
          <button
            className="icon-button compact"
            type="submit"
            aria-label="Save token"
            title="Save token"
            disabled={disabled || !token.trim()}
          >
            {busy === "token" ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
          </button>
          <button
            className="icon-button compact"
            type="button"
            data-sign-in-action="cancel-token"
            aria-label="Cancel"
            title="Cancel"
            onClick={() => onEntering(false)}
          >
            <X size={14} />
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
        {row.limit_resets_at && row.limit_reset_estimated === false
          ? `Limit until ${askedLabel(row.limit_resets_at, new Date())}`
          : "Usage limit"}
      </span>
    );
  } else {
    const checkedAt = row.token?.verified_at ?? row.changed_at;
    body = row.managed ? (
      <span
        className="sign-in-state"
        title={checkedAt ? `Signed in · checked ${formatServerTimestamp(checkedAt)}` : "Signed in"}
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
        busy === "verify" || busy === "sign-out" || busy === "remove" ? (
          <span className="sign-in-more">
            <LoaderCircle size={14} className="spin" />
          </span>
        ) : (
          <OverflowMenu
            label="More"
            items={[]}
            className="sign-in-more"
            triggerClassName="icon-button compact"
            disabled={disabled}
          >
            <SignInMenuRows
              items={items}
              removable={removable}
              running={row.bound_running}
              onRemove={() =>
                void run("remove", () => removeProviderLogin(provider, machineId, loginId))
              }
            />
          </OverflowMenu>
        )
      ) : null}
    </article>
  );
}

type MenuItem = { id: string; label: string; act: () => void };

/** The ⋯ rows. Remove asks once more, with the running count; reopening starts over. */
function SignInMenuRows({
  items,
  removable,
  running,
  onRemove,
}: {
  items: MenuItem[];
  removable: boolean;
  running: number;
  onRemove: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  if (confirming) {
    return (
      <button
        type="button"
        role="menuitem"
        className="danger"
        data-sign-in-action="confirm-remove"
        autoFocus
        onClick={onRemove}
      >
        <Trash2 size={14} aria-hidden="true" />
        Remove from shared
        <span className="tag" data-bound-running={running}>
          {running} running
        </span>
      </button>
    );
  }
  const rows = removable
    ? [...items, { id: "remove", label: "Remove from shared", act: () => setConfirming(true) }]
    : items;
  return rows.map((item) => (
    <button
      key={item.id}
      type="button"
      role="menuitem"
      data-sign-in-action={item.id}
      onClick={item.act}
    >
      {item.label}
    </button>
  ));
}
