import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type Dispatch,
  type DragEvent,
  type RefObject,
  type SetStateAction,
} from "react";
import { File, FolderOpen, LoaderCircle, Plus, Upload, X } from "lucide-react";
import { api, removeComposerAttachment, uploadComposerAttachment } from "../core/api";
import { MAIN_GRAPH } from "../core/graphTarget";
import {
  MAX_CHAT_ATTACHMENTS,
  extractReferences,
  labelArtifactReferences,
  mergeReferences,
  parseReferenceDraft,
  referenceFallbackLabel,
  referenceKey,
  unlabeledArtifactIds,
  type DraftReference,
} from "../core/projectReferences";
import { ReferenceChip } from "../core/ReferenceChip";
import type {
  ChatAttachmentDescriptor,
  ProjectArtifact,
  ProjectReferenceSelector,
  GraphNode,
  GraphTargetRef,
} from "../core/types";
import { readPref, writePref } from "../core/prefStorage";
import { useLayerDismiss } from "../core/useLayerDismiss";
import { ProjectReferencePicker } from "./ProjectReferencePicker";

/** A composer's files and project references for its next turn: chat and the Paper coach
 * share this whole flow, uploads included. Files upload into the scope's attachment set:
 * the chat's own id, or a draft scope this hook owns and renews after each admitted turn. */

/** The project context used by reference chips; threads need only the id. */
export interface ComposerProject {
  id: string;
  graph_target?: GraphTargetRef;
  graph?: { nodes: Record<string, GraphNode> } | null;
}

/** The attachment fields a turn request carries; the server claims them from the scope. */
export interface ComposerTurnFields {
  references?: ProjectReferenceSelector[];
  attachment_set_id?: string;
  attachment_client_id?: string;
  attachment_scope_id?: string;
}

type AttachmentStatus = "preparing" | "ready" | "error";

export interface ComposerAttachment {
  localId: string;
  file: File;
  status: AttachmentStatus;
  descriptor?: ChatAttachmentDescriptor;
  error?: string;
}

const TOO_MANY = `A turn can include at most ${MAX_CHAT_ATTACHMENTS} files and references.`;
const DRAGGED_TYPES = ["Files", "text/plain", "text/uri-list"];

const MAX_CHAT_ATTACHMENT_BYTES = 16 * 1024 * 1024;
const MAX_CHAT_ATTACHMENT_TOTAL_BYTES = 32 * 1024 * 1024;
const CHAT_ATTACHMENT_CLIENT_KEY = "rcp:chat-attachment-client";
const CHAT_ATTACHMENT_EXTENSIONS = new Set([
  "c",
  "cc",
  "cpp",
  "cs",
  "css",
  "csv",
  "fish",
  "go",
  "h",
  "hpp",
  "htm",
  "html",
  "java",
  "js",
  "json",
  "jsx",
  "kt",
  "kts",
  "lua",
  "markdown",
  "md",
  "mjs",
  "mm",
  "php",
  "py",
  "r",
  "rb",
  "rs",
  "scala",
  "sh",
  "sql",
  "svg",
  "swift",
  "toml",
  "ts",
  "tsv",
  "tsx",
  "txt",
  "xml",
  "yaml",
  "yml",
  "zsh",
]);
const CHAT_ATTACHMENT_BINARY_EXTENSIONS = new Set(["jpeg", "jpg", "pdf", "png", "webp"]);
export const CHAT_ATTACHMENT_ACCEPT = [
  ".txt",
  ".md",
  ".csv",
  ".tsv",
  ".json",
  ".html",
  ".htm",
  ".svg",
  ".pdf",
  ".png",
  ".jpg",
  ".jpeg",
  ".webp",
  ...[...CHAT_ATTACHMENT_EXTENSIONS].map((extension) => `.${extension}`),
].join(",");

function validateChatAttachment(file: File, currentTotal: number): string | null {
  const extension = file.name.split(".").at(-1)?.toLowerCase() ?? "";
  if (
    !CHAT_ATTACHMENT_BINARY_EXTENSIONS.has(extension) &&
    !CHAT_ATTACHMENT_EXTENSIONS.has(extension)
  ) {
    return "Unsupported file type";
  }
  if (file.size > MAX_CHAT_ATTACHMENT_BYTES) return "File exceeds 16 MiB";
  if (currentTotal + file.size > MAX_CHAT_ATTACHMENT_TOTAL_BYTES) {
    return "Turn exceeds 32 MiB total";
  }
  return null;
}

function chatAttachmentClientId(): string {
  try {
    const current = sessionStorage.getItem(CHAT_ATTACHMENT_CLIENT_KEY);
    if (current) return current;
    const created = crypto.randomUUID();
    sessionStorage.setItem(CHAT_ATTACHMENT_CLIENT_KEY, created);
    return created;
  } catch {
    return crypto.randomUUID();
  }
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(bytes < 10 * 1024 * 1024 ? 1 : 0)} MiB`;
}

function draftReferencesAt(key: string | null): DraftReference[] {
  return parseReferenceDraft(key ? readPref(key) : null);
}

/** Unsent references, kept for this viewer under `key`, with artifact names looked up. */
function useDraftReferences(key: string | null, apiBase: string | null) {
  const [references, setReferences] = useState<DraftReference[]>(() => draftReferencesAt(key));
  const identity = useRef(key);
  useEffect(() => {
    if (identity.current !== key) {
      identity.current = key;
      setReferences(draftReferencesAt(key));
      return;
    }
    if (key) writePref(key, references.length ? JSON.stringify(references) : null);
  }, [key, references]);
  const unlabeledArtifacts = unlabeledArtifactIds(references).join("\n");
  useEffect(() => {
    if (!unlabeledArtifacts || !apiBase) return;
    const controller = new AbortController();
    api<ProjectArtifact[]>(`${apiBase}/artifacts`, { signal: controller.signal })
      .then((artifacts) => setReferences((current) => labelArtifactReferences(current, artifacts)))
      // A lookup failure keeps the fallback label; the sent turn shows the server's name.
      .catch(() => undefined);
    return () => controller.abort();
  }, [apiBase, unlabeledArtifacts]);
  return [references, setReferences] as const;
}

/** New rows for `incoming`, each preparing or already refused, within the turn's byte budget. */
function validatedCandidates(
  incoming: File[],
  staged: readonly ComposerAttachment[],
): ComposerAttachment[] {
  let total = staged.reduce(
    (sum, item) => (item.status === "error" ? sum : sum + item.file.size),
    0,
  );
  return incoming.map((file) => {
    const error = validateChatAttachment(file, total);
    if (error) return { localId: crypto.randomUUID(), file, status: "error", error };
    total += file.size;
    return { localId: crypto.randomUUID(), file, status: "preparing" };
  });
}

/** A copy of the file's bytes now: a macOS screenshot thumbnail drops a file promise
 * whose bytes can be gone by upload time, so an unreadable source fails here, named. */
async function readAttachmentBytes(item: ComposerAttachment): Promise<ComposerAttachment> {
  try {
    const bytes = await item.file.arrayBuffer();
    if (bytes.byteLength !== item.file.size) throw new Error("short read");
    return {
      ...item,
      file: new globalThis.File([bytes], item.file.name, {
        type: item.file.type,
        lastModified: item.file.lastModified,
      }),
    };
  } catch {
    return {
      ...item,
      status: "error",
      error: "Could not read this file. Save it to disk first, then attach it.",
    };
  }
}

function replaceAttachment(
  list: ComposerAttachment[],
  localId: string,
  next: (item: ComposerAttachment) => ComposerAttachment,
): ComposerAttachment[] {
  return list.map((item) => (item.localId === localId ? next(item) : item));
}

/** Uploads read files in order into one attachment set; a failure before the set exists
 * fails the rest, and a row removed while uploading is deleted again once it lands. */
async function uploadPrepared(
  prepared: ComposerAttachment[],
  context: {
    attachmentsUrl: string;
    clientId: string;
    setIdRef: { current: string | null };
    cancelledIds: Set<string>;
    setAttachmentSetId: (id: string) => void;
    setAttachments: Dispatch<SetStateAction<ComposerAttachment[]>>;
    isCurrent: () => boolean;
  },
) {
  const { attachmentsUrl, clientId, setIdRef, cancelledIds, setAttachments } = context;
  for (const [index, item] of prepared.entries()) {
    if (!context.isCurrent()) break;
    try {
      const result = await uploadComposerAttachment(
        attachmentsUrl,
        item.file,
        clientId,
        setIdRef.current,
      );
      setIdRef.current = result.attachment_set_id;
      context.setAttachmentSetId(result.attachment_set_id);
      if (!context.isCurrent() || cancelledIds.delete(item.localId)) {
        await removeComposerAttachment(
          attachmentsUrl,
          result.attachment_set_id,
          result.attachment.attachment_id,
          clientId,
        );
        continue;
      }
      setAttachments((current) =>
        replaceAttachment(current, item.localId, (row) => ({
          ...row,
          status: "ready",
          descriptor: result.attachment,
        })),
      );
    } catch (error) {
      const cancelled = cancelledIds.delete(item.localId);
      const detail = error instanceof Error ? error.message : String(error);
      if (!cancelled) {
        setAttachments((current) =>
          replaceAttachment(current, item.localId, (row) => ({
            ...row,
            status: "error",
            error: detail,
          })),
        );
      }
      if (!setIdRef.current) {
        const remaining = new Set(prepared.slice(index + 1).map((candidate) => candidate.localId));
        setAttachments((current) =>
          current.map((candidate) =>
            remaining.has(candidate.localId)
              ? { ...candidate, status: "error", error: "Attachment set could not be created" }
              : candidate,
          ),
        );
        break;
      }
    }
  }
}

/** A node on the viewed graph shows its title; anything else its fallback label. */
function referenceLabel(project: ComposerProject, selector: ProjectReferenceSelector): string {
  const target = project.graph_target ?? MAIN_GRAPH;
  const viewedBranch = target.kind === "branch" ? target.branch_id : null;
  if (selector.kind !== "node" || (selector.branch_id ?? null) !== viewedBranch) {
    return referenceFallbackLabel(selector);
  }
  return project.graph?.nodes[selector.node_id]?.title ?? selector.node_id;
}

function carriesDroppable(event: DragEvent<HTMLElement>): boolean {
  return event.dataTransfer.types.some((type) => DRAGGED_TYPES.includes(type));
}

/** The next turn's attachment fields: what is staged, and the scope that holds it. */
function composerTurnFields(
  references: readonly DraftReference[],
  attachmentSetId: string | null,
  clientId: string,
  scope: string,
): ComposerTurnFields {
  if (!attachmentSetId && !references.length) return {};
  return {
    ...(references.length ? { references: references.map((item) => item.selector) } : {}),
    ...(attachmentSetId
      ? { attachment_set_id: attachmentSetId, attachment_client_id: clientId }
      : {}),
    attachment_scope_id: scope,
  };
}

export function useComposerAttachments({
  project,
  attachmentsUrl,
  scopeId,
  referencesKey,
  input,
  message,
  filesDisabled = false,
  referencesDisabled: disableReferences = false,
  onError,
}: {
  project?: ComposerProject | null;
  /** The owner-resolved collection; required when the composer has no project. */
  attachmentsUrl?: string;
  /** The chat whose attachment set the files join; omitted, each draft has its own. */
  scopeId?: string;
  /** Where unsent references persist for this viewer, or null to keep them in memory. */
  referencesKey: string | null;
  /** The message field that pasted and dropped text joins at its cursor. */
  input: RefObject<HTMLTextAreaElement | null>;
  message: { get: () => string; set: (next: string) => void };
  /** The composer takes no files now. */
  filesDisabled?: boolean;
  /** The composer takes no project references now. */
  referencesDisabled?: boolean;
  onError: (message: string | null) => void;
}) {
  const apiBase = project ? `/api/projects/${encodeURIComponent(project.id)}` : null;
  const referencesDisabled = disableReferences || !project;
  const [draftScope, setDraftScope] = useState(() => crypto.randomUUID());
  const scope = scopeId ?? draftScope;
  const uploadUrl =
    attachmentsUrl ??
    (apiBase ? `${apiBase}/chats/${encodeURIComponent(scope)}/attachments` : null);
  const [references, setReferences] = useDraftReferences(referencesKey, apiBase);
  // Upload callbacks retain their owner across awaits. A different conversation starts
  // empty immediately, and a late upload removes its file from the old collection.
  const owner = useMemo(
    () => ({
      scope,
      attachmentsUrl: uploadUrl,
      active: true,
      setIdRef: { current: null as string | null },
      cancelledIds: new Set<string>(),
      busy: false,
    }),
    [uploadUrl, scope],
  );
  const currentScope = useRef({ owner, referencesKey });
  currentScope.current = { owner, referencesKey };
  const [attachmentState, setAttachmentState] = useState({
    owner,
    items: [] as ComposerAttachment[],
    setId: null as string | null,
  });
  const attachments = attachmentState.owner === owner ? attachmentState.items : [];
  const attachmentSetId = attachmentState.owner === owner ? attachmentState.setId : null;
  const setAttachments: Dispatch<SetStateAction<ComposerAttachment[]>> = (update) => {
    if (!owner.active) return;
    setAttachmentState((current) => {
      if (!owner.active) return current;
      const items = current.owner === owner ? current.items : [];
      return {
        owner,
        items: typeof update === "function" ? update(items) : update,
        setId: current.owner === owner ? current.setId : null,
      };
    });
  };
  const setAttachmentSetId = (setId: string | null) => {
    if (!owner.active) return;
    setAttachmentState((current) =>
      owner.active
        ? { owner, items: current.owner === owner ? current.items : [], setId }
        : current,
    );
  };
  useEffect(() => {
    owner.active = true;
    return () => {
      owner.active = false;
    };
  }, [owner]);
  const [dragging, setDragging] = useState(false);
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const clientId = useMemo(() => chatAttachmentClientId(), []);
  // Another chat, or the next draft, starts with its Add menu closed.
  useEffect(() => {
    setAddMenuOpen(false);
    setPickerOpen(false);
  }, [scope]);
  const ready = attachments.flatMap((item) =>
    item.status === "ready" && item.descriptor ? [item.descriptor] : [],
  );
  const preparing = attachments.some((item) => item.status === "preparing");

  const addFiles = async (incoming: File[]) => {
    setDragging(false);
    if (filesDisabled) return;
    if (!uploadUrl) {
      onError("Attachments are unavailable for this conversation.");
      return;
    }
    if (owner.busy) {
      onError("Wait for the current files to finish preparing before adding more.");
      return;
    }
    owner.busy = true;
    onError(null);
    const available = Math.max(0, MAX_CHAT_ATTACHMENTS - attachments.length - references.length);
    if (incoming.length > available) onError(TOO_MANY);
    const candidates = validatedCandidates(incoming.slice(0, available), attachments);
    // Publish the preparing rows first: they block sending while the reads below run.
    setAttachments((current) => [...current, ...candidates]);
    const prepared: ComposerAttachment[] = [];
    for (const item of candidates) {
      if (item.status !== "preparing") continue;
      const next = await readAttachmentBytes(item);
      if (next.status === "preparing") prepared.push(next);
      setAttachments((current) => replaceAttachment(current, item.localId, () => next));
    }
    if (prepared.length > 0) {
      await uploadPrepared(prepared, {
        attachmentsUrl: uploadUrl,
        clientId,
        setIdRef: owner.setIdRef,
        cancelledIds: owner.cancelledIds,
        setAttachmentSetId,
        setAttachments,
        isCurrent: () => owner.active,
      });
    }
    owner.busy = false;
  };

  const removeAttachment = (item: ComposerAttachment) => {
    if (item.status === "preparing") owner.cancelledIds.add(item.localId);
    setAttachments((current) => current.filter((candidate) => candidate.localId !== item.localId));
    const setId = owner.setIdRef.current;
    if (setId && item.descriptor && uploadUrl) {
      void removeComposerAttachment(
        uploadUrl,
        setId,
        item.descriptor.attachment_id,
        clientId,
      ).catch((error) => {
        if (owner.active) onError(error instanceof Error ? error.message : String(error));
      });
    }
  };

  const addReference = (item: DraftReference) => {
    if (referencesDisabled) return;
    const result = mergeReferences(references, [item], attachments.length);
    setReferences(result.references);
    if (result.rejected) onError(TOO_MANY);
  };

  /** Turns pasted or dropped RCP links into references and splices the rest at the cursor;
   * true when the composer consumed the text. */
  const insertReferenceText = (
    text: string,
    fileCount = attachments.length,
    insertPlainText = false,
  ): boolean => {
    if (!text || referencesDisabled || !project) return false;
    const result = extractReferences(text, project.id, references, fileCount, (selector) =>
      referenceLabel(project, selector),
    );
    if (result.rejected) onError(TOO_MANY);
    if (result.text === text && !insertPlainText) return false;
    setReferences(result.references);
    const field = input.current;
    const current = message.get();
    const start = field?.selectionStart ?? current.length;
    const end = field?.selectionEnd ?? start;
    message.set(current.slice(0, start) + result.text + current.slice(end));
    requestAnimationFrame(() =>
      field?.setSelectionRange(start + result.text.length, start + result.text.length),
    );
    return true;
  };

  /** How many dropped or pasted files fit beside what is already staged. */
  const filesAfter = (incoming: number) =>
    attachments.length +
    Math.min(incoming, MAX_CHAT_ATTACHMENTS - attachments.length - references.length);

  /** Drag-and-drop props for the composer: files attach, dropped links become references. */
  const dropTarget = () => ({
    onDragEnter: (event: DragEvent<HTMLElement>) => {
      if (carriesDroppable(event)) setDragging(true);
    },
    onDragOver: (event: DragEvent<HTMLElement>) => {
      if (!carriesDroppable(event)) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "copy";
    },
    onDragLeave: (event: DragEvent<HTMLElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false);
    },
    onDrop: (event: DragEvent<HTMLElement>) => {
      setDragging(false);
      const files = Array.from(event.dataTransfer.files);
      const text =
        event.dataTransfer.getData("text/uri-list") || event.dataTransfer.getData("text/plain");
      if (files.length) {
        event.preventDefault();
        void addFiles(files);
      }
      if (insertReferenceText(text, filesAfter(files.length), files.length === 0))
        event.preventDefault();
    },
  });

  /** A paste of files attaches them; pasted RCP links become references. */
  const paste = (event: ClipboardEvent<HTMLElement>) => {
    const files = Array.from(event.clipboardData.files);
    if (files.length) {
      event.preventDefault();
      void addFiles(files);
    }
    if (insertReferenceText(event.clipboardData.getData("text/plain"), filesAfter(files.length)))
      event.preventDefault();
  };

  const turnFields = (): ComposerTurnFields =>
    composerTurnFields(references, ready.length ? attachmentSetId : null, clientId, scope);

  /** After an admitted turn: the turn owns its files and references now. */
  const clear = () => {
    const sent = new Set(references.map((item) => referenceKey(item.selector)));
    const remainingReferences = (items: DraftReference[]) =>
      items.filter((item) => !sent.has(referenceKey(item.selector)));
    if (!owner.active) {
      // Admission may arrive after this conversation closed; consume only its
      // captured references, leaving any new owner's draft alone.
      if (referencesKey) {
        const remaining = remainingReferences(draftReferencesAt(referencesKey));
        writePref(referencesKey, remaining.length ? JSON.stringify(remaining) : null);
      }
      // A -> B -> A can remount this scope before the original admission returns.
      setReferences((current) =>
        currentScope.current.owner.active && currentScope.current.referencesKey === referencesKey
          ? remainingReferences(current)
          : current,
      );
      return;
    }
    setAttachments([]);
    setReferences(remainingReferences);
    setPickerOpen(false);
    setAttachmentSetId(null);
    owner.setIdRef.current = null;
    setAddMenuOpen(false);
    setDraftScope(crypto.randomUUID());
  };

  return {
    /** The files ready to travel with the next turn. */
    ready,
    /** Files or references are staged. */
    staged: attachments.length + references.length > 0,
    preparing,
    unready: attachments.some((item) => item.status !== "ready"),
    full: attachments.length + references.length >= MAX_CHAT_ATTACHMENTS,
    dragging,
    dropTarget,
    paste,
    turnFields,
    clear,
    /** A send closes the Add menu. */
    closeMenu: () => setAddMenuOpen(false),
    /** What the chips and the Add picker below render and change. */
    ui: {
      project,
      attachments,
      references,
      removeReference: (item: DraftReference) =>
        setReferences((current) =>
          current.filter(
            (candidate) => referenceKey(candidate.selector) !== referenceKey(item.selector),
          ),
        ),
      removeAttachment,
      addMenuOpen,
      setAddMenuOpen,
      pickerOpen,
      setPickerOpen,
      addFiles,
      addReference,
      referencesDisabled,
    },
  };
}

export type ComposerAttachments = ReturnType<typeof useComposerAttachments>;

/** The staged references and files, each removable. */
export function ComposerAttachmentChips({ composer }: { composer: ComposerAttachments }) {
  const { references, attachments, project } = composer.ui;
  return (
    <>
      {project && references.length > 0 && (
        <div className="chat-attachment-chips" aria-label="References for this turn">
          {references.map((item) => (
            <ReferenceChip
              key={referenceKey(item.selector)}
              projectId={project.id}
              reference={item}
              onRemove={() => composer.ui.removeReference(item)}
            />
          ))}
        </div>
      )}
      {attachments.length > 0 && (
        <div className="chat-attachment-chips" aria-label="Files for this turn">
          {attachments.map((item) => (
            <div className={`chat-attachment-chip ${item.status}`} key={item.localId}>
              {item.status === "preparing" ? (
                <LoaderCircle className="spin" size={12} />
              ) : (
                <File size={12} />
              )}
              <span>
                <strong>{item.file.name}</strong>
                <small>
                  {item.status === "preparing"
                    ? "Preparing"
                    : item.status === "ready"
                      ? `Ready · ${formatBytes(item.file.size)}`
                      : item.error || "Could not prepare file"}
                </small>
              </span>
              <button
                type="button"
                aria-label={`Remove ${item.file.name}`}
                onClick={() => composer.ui.removeAttachment(item)}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/** The Add button: upload files from this computer, or pick from RCP. */
export function ComposerAddPicker({
  composer,
  disabled,
}: {
  composer: ComposerAttachments;
  disabled: boolean;
}) {
  const { project, addMenuOpen, setAddMenuOpen, pickerOpen, setPickerOpen } = composer.ui;
  const pickerRef = useRef<HTMLDivElement | null>(null);
  const buttonRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const close = useCallback(() => setAddMenuOpen(false), [setAddMenuOpen]);
  useLayerDismiss(addMenuOpen, close, [pickerRef], buttonRef);
  return (
    <div className="chat-add-picker" ref={pickerRef}>
      <input
        ref={inputRef}
        className="visually-hidden"
        type="file"
        multiple
        accept={CHAT_ATTACHMENT_ACCEPT}
        onChange={(event) => {
          const files = Array.from(event.currentTarget.files ?? []);
          event.currentTarget.value = "";
          if (files.length) void composer.ui.addFiles(files);
        }}
      />
      <button
        ref={buttonRef}
        className="icon-button chat-add-file"
        type="button"
        aria-label="Add"
        aria-expanded={addMenuOpen}
        disabled={disabled}
        onClick={() => {
          setPickerOpen(false);
          setAddMenuOpen((open) => !open);
        }}
      >
        <Plus size={16} />
      </button>
      {addMenuOpen && (
        <div className="chat-add-menu" role="menu">
          <button
            type="button"
            role="menuitem"
            disabled={disabled}
            onClick={() => {
              setAddMenuOpen(false);
              inputRef.current?.click();
            }}
          >
            <Upload size={14} />
            Upload file
          </button>
          {project && (
            <button
              type="button"
              role="menuitem"
              disabled={disabled || composer.ui.referencesDisabled}
              onClick={() => {
                setAddMenuOpen(false);
                setPickerOpen(true);
              }}
            >
              <FolderOpen size={14} />
              From RCP…
            </button>
          )}
        </div>
      )}
      {project && pickerOpen && (
        <ProjectReferencePicker
          projectId={project.id}
          target={project.graph_target ?? MAIN_GRAPH}
          nodes={project.graph?.nodes ?? {}}
          selectedKeys={new Set(composer.ui.references.map((item) => referenceKey(item.selector)))}
          onPick={composer.ui.addReference}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
