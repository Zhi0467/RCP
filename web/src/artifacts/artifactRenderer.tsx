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

const IGNORED_HTML_MATH = "script,noscript,style,textarea,pre,code,option,.katex,.katex-display";
const HTML_MATH_ESCAPES: Record<string, string> = {
  $: "\u0000rcp-dollar\u0000",
  $$: "\u0000rcp-double-dollar\u0000",
  "(": "\u0000rcp-parenthesis\u0000",
  "[": "\u0000rcp-bracket\u0000",
};

function restoreHtmlMathEscapes(text: string): string {
  for (const [delimiter, placeholder] of Object.entries(HTML_MATH_ESCAPES))
    text = text.replaceAll(placeholder, () => "\\" + delimiter);
  return text;
}

function protectHtmlMathEscapes(text: string): string {
  return text.replace(
    /(\\+)(\$\$?|\(|\[)?/g,
    (value: string, slashes: string, delimiter?: string) => {
      if (!delimiter) return value;
      const escaped = delimiter.startsWith("$")
        ? slashes.length % 2 === 1
        : slashes.length % 2 === 0;
      return escaped ? slashes.slice(0, -1) + HTML_MATH_ESCAPES[delimiter] : value;
    },
  );
}

function textNodes(element: HTMLElement): Text[] {
  return [element, ...element.querySelectorAll("*")]
    .filter((parent) => !parent.closest(IGNORED_HTML_MATH))
    .flatMap((parent) =>
      [...parent.childNodes].filter((node): node is Text => node.nodeType === Node.TEXT_NODE),
    );
}

export function renderHtmlMath(element: HTMLElement): void {
  for (const node of textNodes(element)) node.data = protectHtmlMathEscapes(node.data);
  renderMathInElement(element, {
    ...KATEX_OPTIONS,
    throwOnError: false,
    preProcess: restoreHtmlMathEscapes,
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "\\[", right: "\\]", display: true },
      { left: "\\(", right: "\\)", display: false },
      { left: "$", right: "$", display: false },
    ],
    ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code", "option"],
    ignoredClasses: ["katex", "katex-display"],
  });
  for (const node of textNodes(element)) node.data = restoreHtmlMathEscapes(node.data);
}
