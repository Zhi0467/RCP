import assert from "node:assert/strict";
import { test } from "node:test";
import { parseBibtex } from "../src/paper/bibtex.ts";
import { citeKeys, citation } from "../src/paper/citeKeys.ts";

test("BibTeX reads nested braces, quoted fields, strings and concatenation", () => {
  const source = String.raw`@string{conf = "Neur" # "IPS"}
@comment{ignored}
@preamble{"ignored"}
@article{attention,
 title={Attention {Is} All You Need}, author="Vaswani and Shazeer",
 booktitle=conf, year=2017, abstract={Nested {content} and \{escaped\} braces}
}`;
  const [entry] = parseBibtex(source);
  assert.equal(entry.key, "attention");
  assert.equal(entry.fields.title, "Attention Is All You Need");
  assert.equal(entry.fields.booktitle, "NeurIPS");
  assert.equal(entry.fields.year, "2017");
  assert.equal(entry.unreadable, false);
  assert.equal(source.slice(entry.from, entry.to).startsWith("@article"), true);
});

test("malformed entries remain visible without consuming later entries", () => {
  const entries = parseBibtex(
    '@article{bad, title={unfinished\n@book{good, title="Readable"}\n@misc{last, year=2024}',
  );
  assert.deepEqual(
    entries.map(({ key, unreadable }) => [key, unreadable]),
    [
      ["bad", true],
      ["good", false],
      ["last", false],
    ],
  );
});

test("quoted braces and parentheses do not terminate entries", () => {
  const [entry] = parseBibtex('@article(key, title="A {Nested} (Title)", year={2024})');
  assert.equal(entry.fields.title, "A Nested (Title)");
  assert.equal(entry.fields.year, "2024");
});

for (const [format, source, keys] of [
  [
    "latex",
    String.raw`See \cite{one,two} and \citep[see][p. 4]{three} \textcite*{four}.`,
    ["one", "two", "three", "four"],
  ],
  ["markdown", "See [@one; @two, p. 2] and person@example.org", ["one", "two"]],
  ["typst", "See @one and #cite(<two>) and person@example.org", ["one", "two"]],
])
  test(`cite ranges and insertion use ${format} syntax`, () => {
    const found = citeKeys(source, format);
    assert.deepEqual(
      found.map((item) => item.key),
      keys,
    );
    for (const item of found) assert.equal(source.slice(item.from, item.to), item.key);
    assert.deepEqual(
      citeKeys(citation("inserted", format), format).map((item) => item.key),
      ["inserted"],
    );
  });

test("Typst sentence punctuation and capitalized LaTeX commands keep the real key", () => {
  assert.deepEqual(
    citeKeys("See @one.", "typst").map((item) => item.key),
    ["one"],
  );
  assert.deepEqual(
    citeKeys(String.raw`\Citep{one}`, "latex").map((item) => item.key),
    ["one"],
  );
});

test("comments and preambles containing entry syntax are skipped", () => {
  const entries = parseBibtex(
    '@comment{\n@article{hidden, title={Not an entry}}\n}\n@preamble{"text"}\n@book{visible, title={Visible}}',
  );
  assert.deepEqual(
    entries.map((entry) => entry.key),
    ["visible"],
  );
});
