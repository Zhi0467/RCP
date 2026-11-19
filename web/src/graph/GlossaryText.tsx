import { Fragment, type ReactNode } from "react";
import { segmentGlossaryText, type GlossaryIndex } from "./glossary";

interface Props {
  text: string;
  glossaryIndex?: GlossaryIndex;
}

export function GlossaryText({ text, glossaryIndex }: Props) {
  if (!glossaryIndex) return text;

  return segmentGlossaryText(text, glossaryIndex).map((segment, index) =>
    segment.kind === "text" ? (
      <Fragment key={`${index}:${segment.text}`}>{segment.text}</Fragment>
    ) : (
      <dfn
        className="glossary-definition"
        data-definition={segment.plainDefinition}
        tabIndex={0}
        key={`${index}:${segment.term}`}
      >
        {segment.text}
      </dfn>
    ),
  );
}

/** A node field as the node drawer shows it: lists as lists, objects as JSON, text with its glossary. */
export function formatNodeValue(value: unknown, glossaryIndex?: GlossaryIndex): ReactNode {
  if (Array.isArray(value))
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>{formatNodeValue(item, glossaryIndex)}</li>
        ))}
      </ul>
    );
  if (typeof value === "object" && value !== null)
    return <pre>{JSON.stringify(value, null, 2)}</pre>;
  return <GlossaryText text={String(value)} glossaryIndex={glossaryIndex} />;
}
