import { createElement } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownAnswer({ text }: { text: string }) {
  return createElement(ReactMarkdown, {
    children: text,
    remarkPlugins: [remarkGfm],
  });
}
