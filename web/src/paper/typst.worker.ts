import type { TypstSource } from "./typstPreview";

interface TypstRuntime {
  $typst: {
    setCompilerInitOptions(options: { getModule: () => string }): void;
    use(provider: unknown): void;
    resetShadow(): Promise<void>;
    mapShadow(path: string, bytes: Uint8Array): Promise<void>;
    pdf(options: { mainFilePath: string }): Promise<Uint8Array | undefined>;
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
  let activeId = data.id;
  try {
    const $typst = await (runtime ??= compiler());
    while (pending) {
      const request = pending;
      pending = null;
      activeId = request.id;
      await $typst.resetShadow();
      for (const source of request.sources) await $typst.mapShadow(`/${source.path}`, source.bytes);
      const pdf = await $typst.pdf({ mainFilePath: `/${request.root}` });
      if (!pdf) throw new Error("Typst compilation failed");
      self.postMessage({ id: request.id, pdf });
    }
  } catch (error) {
    self.postMessage({
      id: pending?.id ?? activeId,
      error: error instanceof Error ? error.message : String(error),
    });
    pending = null;
  } finally {
    running = false;
  }
};
