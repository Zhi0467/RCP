import type { TypstSource } from "./typstPreview";

interface TypstRuntime {
  $typst: {
    setCompilerInitOptions(options: { getModule: () => string }): void;
    use(provider: unknown): void;
    resetShadow(): Promise<void>;
    mapShadow(path: string, bytes: Uint8Array): Promise<void>;
    getCompiler(): Promise<{
      reset(): Promise<void>;
      compile(options: { mainFilePath: string; format: 1; diagnostics: "full" }): Promise<{
        result?: Uint8Array;
        diagnostics?: { path: string; range: string; message: string }[];
      }>;
    }>;
  };
  TypstSnippet: {
    preloadFontAssets(options: { assetUrlPrefix: string; assets: ["text"] }): unknown;
  };
}

type Request = { id: number; sources: TypstSource[]; root: string };
let pending: Request | null = null;
let running = false;
let runtime: Promise<TypstRuntime["$typst"]> | null = null;

async function compiler() {
  const assets = `${self.location.origin}/api/paper-assets/typst/`;
  Object.assign(globalThis, { window: globalThis });
  const moduleUrl = `${assets}all-in-one-lite.bundle.js`;
  const { $typst, TypstSnippet } = (await import(/* @vite-ignore */ moduleUrl)) as TypstRuntime;
  $typst.setCompilerInitOptions({ getModule: () => `${assets}typst_ts_web_compiler_bg.wasm` });
  $typst.use(TypstSnippet.preloadFontAssets({ assetUrlPrefix: assets, assets: ["text"] }));
  return $typst;
}

self.onmessage = async ({ data }: MessageEvent<Request>) => {
  // Finish the active compile, then run only the newest queued tree.
  pending = data;
  if (running) return;
  running = true;
  try {
    while (pending) {
      const request = pending;
      pending = null;
      try {
        // A failed load (offline, engine not installed yet) must not stick.
        const $typst = await (runtime ??= compiler().catch((error: unknown) => {
          runtime = null;
          throw error;
        }));
        await $typst.resetShadow();
        for (const source of request.sources)
          await $typst.mapShadow(`/${source.path}`, source.bytes);
        // The convenience pdf() API forces diagnostics off and throws Rust debug text.
        const session = await $typst.getCompiler();
        await session.reset();
        const result = await session.compile({
          mainFilePath: `/${request.root}`,
          format: 1,
          diagnostics: "full",
        });
        const pdf = result.result;
        if (!pdf) {
          throw new Error(
            result.diagnostics
              ?.map(({ path, range, message }) => {
                const line = range ? Number.parseInt(range, 10) + 1 : null;
                return path && line ? `${path}:${line}: ${message}` : message;
              })
              .join("\n") || "Typst compilation failed",
          );
        }
        self.postMessage({ id: request.id, pdf });
      } catch (error) {
        // The error belongs to this tree; a newer queued tree still compiles.
        self.postMessage({
          id: request.id,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }
  } finally {
    running = false;
  }
};
