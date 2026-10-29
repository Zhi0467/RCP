import { KeyRound, LogIn } from "lucide-react";
import { Banner } from "../ui/Banner";
import {
  loginNoticeDismissKey,
  providerLabel,
  signOutKey,
  waitsOnOtherMember,
} from "./providerLoginsModel";
import type { ProviderLoginState } from "../core/types";

interface Props {
  states: ProviderLoginState[];
  viewerUserId?: string | null;
  /** Opens Settings at this login's machine card "Sign-ins" fold. */
  onOpenSignIns?: (state: ProviderLoginState) => void;
}

/**
 * One compact row per signed-out login: a state mark and one action that opens
 * its sign-ins. Work waiting on another member's own login names them instead.
 */
export function ProviderLoginNotice({ states, viewerUserId = null, onOpenSignIns }: Props) {
  const seen = new Set<string>();
  return (
    <>
      {states
        .filter((state) => state.state === "signed_out")
        .filter((state) => {
          const key = signOutKey(state);
          if (seen.has(key)) return false;
          seen.add(key);
          return true;
        })
        .map((state) => {
          const other = waitsOnOtherMember(state, viewerUserId);
          const label = providerLabel(state);
          return (
            <Banner
              key={signOutKey(state)}
              dismissKey={loginNoticeDismissKey(state)}
              className="provider-login-notice"
              role="status"
              data-login-notice={other ? "other-member" : "open"}
            >
              <KeyRound size={14} aria-hidden="true" className="provider-login-notice-mark" />
              <strong>{label}</strong>
              {state.host ? <span>{state.host}</span> : null}
              {other ? (
                <span className="provider-login-notice-owner">{state.owner_display_name}</span>
              ) : onOpenSignIns ? (
                <button
                  className="icon-button compact"
                  type="button"
                  data-login-notice-action="open-sign-ins"
                  aria-label={`Sign in ${label}`}
                  title="Sign-ins"
                  onClick={() => onOpenSignIns(state)}
                >
                  <LogIn size={14} />
                </button>
              ) : null}
            </Banner>
          );
        })}
    </>
  );
}
