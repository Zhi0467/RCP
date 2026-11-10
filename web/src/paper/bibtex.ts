export interface BibEntry {
  key: string;
  type: string;
  fields: Record<string, string>;
  from: number;
  to: number;
  unreadable: boolean;
}

class BibReader {
  source: string;
  at: number;
  strings: Map<string, string>;

  constructor(source: string, at: number, strings: Map<string, string>) {
    this.source = source;
    this.at = at;
    this.strings = strings;
  }

  space() {
    while (this.at < this.source.length) {
      if (/\s/.test(this.source[this.at])) this.at++;
      else if (this.source[this.at] === "%") {
        const end = this.source.indexOf("\n", this.at);
        this.at = end < 0 ? this.source.length : end;
      } else break;
    }
  }

  enclosed(recover = true): string {
    const open = this.source[this.at++];
    const close = open === "{" ? "}" : '"';
    let depth = 0;
    let value = "";
    while (this.at < this.source.length) {
      if (recover && /^\n\s*@\w+\s*[{(]/.test(this.source.slice(this.at))) throw new Error();
      const char = this.source[this.at++];
      if (char === "\\") {
        value += char + (this.source[this.at++] ?? "");
      } else if (char === close && depth === 0) return value;
      else {
        if (char === "{") depth++;
        if (char === "}") depth--;
        value += char;
      }
    }
    throw new Error();
  }

  value(): string {
    let value = "";
    do {
      this.space();
      if (this.source[this.at] === "{" || this.source[this.at] === '"') value += this.enclosed();
      else {
        const token = /^[\w:./+-]+/.exec(this.source.slice(this.at))?.[0];
        if (!token) throw new Error();
        this.at += token.length;
        value += this.strings.get(token.toLowerCase()) ?? token;
      }
      this.space();
      if (this.source[this.at] !== "#") break;
      this.at++;
    } while (this.at < this.source.length);
    return value
      .replace(/(?<!\\)[{}]/g, "")
      .replace(/\\([{}&%_])/g, "$1")
      .replace(/\s+/g, " ")
      .trim();
  }
}

export function parseBibtex(source: string): BibEntry[] {
  const entries: BibEntry[] = [];
  const strings = new Map<string, string>();
  const start = /%[^\r\n]*|@(\w+)\s*([{(])/g;
  let match: RegExpExecArray | null;
  while ((match = start.exec(source))) {
    if (!match[1]) continue;
    const type = match[1].toLowerCase();
    const close = match[2] === "{" ? "}" : ")";
    const reader = new BibReader(source, start.lastIndex, strings);
    const entry: BibEntry = {
      key: "",
      type,
      fields: {},
      from: match.index,
      to: source.length,
      unreadable: false,
    };
    try {
      if (type === "comment" || type === "preamble") {
        reader.at--;
        if (match[2] === "{") reader.enclosed(false);
        else {
          const end = source.indexOf(")", reader.at);
          reader.at = end < 0 ? source.length : end + 1;
        }
      } else {
        reader.space();
        if (type !== "string") {
          const key = /^[^\s,{}()]+/.exec(source.slice(reader.at))?.[0];
          if (!key) throw new Error();
          entry.key = key;
          reader.at += key.length;
          reader.space();
          if (source[reader.at] === ",") reader.at++;
          else if (source[reader.at] !== close) throw new Error();
        }
        while (true) {
          reader.space();
          if (source[reader.at] === close) {
            reader.at++;
            break;
          }
          const field = /^([\w-]+)\s*=\s*/.exec(source.slice(reader.at));
          if (!field) throw new Error();
          reader.at += field[0].length;
          const value = reader.value();
          entry.fields[field[1].toLowerCase()] = value;
          if (type === "string") strings.set(field[1].toLowerCase(), value);
          if (source[reader.at] === ",") reader.at++;
          else if (source[reader.at] !== close) throw new Error();
        }
      }
      entry.to = reader.at;
      start.lastIndex = reader.at;
    } catch {
      entry.unreadable = true;
      const next = new RegExp(start.source, "g");
      next.lastIndex = start.lastIndex;
      let boundary = next.exec(source);
      while (boundary && !boundary[1]) boundary = next.exec(source);
      entry.to = boundary?.index ?? source.length;
      start.lastIndex = entry.to;
    }
    if (!["string", "comment", "preamble"].includes(type)) entries.push(entry);
  }
  return entries;
}
