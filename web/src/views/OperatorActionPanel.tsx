import { Check, Clipboard, ExternalLink, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import type { ServerOperatorRoute } from "../desktopRuntime";
import { formatCommandArgv } from "../projectSetup";
import type { ServerExecutionContext, ServerStep } from "../types";

/** The shell a displayed command belongs to, or null when the step never said. */
function executionLabel(context: ServerExecutionContext | null | undefined): string | null {
  if (!context) return null;
  return context.shell_account === null
    ? "Run on the server"
    : `Run on the server as ${context.shell_account}`;
}

/**
 * The OS account a saved operator route signs into, or null for the operator's
 * own login. A direct route is validated to be `rcp@host`, so it lands in the
 * service account itself rather than in a login that can elevate into it.
 */
function routeLandsAs(route: ServerOperatorRoute | null): string | null {
  if (!route || route.mode !== "direct_rcp") return null;
  return route.ssh_target.split("@")[0] ?? null;
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
  route,
}: {
  argv: string[];
  context: ServerExecutionContext | null | undefined;
  route: ServerOperatorRoute | null;
}) {
  const command = formatCommandArgv(argv);
  const where = executionLabel(context);
  // Only offer the saved route as the way in when it actually lands in the
  // shell this command needs. A direct route signs in as the service account,
  // which cannot then elevate into it, so pasting it beside a command that
  // expects the operator's own login would strand them mid-stop.
  const reaches = context != null && routeLandsAs(route) === context.shell_account;
  // Quoted like any other displayed command: a saved target may legally hold
  // shell metacharacters, and this string is copied straight into a shell.
  const entry = where && route && reaches ? formatCommandArgv(["ssh", route.ssh_target]) : null;
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

/** Whether two execution contexts name the same shell. */
function sameExecution(
  left: ServerExecutionContext | null | undefined,
  right: ServerExecutionContext | null | undefined,
): boolean {
  if (!left || !right) return !left && !right;
  return left.kind === right.kind && left.shell_account === right.shell_account;
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
  route = null,
  onRefresh,
}: {
  step: ServerStep;
  route?: ServerOperatorRoute | null;
  onRefresh?: () => void;
}) {
  const target =
    step.target.kind === "machine"
      ? `${step.target.os_account}@${step.target.host}`
      : `${step.target.service} · ${step.target.resource}`;
  const authority = step.target.kind === "machine" ? null : step.target.required_authority_role;
  // A stop may list its resume command among its actions; it is one step, not
  // two, and it belongs at the end where the resume block already puts it.
  // A value the operator pastes somewhere is a form field; one they only
  // compare is not, and giving it a Copy button invites pasting it into the
  // wrong box. A step that says nothing keeps the copyable form it had.
  const inputs = step.fields.filter((field) => field.role !== "evidence");
  const evidence = step.fields.filter((field) => field.role === "evidence");
  const actions = step.actions.filter(
    (action) =>
      action.kind !== "command" ||
      formatCommandArgv(action.argv) !== formatCommandArgv(step.resume_argv) ||
      !sameExecution(action.execution, step.resume_execution),
  );
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

      {inputs.length > 0 && (
        <div className="operator-values">
          {inputs.map((field) => (
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

      {evidence.length > 0 && (
        <dl className="operator-evidence">
          {evidence.map((field) => (
            <div key={field.name}>
              <dt>{field.name.replaceAll("_", " ")}</dt>
              <dd>{String(field.value)}</dd>
            </div>
          ))}
        </dl>
      )}

      <ol className="operator-steps">
        {actions.map((action, index) =>
          action.kind === "command" ? (
            <OperatorStep key={index} title={action.title ?? undefined}>
              <CommandBlock argv={action.argv} context={action.execution} route={route} />
            </OperatorStep>
          ) : (
            <OperatorStep key={index} title={action.title ?? undefined}>
              <p className="operator-note">{action.instruction}</p>
              {action.requirement && <p className="operator-requirement">{action.requirement}</p>}
            </OperatorStep>
          ),
        )}
        {step.resume_argv.length > 0 && (
          <OperatorStep title="Resume setup">
            <CommandBlock argv={step.resume_argv} context={step.resume_execution} route={route} />
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
