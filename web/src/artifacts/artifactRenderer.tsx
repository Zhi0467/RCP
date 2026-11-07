import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ReactMarkdown from "react-markdown";
import rehypeKatex from "rehype-katex";
import renderMathInElement from "katex/contrib/auto-render";
import splitAtDelimiters from "katex/contrib/auto-render/splitAtDelimiters.ts";
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

const IGNORED_HTML_MATH = "script,noscript,style,textarea,pre,code,option,.katex,.katex-display";
const HTML_MATH_DELIMITERS = [
  { left: "$$", right: "$$", display: true },
  { left: "\\[", right: "\\]", display: true },
  { left: "\\(", right: "\\)", display: false },
  { left: "$", right: "$", display: false },
];
const HTML_MATH_LITERAL_PREFIXES = [
  { left: "\\\\", placeholder: "\u0000b" },
  { left: "\\$$", placeholder: "\u0000D\u0000" },
  { left: "\\$", placeholder: "\u0000d" },
];
const HTML_MATH_SPLITTER_DELIMITERS = [
  ...HTML_MATH_LITERAL_PREFIXES.map(({ left }) => ({ left, right: "", display: false })),
  ...HTML_MATH_DELIMITERS,
];

function restoreHtmlMathEscapes(text: string): string {
  for (const { left, placeholder } of HTML_MATH_LITERAL_PREFIXES)
    text = text.replaceAll(placeholder, () => left);
  return text;
}

function protectHtmlMathEscapes(text: string): string {
  return splitAtDelimiters(text, HTML_MATH_SPLITTER_DELIMITERS)
    .map(({ rawData, data }) => {
      const source = rawData ?? data;
      return HTML_MATH_LITERAL_PREFIXES.find(({ left }) => left === source)?.placeholder ?? source;
    })
    .join("");
}

function textNodes(element: HTMLElement): Text[] {
  return [element, ...element.querySelectorAll("*")]
    .filter((parent) => !parent.closest(IGNORED_HTML_MATH))
    .flatMap((parent) =>
      [...parent.childNodes].filter((node): node is Text => node.nodeType === Node.TEXT_NODE),
    );
}

function transformTextRuns(element: HTMLElement, transform: (text: string) => string): void {
  for (const node of textNodes(element)) {
    if (node.previousSibling instanceof Text) continue;
    const nodes = [node];
    for (let next = node.nextSibling; next instanceof Text; next = next.nextSibling)
      nodes.push(next);
    const source = nodes.map((text) => text.data).join("");
    const transformed = transform(source);
    if (transformed === source) continue;
    let start = 0;
    for (const text of nodes) {
      const end = start + text.length;
      text.data = transformed.slice(start, end);
      start = end;
    }
  }
}

export function renderHtmlMath(element: HTMLElement): void {
  transformTextRuns(element, protectHtmlMathEscapes);
  renderMathInElement(element, {
    ...KATEX_OPTIONS,
    throwOnError: false,
    preProcess: restoreHtmlMathEscapes,
    delimiters: HTML_MATH_DELIMITERS,
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    ignoredClasses: ["katex", "katex-display"],
  });
  transformTextRuns(element, restoreHtmlMathEscapes);
}
