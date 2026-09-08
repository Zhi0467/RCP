import type { ModelChoice, ProviderReadiness } from "./types";

/**
 * Turning the backend provider registry into select options.
 *
 * The frontend knows no provider facts of its own. Ids, labels, models, and
 * reasoning efforts all arrive from `src/rcp/providers.py` by way of a
 * readiness probe, so adding a provider there is enough to make it appear here.
 */

export interface Option {
  id: string;
  label: string;
}

/**
 * Keep a saved value selectable even when the provider no longer offers it —
 * the CLI is unreachable, the model was retired, the manifest predates the
 * catalog. Dropping it would silently rewrite what the human saved.
 */
export function withSaved(options: Option[], saved: string): Option[] {
  if (!saved || options.some((option) => option.id === saved)) return options;
  return [...options, { id: saved, label: saved }];
}

export function providerOptions(providers: ProviderReadiness[], saved: string): Option[] {
  return withSaved(
    providers.map((item) => ({ id: item.provider, label: item.label || item.provider })),
    saved,
  );
}

export function readinessFor(
  providers: ProviderReadiness[],
  provider: string,
): ProviderReadiness | undefined {
  return providers.find((item) => item.provider === provider);
}

export function modelsFor(providers: ProviderReadiness[], provider: string): ModelChoice[] {
  return readinessFor(providers, provider)?.models ?? [];
}

/**
 * Runtimes the provider offers. Unlike a model, a runtime has no "provider
 * default" entry: the backend resolves an omitted value to a concrete name
 * before any surface sees it, so every option here is a real runtime.
 */
export function runtimeOptions(readiness: ProviderReadiness | undefined, saved: string): Option[] {
  return withSaved(
    (readiness?.runtimes ?? []).map(({ id, label }) => ({ id, label })),
    saved,
  );
}

/**
 * Only catalogued models are offered; there is no "provider default" entry. An
 * empty saved value is shown, and run, as the head of the catalog.
 */
export function modelOptions(models: ModelChoice[], saved: string): Option[] {
  return withSaved(
    models.map(({ id, label }) => ({ id, label })),
    saved,
  );
}

/** The model the backend runs for an empty saved value: the first one vendored. */
export function firstModel(models: ModelChoice[]): string {
  return models[0]?.id ?? "";
}

/** What a model select shows: the saved model, else the first catalogued one. */
export function selectedModel(models: ModelChoice[], saved: string): string {
  return saved || firstModel(models);
}

/** Efforts the chosen model accepts; every known effort when the model is unknown. */
export function reasoningFor(models: ModelChoice[], model: string): string[] {
  const chosen = models.find((item) => item.id === model);
  if (chosen) return chosen.reasoning;
  return [...new Set(models.flatMap((item) => item.reasoning))];
}

export function reasoningOptions(models: ModelChoice[], model: string, saved: string): Option[] {
  return withSaved(
    reasoningFor(models, model).map((id) => ({ id, label: id })),
    saved,
  );
}

/**
 * Move to a provider, dropping the model with it. A model id belongs to one
 * provider — carrying `gpt-5.5` over to Claude would offer a value Claude
 * rejects — so the choice becomes the new provider's first catalogued model.
 * The effort survives when that model accepts it, which the common
 * `low`..`xhigh` levels do.
 */
export function providerChange(
  models: ModelChoice[],
  provider: string,
  reasoning: string,
): { provider: string; model: string; reasoning?: string } {
  return { provider, ...modelChange(models, firstModel(models), reasoning) };
}

/**
 * Move to a model, keeping the current effort only if the new model accepts it.
 * Codex's efforts differ per model — `gpt-5.6-sol` takes `ultra` and `gpt-5.5`
 * does not — so an unchecked carry-over would be rejected at the API.
 */
export function modelChange(
  models: ModelChoice[],
  model: string,
  reasoning: string,
): { model: string; reasoning?: string } {
  const accepted = reasoningFor(models, model);
  if (accepted.length === 0 || accepted.includes(reasoning)) return { model };
  const fallback = models.find((item) => item.id === model)?.default_reasoning;
  return { model, reasoning: fallback || accepted[0] };
}
