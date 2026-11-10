import assert from "node:assert/strict";
import { test } from "node:test";
import { parseBibtex } from "../src/paper/bibtex.ts";
import { citeKeys, citation } from "../src/paper/citeKeys.ts";

test("percent comments outside entries cannot introduce entries", () => {
  const source =
    "% @article{old, title={x}}\n@misc{new, title={100% valid}} % @book{hidden}\n% @misc{last}";
  const entries = parseBibtex(source);
  assert.deepEqual(
    entries.map((entry) => entry.key),
    ["new"],
  );
  assert.equal(entries[0].fields.title, "100% valid");
  assert.equal(entries[0].from, source.indexOf("@misc{new"));
});

test("malformed-entry recovery skips percent-commented entries", () => {
  const entries = parseBibtex("@article{bad, broken\n% @book{hidden}\n@misc{kept}");
  assert.deepEqual(
    entries.map(({ key }) => key),
    ["bad", "kept"],
  );
});

test("Markdown strips sentence punctuation and excludes email addresses", () => {
  const source = "[see @c.] [me@x.com; @real:key.]";
  assert.deepEqual(citeKeys(source, "markdown"), [
    { key: "c", from: source.indexOf("@c") + 1, to: source.indexOf("@c") + 2 },
    { key: "real:key", from: source.indexOf("@real") + 1, to: source.indexOf("@real") + 9 },
  ]);
});

test("Typst explicit cite ranges start inside angle brackets", () => {
  for (const key of ["c", "cite", "i"]) {
    const source = `#cite(<${key}>)`;
    assert.deepEqual(citeKeys(source, "typst"), [{ key, from: 7, to: 7 + key.length }]);
  }
  assert.deepEqual(citeKeys("me@x.com", "typst"), []);
});

test("LaTeX bibliography records are not citation commands", () => {
  const source = String.raw`\bibcite{g} \cite{g}`;
  const from = source.lastIndexOf("g");
  assert.deepEqual(citeKeys(source, "latex"), [{ key: "g", from, to: from + 1 }]);
});

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
