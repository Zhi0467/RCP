import { useEffect, useRef, useState } from "react";
import { Maximize2 } from "lucide-react";
import { errorMessage } from "../core/errors";
import { useResource } from "../core/useResource";
import {
  artifactReferenceId,
  repositoryFileHrefTarget,
  repositoryFilePreviewUrl,
  resolveRepositoryFileHref,
  type RepositoryFileTarget,
} from "../core/repositoryFileLinks";
import type { AgentArtifactDescriptor, ProjectSnapshot } from "../core/types";
import { InlineArtifact } from "../artifacts/InlineArtifact";
import { artifactStateProfile } from "../artifacts/artifactResources";
import { openArtifact, openRepositoryFile } from "../artifacts/artifactViewerModel";
import { INLINE_ARTIFACT_HEIGHT } from "../artifacts/inlineArtifacts";
import type { AgentWindowEndpoints } from "./agentWindowEndpoints";

/** An original a reply references, resolved to the project that holds it. */
export type ResolvedReference =
  | { kind: "file"; projectId: string; target: RepositoryFileTarget }
  | { kind: "artifact"; projectId: string; artifactId: string }
  | { kind: "unresolved"; reason: string };

/**
 * Where a repository path or `artifact:ID` lives. A project window holds its own
 * repositories and project; a Space window asks its resolver, which reads only
 * the member's projects. The original is then read through that project's routes.
 */
export async function resolveReference(
  href: string,
  endpoints: AgentWindowEndpoints,
  repositories: ProjectSnapshot["repositories"],
): Promise<ResolvedReference> {
  const artifactId = artifactReferenceId(href);
  const target = artifactId ? null : repositoryFileHrefTarget(href);
  if (endpoints.projectId) {
    if (artifactId) return { kind: "artifact", projectId: endpoints.projectId, artifactId };
    const resolution = resolveRepositoryFileHref(href, repositories);
    return resolution.kind === "error"
      ? { kind: "unresolved", reason: resolution.message }
      : { kind: "file", projectId: endpoints.projectId, target: resolution.target };
  }
  if (!endpoints.referenceProject || !(artifactId || target))
    return { kind: "unresolved", reason: "Not a repository file or saved artifact." };
  try {
    const projectId = await endpoints.referenceProject(
      artifactId ? { artifact_id: artifactId } : { path: target!.path },
    );
    return artifactId
      ? { kind: "artifact", projectId, artifactId }
      : { kind: "file", projectId, target: target! };
  } catch (failure) {
    return { kind: "unresolved", reason: errorMessage(failure) };
  }
}

/** A repository file or saved artifact shown in place where the reply embeds it. */
export function ReferenceEmbed({
  href,
  title,
  resolve,
  refreshToken,
}: {
  href: string;
  title: string;
  resolve: (href: string) => Promise<ResolvedReference>;
  refreshToken: string;
}) {
  const [found, setFound] = useState<{ href: string; value: ResolvedReference } | null>(null);
  const resolveRef = useRef(resolve);
  resolveRef.current = resolve;
  useEffect(() => {
    let current = true;
    void resolveRef.current(href).then((value) => current && setFound({ href, value }));
    return () => {
      current = false;
    };
  }, [href, resolveRef]);
  const name = title.trim() || href.split("/").at(-1) || href;
  const shown = found?.href === href ? found.value : null;
  if (!shown) return <Placeholder name={name} />;
  if (shown.kind === "unresolved")
    return <UnresolvedReference href={href} name={name} reason={shown.reason} />;
  if (shown.kind === "file")
    return <RepositoryFileEmbed projectId={shown.projectId} target={shown.target} name={name} />;
  return (
    <SavedArtifactEmbed
      href={href}
      projectId={shown.projectId}
      artifactId={shown.artifactId}
      title={title.trim()}
      refreshToken={refreshToken}
    />
  );
}

function Placeholder({ name }: { name: string }) {
  return (
    <span className="chat-inline-artifact">
      <span
        className="chat-inline-artifact-placeholder"
        style={{ height: INLINE_ARTIFACT_HEIGHT }}
        role="status"
        aria-label={`Loading ${name}`}
      />
      <span className="chat-inline-artifact-bar">
        <span className="chat-inline-artifact-name">{name}</span>
      </span>
    </span>
  );
}

/** A reference RCP cannot show stays readable as a plain link that says why on hover. */
function UnresolvedReference({
  href,
  name,
  reason,
}: {
  href: string;
  name: string;
  reason: string;
}) {
  return (
    <a
      href={href}
      title={reason}
      aria-disabled="true"
      data-reference="unresolved"
      onClick={(event) => event.preventDefault()}
    >
      {name}
    </a>
  );
}

/** The repository preview route's own page, framed like an artifact in the reply. */
function RepositoryFileEmbed({
  projectId,
  target,
  name,
}: {
  projectId: string;
  target: RepositoryFileTarget;
  name: string;
}) {
  const url = repositoryFilePreviewUrl(projectId, target);
  // The route's HEAD validates the same window GET renders.
  const [checked, setChecked] = useState<{ url: string; problem: string | null } | null>(null);
  useEffect(() => {
    let current = true;
    fetch(url, { method: "HEAD", credentials: "same-origin" }).then(
      (response) =>
        current &&
        setChecked({
          url,
          problem: response.ok ? null : `This file cannot be shown (${response.status}).`,
        }),
      (failure: unknown) => current && setChecked({ url, problem: errorMessage(failure) }),
    );
    return () => {
      current = false;
    };
  }, [url]);
  if (checked?.url !== url) return <Placeholder name={name} />;
  if (checked.problem)
    return <UnresolvedReference href={target.path} name={name} reason={checked.problem} />;
  return (
    <span className="chat-inline-artifact text" data-reference="file">
      <span className="chat-inline-artifact-box" style={{ height: INLINE_ARTIFACT_HEIGHT }}>
        <iframe className="chat-inline-artifact-frame" src={url} title={name} sandbox="" />
      </span>
      <span className="chat-inline-artifact-bar">
        <span className="chat-inline-artifact-name" title={target.path}>
          {name}
        </span>
        <button
          type="button"
          onClick={() => openRepositoryFile({ projectId, path: target.path, line: target.line })}
          title="Open in the viewer"
          aria-label="Expand"
        >
          <Maximize2 size={12} />
          <span className="chat-inline-artifact-label">Expand</span>
        </button>
      </span>
    </span>
  );
}

/** A saved artifact read through its project's owner-checked state and viewer routes. */
function SavedArtifactEmbed({
  href,
  projectId,
  artifactId,
  title,
  refreshToken,
}: {
  href: string;
  projectId: string;
  artifactId: string;
  title: string;
  refreshToken: string;
}) {
  const profile = artifactStateProfile(projectId, artifactId);
  const state = useResource(`${projectId}:reference:${artifactId}`, profile);
  if (state.error)
    return (
      <UnresolvedReference href={href} name={title || href} reason={errorMessage(state.error)} />
    );
  if (!state.value) return <Placeholder name={title || artifactId} />;
  const descriptor: AgentArtifactDescriptor = {
    artifact_id: artifactId,
    name: state.value.name,
    media_type: state.value.media_type,
    view: state.value.view,
    available: true,
    unavailable_reason: null,
    can_open: true,
    can_download: false,
    can_keep: false,
    can_discuss: false,
  };
  return (
    <InlineArtifact
      projectId={projectId}
      stateProfile={profile}
      artifact={descriptor}
      title={title || state.value.name}
      refreshToken={refreshToken}
      canComment={false}
      keeping={false}
      onExpand={() => openArtifact({ projectId, artifactId })}
      onKeep={null}
      download={null}
      onSelection={() => {}}
      onSelectionCancel={() => {}}
      onVersionChange={() => {}}
    />
  );
}
