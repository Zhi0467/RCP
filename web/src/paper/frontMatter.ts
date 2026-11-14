const FENCE = /^---[ \t]*\r?$/;
const CLOSE = /^(---|\.\.\.)[ \t]*\r?$/;

/** The text with a leading YAML front matter block (a first `---` line through the next
 *  `---` or `...` line) blanked to empty lines, so the preview shows only the body and
 *  every later line keeps its number. Text without a closed block is returned as is. */
export function blankFrontMatter(text: string): string {
  const lines = text.split("\n");
  if (!FENCE.test(lines[0] ?? "")) return text;
  const end = lines.findIndex((line, index) => index > 0 && CLOSE.test(line));
  if (end < 0) return text;
  return lines.map((line, index) => (index <= end ? "" : line)).join("\n");
}
