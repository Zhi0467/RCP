import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import renderMathInElement from "katex/contrib/auto-render";
import { ANSWER_SYNTAX_PLUGINS, KATEX_OPTIONS } from "../core/markdownSyntax";

function InertLink({ href = "", children }: { href?: string; children?: ReactNode }) {
  return (
    <>
      {children}
      {" ("}
      {href}
      {")"}
    </>
  );
}

function InertImage({ src = "", alt = "" }: { src?: string | Blob; alt?: string }) {
  return (
    <>
      {alt}
      {" ("}
      {String(src)}
      {")"}
    </>
  );
}

export function renderMarkdown(source: string): string {
  return renderToStaticMarkup(
    <ReactMarkdown
      remarkPlugins={[...ANSWER_SYNTAX_PLUGINS]}
      rehypePlugins={[[rehypeKatex, KATEX_OPTIONS]]}
      components={{ a: InertLink, img: InertImage }}
      urlTransform={(url) => url}
    >
      {source}
    </ReactMarkdown>,
  );
}

export function renderHtmlMath(element: HTMLElement): void {
  renderMathInElement(element, {
    ...KATEX_OPTIONS,
    throwOnError: false,
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      { left: "$", right: "$", display: false },
    ],
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    ignoredClasses: ["katex", "katex-display"],
  });
}
