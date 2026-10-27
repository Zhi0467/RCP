import type { TypstSource } from "./typstPreview";

interface TypstRuntime {
  $typst: {
    setCompilerInitOptions(options: { getModule: () => string }): void;
    use(provider: unknown): void;
    mapShadow(path: string, bytes: Uint8Array): Promise<void>;
    pdf(options: { mainFilePath: string }): Promise<Uint8Array | undefined>;
  };
  TypstSnippet: {
    preloadFontAssets(options: { assetUrlPrefix: string; assets: ["text"] }): unknown;
  };
}

self.onmessage = async ({ data }: MessageEvent<{ sources: TypstSource[]; root: string }>) => {
  try {
    // S4 serves the pinned typst.ts 0.7.0 bundle, WASM, and fonts. No runtime CDN.
    const assets = `${self.location.origin}/api/paper-assets/typst/`;
    // The browser bundle publishes $typst on window, including in a worker.
    Object.assign(globalThis, { window: globalThis });
    const moduleUrl = `${assets}all-in-one-lite.bundle.js`;
    const { $typst, TypstSnippet } = (await import(/* @vite-ignore */ moduleUrl)) as TypstRuntime;
    $typst.setCompilerInitOptions({ getModule: () => `${assets}typst_ts_web_compiler_bg.wasm` });
    $typst.use(TypstSnippet.preloadFontAssets({ assetUrlPrefix: assets, assets: ["text"] }));
    for (const source of data.sources) await $typst.mapShadow(`/${source.path}`, source.bytes);
    const pdf = await $typst.pdf({ mainFilePath: `/${data.root}` });
    if (!pdf) throw new Error("Typst compilation failed");
    self.postMessage({ pdf });
  } catch (error) {
    self.postMessage({ error: error instanceof Error ? error.message : String(error) });
  }
};
