---
id: S48-screen-story-token-scale
status: implemented
tier: hermetic
driver: browser
covered_by: web/tests/screenStoryComparisons.test.mjs; browser-driven 2026-08-02
invariants: []
last_passed: 2026-08-02 — browser-driven against isolated counted and zero-usage projects
---

# Measure a project's tokens in favorite screen stories

## UI path (confirmed 2026-08-02)

Open a project and enter **Settings**. Directly beneath the side-by-side
**Input processed** and **Generated** widgets, one narrow full-width film-frame
strip compares this project's counted token usage with one favorite screen
story. It has no heading, subtitle, icon, control, explanatory copy, or timed
rotation.

Every comparison entry represents a complete series or a film IP. The Before,
Back to the Future, and Lord of the Rings films are each represented by one
combined franchise entry. Television entries name only the series; there are
no episode or season-range entries. Source coverage and estimation confidence
remain in the bundled data record rather than appearing as UI commentary.

The comparison uses counted processed input plus counted generated output.
Cached input is already part of processed input and is not added again. The
strip chooses one entry once when Settings mounts and keeps it stable while
Settings remains open; leaving and reopening Settings samples again. It makes
no network request and stores no screenplay or transcript text.

At or above one screenplay-equivalent, the line reads in this form:

> This project has used about 7.4× as many tokens as the scripts for Breaking Bad.

Ratios below one use a percentage, ratios from one through ten use one decimal,
and larger ratios use whole numbers. A project with zero recorded usage shows
only the two zero-total widgets and no comparison strip.

## Drive

1. Open Settings for a project with counted input and generated usage.
2. Confirm the two usage widgets remain first and side by side, with one
   full-width film-frame strip immediately beneath them.
3. Confirm the strip names a whole series or film IP and never an episode,
   season, or season range.
4. Trigger usage refreshes while Settings remains open and confirm the chosen
   comparison does not change.
5. Leave Settings, reopen it, and confirm a comparison is sampled for the new
   mount without requiring a manual refresh or network request.
6. Open a zero-usage project and confirm the strip is absent.
7. Narrow the viewport and confirm the sentence wraps without clipping or
   disturbing the side-by-side desktop layout.

## Assert

- `comparison_ledger_contains_only_series_and_film_ip_entries`
- `estimated_tokens_are_derived_from_auditable_source_profiles`
- `comparison_total_is_processed_input_plus_generated_output`
- `cached_input_is_not_added_twice`
- `comparison_is_sampled_once_per_settings_mount`
- `comparison_copy_uses_unambiguous_ratio_rounding`
- `zero_usage_hides_the_comparison`
- `film_frame_sits_directly_beneath_the_two_usage_widgets`
- `no_console_or_application_request_errors`

## Failure means

The comparison uses a season or episode as its unit, changes during an open
Settings visit, double-counts cached input, fetches transcript text at runtime,
or displaces or obscures either usage widget.
