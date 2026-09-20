import { Check, Clipboard, ExternalLink, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { formatCommandArgv } from "../projectSetup";
import type { ServerExecutionContext, ServerStep } from "../types";

/** The shell a displayed command belongs to, or null when the step never said. */
function executionLabel(context: ServerExecutionContext | null | undefined): string | null {
  if (!context) return null;
  return context.shell_account === null
    ? "Run on the server"
    : `Run on the server as ${context.shell_account}`;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1600);
    return () => window.clearTimeout(timer);
  }, [copied]);
  return (
    <button
      className="button secondary tiny"
      type="button"
      aria-label={`Copy ${label}`}
      onClick={() => {
        void navigator.clipboard.writeText(value).then(
          () => setCopied(true),
          () => setCopied(false),
        );
      }}
    >
      {copied ? <Check size={12} /> : <Clipboard size={12} />} {copied ? "Copied" : "Copy"}
    </button>
  );
}

/** One command, with the shell it belongs to and the way into that shell. */
function CommandBlock({
  argv,
  context,
  sshTarget,
}: {
  argv: string[];
  context: ServerExecutionContext | null | undefined;
  sshTarget: string | null;
}) {
  const command = formatCommandArgv(argv);
  const where = executionLabel(context);
  const entry = where && sshTarget ? `ssh ${sshTarget}` : null;
  return (
    <>
      {where && (
        <div className="operator-run-on">
          <span className="tag">{where}</span>
          {entry && (
            <>
              <code>{entry}</code>
              <CopyButton value={entry} label="the sign-in command" />
            </>
          )}
        </div>
      )}
      <div className="operator-copy-row">
        <code className="operator-value">{command}</code>
        <CopyButton value={command} label="this command" />
      </div>
    </>
  );
}

/** One numbered action. A step that needs no name does not get a filler one. */
function OperatorStep({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <li>
      {title && (
        <header>
          <span>{title}</span>
        </header>
      )}
      {children}
    </li>
  );
}

/**
 * A human stop, rendered as the ordered list of single actions it actually is.
 *
 * The step's own contents decide what appears: one numbered entry per action,
 * the values it carries as a labelled copy list, and its resume command last.
 * Nothing here knows which stop it is rendering.
 */
export function OperatorActionPanel({
  step,
  sshTarget = null,
  onRefresh,
}: {
  step: ServerStep;
  sshTarget?: string | null;
  onRefresh?: () => void;
}) {
  const target =
    step.target.kind === "machine"
      ? `${step.target.os_account}@${step.target.host}`
      : `${step.target.service} · ${step.target.resource}`;
  const authority = step.target.kind === "machine" ? null : step.target.required_authority_role;
  return (
    <section className="provisioning-operator-action">
      <span className="eyebrow">Human action required</span>
      <h2>{step.title}</h2>
      <p>{step.message}</p>

      <div className="operator-where">
        <b>{step.target.kind === "machine" ? "Machine" : "Target"}</b>
        <code>{target}</code>
        {authority && (
          <>
            <b>You need</b>
            <span>{authority}</span>
          </>
        )}
      </div>

      <details className="operator-details">
        <summary>Why this step, and what success looks like</summary>
        <dl>
          <div>
            <dt>Purpose</dt>
            <dd>{step.purpose}</dd>
          </div>
          <div>
            <dt>Expected success</dt>
            <dd>{step.expected_success}</dd>
          </div>
        </dl>
      </details>

      {step.target.kind === "external_service" && (
        <a
          className="button secondary tiny operator-destination"
          href={step.target.destination_url}
          target="_blank"
          rel="noreferrer"
        >
          <ExternalLink size={12} /> Open {step.target.service}
        </a>
      )}

      {step.fields.length > 0 && (
        <div className="operator-values">
          {step.fields.map((field) => (
            <label key={field.name}>
              <span className="operator-value-name">{field.name.replaceAll("_", " ")}</span>
              <div className="operator-copy-row">
                <code className="operator-value">{String(field.value)}</code>
                <CopyButton value={String(field.value)} label={field.name} />
              </div>
            </label>
          ))}
        </div>
      )}

      <ol className="operator-steps">
        {step.actions.map((action, index) =>
          action.kind === "command" ? (
            <OperatorStep key={index}>
              <CommandBlock argv={action.argv} context={action.execution} sshTarget={sshTarget} />
            </OperatorStep>
          ) : (
            <OperatorStep key={index}>
              <p className="operator-note">{action.instruction}</p>
            </OperatorStep>
          ),
        )}
        {step.resume_argv.length > 0 && (
          <OperatorStep title="Resume setup">
            <CommandBlock
              argv={step.resume_argv}
              context={step.resume_execution}
              sshTarget={sshTarget}
            />
            {onRefresh && (
              <div className="operator-run-on">
                <button className="button primary tiny" type="button" onClick={onRefresh}>
                  <RefreshCw size={12} /> Refresh
                </button>
              </div>
            )}
          </OperatorStep>
        )}
      </ol>
    </section>
  );
}
