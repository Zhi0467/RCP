import { Plus, X } from "lucide-react";

import { proxyDraftRows } from "../nodeEditing";
import type { ExperimentProxy } from "../types";

/** Edits an Experiment's proxies as rows of "stands for" and "measured as". */
export function ProxyRowsEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (value: string) => void;
}) {
  const rows = proxyDraftRows(value);
  const update = (next: ExperimentProxy[]) => onChange(JSON.stringify(next));
  return (
    <span className="proxy-rows">
      {rows.map((row, index) => (
        <span className="proxy-row" key={index}>
          <input
            type="text"
            aria-label={`Proxy ${index + 1} stands for`}
            placeholder="Stands for"
            value={row.stands_for}
            onChange={(event) =>
              update(
                rows.map((item, i) =>
                  i === index ? { ...item, stands_for: event.target.value } : item,
                ),
              )
            }
          />
          <input
            type="text"
            aria-label={`Proxy ${index + 1} measured as`}
            placeholder="Measured as"
            value={row.measure}
            onChange={(event) =>
              update(
                rows.map((item, i) =>
                  i === index ? { ...item, measure: event.target.value } : item,
                ),
              )
            }
          />
          <button
            className="icon-button"
            type="button"
            aria-label={`Remove proxy ${index + 1}`}
            onClick={() => update(rows.filter((_, i) => i !== index))}
          >
            <X size={14} />
          </button>
        </span>
      ))}
      <button
        className="button compact"
        type="button"
        onClick={() => update([...rows, { stands_for: "", measure: "" }])}
      >
        <Plus size={14} /> Add proxy
      </button>
    </span>
  );
}
