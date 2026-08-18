---
id: S58-inline-glossary-definitions
status: implemented
tier: hermetic
driver: browser
covered_by: web/tests/glossary.test.mjs, web/tests/chatMarkdown.test.mjs,
  web/tests/inlineGlossary.test.mjs
invariants: []
reported_by: human, 2026-08-03
last_passed: 2026-08-03
---

# Definitions appear where a term is read

Confirmed by the human on 2026-08-03.

Glossary terms are supplementary reading aids, not a destination. The Glossary
navigation item and table are gone. Existing terms are matched best-effort as
whole terms in node prose, chat Markdown, and Proposal cards; hovering or
focusing a matched term exposes its plain definition without changing the
underlying text.

Who authors glossary entries remains an open question. This scenario adds no
creation or editing path.

## Drive

1. Open a project with at least one glossary term used in node prose, a chat
   reply, and a Proposal card.
2. Hover and keyboard-focus each occurrence.
3. Inspect similar substrings that are not whole-term matches.

## Assert

- Glossary is absent from primary navigation.
- Each whole-term occurrence exposes the stored plain definition on hover and
  focus.
- Substrings inside a different word are not marked.
- The rendered and copied text is unchanged.
- Matching work is indexed once per graph revision rather than rebuilt for each
  text node.
- No console, network, or server error occurs.

## Failure means

Definitions remain detached from reading, a spurious match changes meaning, or
the inline aid becomes a new authoring authority.
