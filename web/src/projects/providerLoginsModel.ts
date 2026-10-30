import type {
  ProviderLoginAccount,
  ProviderLoginSettingValue,
  ProviderLoginState,
} from "../core/types";

/** One of a member's two groups: their own login, or the machine's shared logins. */
export type Tray = "own" | "shared";

/** How one stored setting draws: tray order, switches, and what Swap would store. */
export interface TrayLayout {
  order: [Tray, Tray];
  on: Record<Tray, boolean>;
  /** The only group that is on; its switch cannot turn off. */
  locked: Tray | null;
  /** The value Swap stores; null when one group is on and Swap is disabled. */
  swap: ProviderLoginSettingValue | null;
}

const LAYOUTS: Record<ProviderLoginSettingValue, TrayLayout> = {
  own_then_shared: {
    order: ["own", "shared"],
    on: { own: true, shared: true },
    locked: null,
    swap: "shared_then_own",
  },
  shared_then_own: {
    order: ["shared", "own"],
    on: { own: true, shared: true },
    locked: null,
    swap: "own_then_shared",
  },
  own: { order: ["own", "shared"], on: { own: true, shared: false }, locked: "own", swap: null },
  shared: {
    order: ["shared", "own"],
    on: { own: false, shared: true },
    locked: "shared",
    swap: null,
  },
};

export function trayLayout(setting: ProviderLoginSettingValue): TrayLayout {
  return LAYOUTS[setting];
}

/**
 * The value a switch stores, or null when it would leave both groups off. Turning
 * the other group back on stores "the group that is on, then the other".
 */
export function switchedSetting(
  setting: ProviderLoginSettingValue,
  tray: Tray,
  on: boolean,
): ProviderLoginSettingValue | null {
  const layout = LAYOUTS[setting];
  if (layout.on[tray] === on) return setting;
  if (!on) return layout.locked === tray ? null : tray === "own" ? "shared" : "own";
  return tray === "own" ? "shared_then_own" : "own_then_shared";
}

export type CardState = "draining" | "signing_in" | "signed_out" | "limited" | "ready";

/** The one state a sign-in card shows: Draining, Signing in, Signed out, Limited, Ready. */
export function cardState(
  row: Pick<ProviderLoginAccount, "draining_since" | "sign_in" | "state" | "limited_at">,
): CardState {
  if (row.draining_since) return "draining";
  if (row.sign_in?.state === "pending") return "signing_in";
  if (row.state === "signed_out") return "signed_out";
  if (row.limited_at) return "limited";
  return "ready";
}

/** A device code is shown to the member who started the sign-in, never to anyone else. */
export function showsDeviceCode(
  signIn: ProviderLoginAccount["sign_in"],
  viewerUserId: string | null,
): boolean {
  return Boolean(
    signIn &&
    viewerUserId &&
    signIn.started_by === viewerUserId &&
    signIn.user_code &&
    signIn.verification_url,
  );
}

/** One provider's logins on one host, split the way the Sign-ins panel draws them. */
export interface ProviderSignIns {
  provider: string;
  label: string;
  managed: boolean;
  /** The viewer's own login. */
  own: ProviderLoginAccount | null;
  /** The machine sign-in first, then added shared logins in the order listed. */
  shared: ProviderLoginAccount[];
  /** Other members' own logins. */
  members: ProviderLoginAccount[];
}

export function signInsForHost(
  rows: readonly ProviderLoginAccount[],
  host: string,
  viewerUserId: string | null,
): ProviderSignIns[] {
  const byProvider = new Map<string, ProviderSignIns>();
  for (const row of rows) {
    if (row.host !== host) continue;
    let entry = byProvider.get(row.provider);
    if (!entry) {
      entry = {
        provider: row.provider,
        label: row.label,
        managed: row.managed,
        own: null,
        shared: [],
        members: [],
      };
      byProvider.set(row.provider, entry);
    }
    if (row.kind === "shared") entry.shared.push(row);
    else if (viewerUserId && row.owner_user_id === viewerUserId) entry.own = row;
    else entry.members.push(row);
  }
  for (const entry of byProvider.values()) {
    entry.shared.sort((a, b) => Number(b.login_id === "") - Number(a.login_id === ""));
  }
  return [...byProvider.values()];
}

/** Members row: n other members signed in on their own login, of m other members. */
export function membersSignedIn(
  members: readonly Pick<ProviderLoginState, "state">[],
  otherMemberCount: number,
): { signedIn: number; of: number } {
  return {
    signedIn: members.filter((row) => row.state === "signed_in").length,
    of: otherMemberCount,
  };
}

/** The notice row for another member's own login names them and offers nothing. */
export function waitsOnOtherMember(
  row: Pick<ProviderLoginState, "kind" | "owner_user_id">,
  viewerUserId: string | null,
): boolean {
  return row.kind === "own" && row.owner_user_id !== viewerUserId;
}

type LoginKey = Pick<ProviderLoginState, "provider" | "host"> & { login_id: string | null };

export const loginKey = (row: LoginKey) => `${row.provider}:${row.host}:${row.login_id}`;

export const sameLogin = (a: LoginKey, b: LoginKey) =>
  a.provider === b.provider && a.host === b.host && a.login_id === b.login_id;

type SignOut = LoginKey & Pick<ProviderLoginState, "generation">;

// One sign-out of one login. Only a sign-in or sign-out starts a new
// generation; another failure of the same sign-out keeps it, so a dismissal holds.
export const signOutKey = (state: SignOut) => `${loginKey(state)}:${state.generation}`;
export const loginNoticeDismissKey = (state: SignOut) =>
  `login-notice-dismissed:${signOutKey(state)}`;

/** A provider id as a person reads it when the row carries no label. */
export function providerLabel(row: Pick<ProviderLoginState, "label" | "provider">): string {
  const named = row.label ?? row.provider;
  return named.charAt(0).toUpperCase() + named.slice(1);
}
