export interface CiteKey {
  key: string;
  from: number;
  to: number;
}

export function citeKeys(source: string, format: string): CiteKey[] {
  const found: CiteKey[] = [];
  const add = (key: string, from: number) => found.push({ key, from, to: from + key.length });
  if (format === "latex") {
    const commands = /\\(?!bibcite\b)[a-zA-Z]*cite[a-zA-Z]*\*?(?:\s*\[[^\]]*\])*\s*\{([^}]*)\}/gi;
    for (const match of source.matchAll(commands)) {
      const offset = match.index + match[0].lastIndexOf("{") + 1;
      for (const key of match[1].matchAll(/[^,\s]+/g)) add(key[0], offset + key.index);
    }
  } else if (format === "markdown") {
    for (const group of source.matchAll(/\[[^\]\n]*@[^[\]\n]*\]/g)) {
      for (const key of group[0].matchAll(/(?<![\w\\])@([\w:./+-]+)/g))
        add(key[1].replace(/[.:]+$/, ""), group.index + key.index + 1);
    }
  } else if (format === "typst") {
    for (const match of source.matchAll(/(?<![\w\\])@([\w:./+-]+)|#cite\(\s*<([^>]+)>/g)) {
      const key = match[1]?.replace(/[.:]+$/, "") ?? match[2];
      add(key, match.index + (match[1] === undefined ? match[0].indexOf("<") + 1 : 1));
    }
  }
  return found;
}

export function citation(key: string, format: string): string {
  if (format === "latex") return `\\cite{${key}}`;
  if (format === "markdown") return `[@${key}]`;
  if (format === "typst") return `#cite(<${key}>)`;
  return "";
}
