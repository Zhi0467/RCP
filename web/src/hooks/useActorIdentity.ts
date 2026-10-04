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
} from "../api";
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
} from "../desktopRuntime";
import type { Health, IdentityResponse } from "../types";

export function useActorIdentity() {
  const [identityReady, setIdentityReady] = useState(false);
  const [identityIssue, setIdentityIssue] = useState<string | null>(null);
  const [verifiedHealth, setVerifiedHealth] = useState<Health | null>(null);
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
  const verifiedHealthRef = useRef<Health | null>(null);
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
        setOwnerSessionRequired(false);
        setActorIdentity(identity);
        setActorIdentityChecked(true);
      })
      .catch((error) => {
        if (stopped) return;
        setActorIdentity(null);
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
  }, [
    identityIssue,
    identityReady,
    identityRetry,
    verifiedHealth?.space_id,
    verifiedHealth?.space_kind,
  ]);

  useEffect(() => {
    const expired = () => {
      if (verifiedHealthRef.current?.space_kind === "team") return;
      setOwnerSessionRequired(true);
      setActorIdentity(null);
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

  const currentActiveAgentTasks = useCallback(
    () => verifiedHealthRef.current?.active_agent_tasks ?? 0,
    [],
  );

  const updateActorNameDraft = useCallback((value: string) => {
    setActorNameDraft(value);
    setActorNameError(null);
  }, []);

  return {
    identityReady,
    identityIssue,
    verifiedHealth,
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
