import { useState } from "react";
import { Package, X } from "lucide-react";
import type { PaperFile } from "../core/types";
import { PaperDialog } from "./PaperDialog";
import { fetchArxivExport } from "./arxivExport";
import { downloadPaperBlob } from "./paperDownloadModel";

export function PaperExport({
  projectId,
  file,
}: {
  projectId: string;
  file: PaperFile | undefined;
}) {
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<{ reason: string; log_tail: string } | null>(null);
  const enabled = file?.format === "latex";
  const run = async () => {
    if (!file || busy) return;
    setBusy(true);
    try {
      const result = await fetchArxivExport(projectId, file.file_id);
      if ("reason" in result) setFailure(result);
      else
        downloadPaperBlob(
          result.blob,
          `${
            file.path
              .split("/")
              .at(-1)
              ?.replace(/\.tex$/i, "") ?? "paper"
          }-arxiv.zip`,
        );
    } catch (error) {
      setFailure({ reason: "failed", log_tail: String(error) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <span title={enabled ? "Export for arXiv" : "Open a LaTeX file to export for arXiv"}>
        <button
          className="icon-button paper-export-button"
          aria-label="Export for arXiv"
          disabled={!enabled || busy}
          onClick={() => void run()}
        >
          <Package size={16} />
        </button>
      </span>
      {failure && (
        <PaperDialog
          label="Export for arXiv"
          className="paper-box paper-export-dialog"
          onClose={() => setFailure(null)}
        >
          <header className="paper-bar">
            <strong>
              {failure.reason === "not_root"
                ? "Select a LaTeX root file"
                : failure.reason === "unavailable"
                  ? "LaTeX engine unavailable"
                  : "Export failed"}
            </strong>
            <span className="paper-grow" />
            <button
              className="icon-button"
              aria-label="Close export"
              onClick={() => setFailure(null)}
            >
              <X size={16} />
            </button>
          </header>
          {failure.log_tail && (
            <pre
              ref={(node) => {
                if (node) node.scrollTop = node.scrollHeight;
              }}
            >
              {failure.log_tail}
            </pre>
          )}
        </PaperDialog>
      )}
    </>
  );
}
