import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import type { PaperTree } from "../core/types";
import { readReferenceFiles, type PaperReference } from "./referenceFiles";
export type { PaperReference } from "./referenceFiles";

export function usePaperReferences(projectId: string, tree: PaperTree | undefined) {
  const cache = useRef(new Map<string, string>());
  const [result, setResult] = useState<{ entries: PaperReference[]; error: string | null }>({
    entries: [],
    error: null,
  });
  useEffect(() => {
    const controller = new AbortController();
    void readReferenceFiles(projectId, tree?.files ?? [], controller.signal, cache.current)
      .then((entries) => {
        if (!controller.signal.aborted) setResult({ entries, error: null });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setResult({ entries: [], error: String(error) });
      });
    return () => controller.abort();
  }, [projectId, tree]);
  return result;
}

export function PaperReferences({
  entries,
  error,
  folded = false,
  onChoose,
}: {
  entries: PaperReference[];
  error: string | null;
  folded?: boolean;
  onChoose: (entry: PaperReference) => void;
}) {
  const [open, setOpen] = useState(!folded);
  const [search, setSearch] = useState("");
  const groups = new Map<string, PaperReference[]>();
  for (const entry of entries) {
    if (
      `${entry.key} ${Object.values(entry.fields).join(" ")}`
        .toLowerCase()
        .includes(search.toLowerCase())
    )
      groups.set(entry.file.path, [...(groups.get(entry.file.path) ?? []), entry]);
  }
  const counts = new Map<string, number>();
  for (const entry of entries)
    if (entry.key) counts.set(entry.key, (counts.get(entry.key) ?? 0) + 1);
  return (
    <section className={`paper-box paper-references ${open ? "expanded" : ""}`}>
      <button
        className="paper-bar paper-reference-fold"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <strong>References</strong>
        <span>{entries.length}</span>
        <span className="paper-grow" />
        <ChevronRight size={15} className={open ? "open" : ""} />
      </button>
      <div className="paper-reference-fold-body" inert={!open}>
        <div className="paper-reference-list paper-well">
          <input
            aria-label="Search references"
            placeholder="Search"
            type="search"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          {error && <p role="alert">{error}</p>}
          {[...groups].map(([path, group]) => (
            <div key={path}>
              <div className="paper-reference-group">{path}</div>
              {group.map((entry) => (
                <button
                  className="paper-reference-entry"
                  key={entry.from}
                  onClick={() => onChoose(entry)}
                  title={entry.key}
                >
                  <strong>{entry.fields.title || entry.key || "Unreadable entry"}</strong>
                  <span>
                    {[entry.fields.author, entry.fields.year].filter(Boolean).join(" · ")}
                  </span>
                  {entry.unreadable && <span className="paper-reference-warning">Unreadable</span>}
                  {(counts.get(entry.key) ?? 0) > 1 && (
                    <span className="paper-reference-warning">Duplicate</span>
                  )}
                </button>
              ))}
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function referenceUrl(value: string | undefined, prefix = ""): string | null {
  if (!value) return null;
  try {
    const url = new URL(/^https?:\/\//i.test(value) ? value : prefix + value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

export function PaperReferencePage({
  entry,
  unknown,
  onSource,
  onBack,
}: {
  entry: PaperReference | undefined;
  unknown: string | null;
  onSource: (entry: PaperReference) => void;
  onBack?: () => void;
}) {
  const fields = entry?.fields ?? {};
  const links = [
    ["DOI", referenceUrl(fields.doi, "https://doi.org/")],
    ["URL", referenceUrl(fields.url)],
    ["eprint", referenceUrl(fields.eprint, "https://arxiv.org/abs/")],
  ];
  return (
    <article className="paper-reference-page">
      {onBack && (
        <button className="icon-button" aria-label="Back to references" onClick={onBack}>
          <ChevronLeft size={16} />
        </button>
      )}
      {entry ? (
        <>
          <h2>{fields.title || entry.key}</h2>
          <p>{fields.author}</p>
          <p>
            {[fields.journal || fields.booktitle || fields.publisher, fields.year]
              .filter(Boolean)
              .join(" · ")}
          </p>
          {fields.abstract && <p className="paper-reference-abstract">{fields.abstract}</p>}
          <nav>
            {links.map(
              ([label, href]) =>
                href && (
                  <a key={label} href={href} target="_blank" rel="noopener noreferrer">
                    {label}
                  </a>
                ),
            )}
          </nav>
          <button className="paper-reference-source button" onClick={() => onSource(entry)}>
            {entry.file.path}
          </button>
          {entry.unreadable && <p role="status">Unreadable entry</p>}
        </>
      ) : unknown ? (
        <p>{`Unknown reference: ${unknown}`}</p>
      ) : null}
    </article>
  );
}
