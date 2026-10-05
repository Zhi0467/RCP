import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  exchangeTeamSession,
  pairTeamDevice as pairTeamDeviceRequest,
  pinApiInstance,
  registerIdentityNameRequiredHandler,
  registerMutationFailureHandler,
  registerTransportFailureHandler,
} from "../core/api";
import {
  BACKEND_IDENTITY_EVENT,
  isDesktopRuntime,
  signInDesktopOwner,
  establishBackendIdentity,
  recoverTeamTransport,
  reverifyBackendIdentity,
  TEAM_TRANSPORT_RECOVERED,
  verifyIdentityAfterMutationFailure,
  type BackendIdentityEventDetail,
} from "../core/desktopRuntime";
import type { Health, PublicHealth, IdentityResponse } from "../core/types";

export function useActorIdentity() {
  const [identityReady, setIdentityReady] = useState(false);
  const [identityIssue, setIdentityIssue] = useState<string | null>(null);
  const [verifiedHealth, setVerifiedHealth] = useState<PublicHealth | Health | null>(null);
  const [authenticatedHealth, setAuthenticatedHealth] = useState<Health | null>(null);
  const [actorIdentity, setActorIdentity] = useState<IdentityResponse | null>(null);
  const [actorIdentityError, setActorIdentityError] = useState<string | null>(null);
  const [actorIdentityChecked, setActorIdentityChecked] = useState(false);
  const [identityRetry, setIdentityRetry] = useState(0);
  const [ownerSessionRequired, setOwnerSessionRequired] = useState(false);
  const [teamSessionRequired, setTeamSessionRequired] = useState(false);
  const [actorNamePromptOpen, setActorNamePromptOpen] = useState(false);
  const [actorNameDraft, setActorNameDraft] = useState("");
  const [actorNameSaving, setActorNameSaving] = useState(false);
  const [actorNameError, setActorNameError] = useState<string | null>(null);

  const actorIdentityRef = useRef<IdentityResponse | null>(null);
  const actorNamePromptResolver = useRef<((saved: boolean) => void) | null>(null);
  const verifiedHealthRef = useRef<PublicHealth | Health | null>(null);
  actorIdentityRef.current = actorIdentity;

  const requestActorName = useCallback((): Promise<boolean> => {
    if (actorNamePromptResolver.current) return Promise.resolve(false);
    setActorNameDraft(actorIdentityRef.current?.user.display_name ?? "");
    setActorNameError(null);
    setActorNamePromptOpen(true);
    return new Promise((resolve) => {
      actorNamePromptResolver.current = resolve;
    });
  }, []);

  const settleActorNamePrompt = useCallback((saved: boolean) => {
    const resolve = actorNamePromptResolver.current;
    actorNamePromptResolver.current = null;
    setActorNamePromptOpen(false);
    setActorNameSaving(false);
    setActorNameError(null);
    resolve?.(saved);
  }, []);

  const saveActorName = useCallback(async () => {
    const displayName = actorNameDraft.trim();
    if (!displayName || actorNameSaving) return;
    setActorNameSaving(true);
    setActorNameError(null);
    try {
      const saved = await api<IdentityResponse>("/api/identity", {
        method: "PATCH",
        body: JSON.stringify({ display_name: displayName }),
      });
      setActorIdentity(saved);
      setActorIdentityError(null);
      settleActorNamePrompt(true);
    } catch (error) {
      setActorNameError(error instanceof Error ? error.message : String(error));
      setActorNameSaving(false);
    }
  }, [actorNameDraft, actorNameSaving, settleActorNamePrompt]);

  useEffect(() => {
    const onIdentity = (event: Event) => {
      const detail = (event as CustomEvent<BackendIdentityEventDetail>).detail;
      setIdentityReady(true);
      setIdentityIssue(detail.ok ? null : detail.message || "RCP could not verify its backend.");
      if (detail.health) {
        if (verifiedHealthRef.current?.instance_id !== detail.health.instance_id) {
          setAuthenticatedHealth(null);
        }
        const health =
          verifiedHealthRef.current?.instance_id === detail.health.instance_id
            ? { ...verifiedHealthRef.current, ...detail.health }
            : detail.health;
        verifiedHealthRef.current = health;
        setVerifiedHealth(health);
        if (detail.ok) pinApiInstance(detail.health.instance_id);
      }
      // An identity read the dropped tunnel failed is not retried by anything else.
      if (detail.ok && detail.reason === TEAM_TRANSPORT_RECOVERED && !actorIdentityRef.current) {
        setIdentityRetry((count) => count + 1);
      }
    };
    window.addEventListener(BACKEND_IDENTITY_EVENT, onIdentity);
    registerMutationFailureHandler(verifyIdentityAfterMutationFailure);
    registerTransportFailureHandler(() => {
      if (verifiedHealthRef.current?.space_kind === "team") void recoverTeamTransport();
    });
    void establishBackendIdentity();
    return () => {
      registerMutationFailureHandler(null);
      registerTransportFailureHandler(null);
      window.removeEventListener(BACKEND_IDENTITY_EVENT, onIdentity);
    };
  }, []);

  useEffect(() => {
    registerIdentityNameRequiredHandler(requestActorName);
    return () => {
      registerIdentityNameRequiredHandler(null);
      const resolve = actorNamePromptResolver.current;
      actorNamePromptResolver.current = null;
      resolve?.(false);
    };
  }, [requestActorName]);

  useEffect(() => {
    if (!identityReady || identityIssue || !verifiedHealth) return;
    let stopped = false;
    setActorIdentityChecked(false);
    setTeamSessionRequired(false);
    setActorIdentityError(null);
    void api<IdentityResponse>("/api/identity")
      .then(async (identity) => {
        const details = await api<Health>("/api/health/details");
        if (stopped) return;
        verifiedHealthRef.current = details;
        setVerifiedHealth(details);
        setAuthenticatedHealth(details);
        setOwnerSessionRequired(false);
        setActorIdentity(identity);
        setActorIdentityChecked(true);
      })
      .catch((error) => {
        if (stopped) return;
        setActorIdentity(null);
        setAuthenticatedHealth(null);
        if (
          verifiedHealth.space_kind === "team" &&
          error instanceof ApiError &&
          error.status === 401
        ) {
          setTeamSessionRequired(true);
        } else if (error instanceof ApiError && error.status === 401) {
          setOwnerSessionRequired(true);
        } else {
          setActorIdentityError(error instanceof Error ? error.message : String(error));
        }
        setActorIdentityChecked(true);
      });
    return () => {
      stopped = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by backend identity; the effect sets verifiedHealth itself, so the object would loop
  }, [
    identityIssue,
    identityReady,
    identityRetry,
    verifiedHealth?.instance_id,
    verifiedHealth?.space_id,
    verifiedHealth?.space_kind,
  ]);

  useEffect(() => {
    const expired = () => {
      if (verifiedHealthRef.current?.space_kind === "team") return;
      setOwnerSessionRequired(true);
      setActorIdentity(null);
      setAuthenticatedHealth(null);
    };
    window.addEventListener("rcp:session-required", expired);
    return () => window.removeEventListener("rcp:session-required", expired);
  }, []);

  const authenticateOwnerSession = useCallback(async (code: string) => {
    if (isDesktopRuntime()) await signInDesktopOwner(code);
    else await api("/api/owner/redeem", { method: "POST", body: JSON.stringify({ code }) });
    setOwnerSessionRequired(false);
    setActorIdentityChecked(false);
    setIdentityRetry((count) => count + 1);
  }, []);

  const adoptTeamIdentity = useCallback(async (identity: IdentityResponse) => {
    const details = await api<Health>("/api/health/details");
    verifiedHealthRef.current = details;
    setVerifiedHealth(details);
    setAuthenticatedHealth(details);
    setActorIdentity(identity);
    setActorIdentityError(null);
    setActorIdentityChecked(true);
    setTeamSessionRequired(false);
  }, []);

  const authenticateTeamSession = useCallback(
    async (token: string) => adoptTeamIdentity(await exchangeTeamSession(token)),
    [adoptTeamIdentity],
  );

  const pairTeamDevice = useCallback(
    async (code: string, label: string) =>
      adoptTeamIdentity(await pairTeamDeviceRequest(code, label)),
    [adoptTeamIdentity],
  );

  const reportIdentityIssue = useCallback((message: string) => {
    setIdentityIssue(message);
  }, []);

  const reverifyIdentity = useCallback((reason: string) => reverifyBackendIdentity(reason), []);

  const currentActiveAgentTasks = useCallback(() => {
    const health = verifiedHealthRef.current;
    return health && "active_agent_tasks" in health ? health.active_agent_tasks : 0;
  }, []);

  const updateActorNameDraft = useCallback((value: string) => {
    setActorNameDraft(value);
    setActorNameError(null);
  }, []);

  return {
    identityReady,
    identityIssue,
    verifiedHealth,
    authenticatedHealth,
    actorIdentity,
    actorIdentityError,
    actorIdentityChecked,
    teamSessionRequired,
    ownerSessionRequired,
    authenticateOwnerSession,
    actorNamePromptOpen,
    actorNameDraft,
    actorNameSaving,
    actorNameError,
    requestActorName,
    settleActorNamePrompt,
    saveActorName,
    authenticateTeamSession,
    pairTeamDevice,
    reportIdentityIssue,
    reverifyIdentity,
    currentActiveAgentTasks,
    updateActorNameDraft,
  };
}
