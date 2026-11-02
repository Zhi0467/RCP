# Design: Snowbound theme and the Settings rail

Status: design for review, in PR #368. Delete this file before merge. Its rules
move into `docs/specs/interface-and-visual-design.md` and
`docs/specs/api-web-and-desktop-projections.md` ("Paper, Settings, and History").

## Goal

1. Add a third theme, **Snowbound**: an 8-bit console look of a cabin in snowy
   woods. Light mode is a snowy day; dark mode is a starry night.
2. Move the display preferences out of the identity menu into Space Settings.
3. Give both settings pages a section rail that shows one pane at a time, with
   a link per section.

The human chose Snowbound, including its falling snow, over two other pixel
directions (a 16-bit cabin interior and a four-shade handheld LCD) from mockups
rendered on the real app. The human also chose the picker's new home, one pane
at a time with a link per section, and the rail on both settings pages.

## Non-goals

- The other two pixel directions.
- The woods scene anywhere but the space page's project view. Pip's thread view,
  project pages, and the DAG canvas stay plain.
- An Appearance pane in Project Settings. Each preference has one home.
- A route for Space Settings, remembering the last pane, or search in Settings.
- Renaming Aqua's `--aqua-*` material tokens.

## Part 1: Snowbound

Extend the theme mechanism from two themes to three. `web/src/ui/theme.ts` owns
the choice. One CSS file per theme owns the look, as Aqua does today.

### Theme choice

- `ThemeChoice` becomes `"classic" | "aqua" | "snowbound"`. `THEME_CHOICES`
  lists all three in that order. `themeChoiceLabel` returns "Snowbound".
- `normalizeThemeChoice` keeps each known value and maps unknown values to Aqua.
  Aqua stays the default. Mode stays independent.
- The pre-paint script in `web/index.html` reads `rcp:appearance` and stamps
  `data-theme` before React runs. It accepts `"snowbound"`. Today it maps
  everything but `"classic"` to Aqua, which would flash Aqua on load.
- `SPACE_RUN_BADGE_PALETTE` in `experiments/SpaceRuns.tsx` is keyed by
  `ResolvedTheme`. It gains `snowbound-light` and `snowbound-dark` entries.

### Look

`web/src/themes/snowbound.css`, imported after `aqua.css` in `styles.css`.
Light rules use `:root[data-theme="snowbound"]`. Dark rules use
`:root[data-theme="snowbound"][data-color-mode="dark"]`, which outranks the base
`:root[data-color-mode="dark"]`.

The grammar:

- Square corners. Frames are 2px with the four corner pixels knocked out.
- Shadows are hard and unblurred, offset 4–6px down and right.
- Buttons rise 1px on hover and press into their shadow on click, with the
  app's normal short transitions. No stepped timing on controls.
- The selected tab, segment, or rail item is the "lit square": a pale gold fill
  (dim gold in dark) inside the notched frame.
- Focus is a 2px dashed outline in the accent color.

Coverage. Snowbound restyles every surface Aqua restyles, by the same two
routes Aqua uses:

- Selector rules, mirroring `aqua.css` and Aqua's rules in
  `styles/10-settings.css`, `styles/12-operations.css`,
  `styles/14-agent.css` (`.upcoming-popover`, `.approval-card`), and
  `ui/AppearancePicker.css`.
- Material tokens. `styles/07-paper.css`, `paper/paper-coach.css`, and
  `paper/paper-preview.css` read `--aqua-surface`, `--aqua-raised`,
  `--aqua-inset`, `--aqua-field`, and `--aqua-selected`, with Classic
  fallbacks. Snowbound defines those five tokens with its pixel values, so
  those surfaces follow it with no edits to the paper files.

Palette. These are the build inputs. They live in `snowbound.css`; the spec gets
the grammar in prose, not these values.

| Token | Light | Dark |
| --- | --- | --- |
| `--paper` | `#dfe9f5` | `#0d1733` |
| `--paper-deep` | `#c9d9ec` | `#0a1129` |
| `--sheet` | `#fbfcff` | `#1a2850` |
| `--walnut` | `#17203a` | `#f2f5ff` |
| `--ink-soft` | `#4b5878` | `#a9b6d8` |
| `--oxblood` | `#b8282c` | `#ffb833` |
| `--oxblood-dark` | `#8c1a1e` | `#ffd36e` |
| `--accent-ink` | `#ffffff` | `#1a1206` |
| `--teal` | `#1f6f86` | `#6fd6e8` |
| `--mustard` | `#8a6000` | `#ffd23f` |
| `--coral` | `#b8382a` | `#ff7a5c` |
| `--indigo` | `#3a4fa0` | `#9fb0ff` |
| `--plum` | `#7a3a8a` | `#e09bf0` |
| `--forest` | `#1c6b3c` | `#6ee07a` |
| `--moss` | `#4f6a1c` | `#b8e05a` |
| `--slate` | `#48668c` | `#8fb0e0` |
| `--rose-paper` | `#f6dcdc` | `#3a2a40` |
| `--red-soft` | `#f6dcdc` | `#3a2a40` |
| `--rule` | `#8ea4c4` | `#3a4f86` |
| `--shadow-rgb` | `23 32 58` | `0 0 0` |
| `--px-frame` | `#17203a` | `#04081a` |
| `--px-drop` | `#17203a` | `#04081a` |
| `--px-raise` | `#ffffff` | `#2b3f72` |
| `--px-field` | `#eef3fa` | `#101c3e` |
| `--px-select` | `#ffe9a8` | `#3a3416` |

`--segmented-selected` is `var(--px-select)` and `--segmented-selected-shadow`
is the notched frame, so every segmented control and the rail pick up the lit
square.

Contrast. `theme.test.mjs` asserts these pairs for both Snowbound modes, at
4.5:1 or better: ink on paper, ink on sheet, soft ink on sheet, accent ink on
the accent, ink on the lit square, and each semantic accent on sheet.
The values above pass; the lowest is 5.45:1.

### Type

- Snowbound sets `h1`, `h2`, and `h3` in **Pixelify Sans** 600 through one
  selector rule. It does not touch `--display`: `--editorial` derives from
  `--display` (`01-base.css`) and styles chat lines and node text, which must
  stay in Figtree.
- Body text, labels, chat, node text, and data stay in Figtree and JetBrains
  Mono.
- The font is bundled like Figtree: add `@fontsource/pixelify-sans` 5.3.0 to
  `web/package.json` and the lockfile, and import
  `@fontsource/pixelify-sans/600.css` in `main.tsx` beside the Figtree import.
  This is the only new dependency.
- The spec's font sentence ("Every theme sets interface and display text in
  Figtree…") gains the exception: Snowbound sets headings in Pixelify Sans.

### The space page scene

Only the space page's project view (`.landing-shell:not(.agent-mode)`) shows
the scene. `.landing-shell` gets `isolation: isolate` so the scene layers sit
between its paper and its content.

- **Woods band.** A pixel strip of snowy pines and a cabin (lit window, chimney
  smoke) sits at the bottom of the page, after the content. It is pure CSS: an
  `::after` with a background SVG, plus bottom padding on the shell so it never
  covers content. The cabin is centered at every width and the pines repeat to
  fill the sides. Two checked-in files, `themes/snowbound-woods-light.svg` and
  `themes/snowbound-woods-dark.svg`, each under 20 KB, use
  `shape-rendering="crispEdges"`. No component, no theme branching in React.
- **Snow dots.** A sparse 2px dot pattern as a `background-image` on the shell.
  Color tokens stay plain hex.
- **Snowfall.** Pixel snowflakes drift slowly behind the content. A fixed
  `::before` on its own layer animates `transform`, not `background-position`,
  so the page is not repainted each frame. `prefers-reduced-motion: reduce`
  stops it. This is the theme's one continuous animation. The spec's
  smooth-motion rule gains one sentence: ambient decoration may move
  continuously when it sits behind content, never shifts layout, and stops
  under reduced motion.

## Part 2: Display preferences move to Space Settings

- `AppearancePicker` (Mode, Theme, and the desktop-only Text size) leaves
  `LandingIdentityMenu`. It becomes the **Appearance** pane of Space Settings.
  Its region label becomes "Appearance" and its doc comment names its new home.
- `SpaceSettings` gains `appearance: AppearancePickerProps` and
  `textScale?: TextScaleControl` props, passed from `App.tsx`.
- Both `LandingIdentityMenu` call sites drop the picker props: the project
  header menu in `App.tsx` and `ProjectLanding.tsx`. `ProjectLanding` drops its
  appearance props except `palette`, which `SpaceRuns` still needs.
- Storage does not change: it is still a per-device preference in local storage.

## Part 3: the Settings rail

### Shared component

`web/src/ui/SettingsRail.tsx` plus `web/src/ui/SettingsRail.css`.

```ts
interface SettingsSection {
  id: string;        // fixed, owned by the page's list
  label: string;
  icon: ReactNode;
  render: () => ReactNode;
}
// <SettingsRail sections={…} selected={id | null} onSelect={(id) => …} />
```

- The page passes only the sections that exist. Hidden sections are left out of
  the array.
- Left: the rail, icon and label, no descriptions. Right: the panes.
- **Panes stay mounted.** Each pane renders once; non-selected panes get
  `hidden`, and `SettingsRail.css` has `.settings-pane[hidden] { display: none }`
  so no wrapper `display` overrides it. Drafts (new-machine form, Agent
  defaults, Skills), open dialogs, and loaded data survive a switch. Switching
  never refetches.
- Switching panes scrolls the pane area to the top.
- The selected item uses `--segmented-selected` and
  `--segmented-selected-shadow`: raised in Aqua, the lit square in Snowbound, a
  plain sheet in Classic.
- `selected` that is `null`, unknown, or absent from the array shows the first
  section.
- At ≤560px the rail becomes a horizontal strip at the top of the page that
  scrolls within its own width, like the Artifacts Files | Paper switch. The pane
  sits below it. No list page, no extra Back.

### Space Settings

Section list in `SpaceSettings.tsx`, in this order:

| id | Label | Icon | Present when |
| --- | --- | --- | --- |
| `appearance` | Appearance | `Palette` | always |
| `server` | Team server | its existing header icon | team space |
| `this-mac` | This Mac | `HardDrive` | personal space, unless the status loaded and says unsupported |
| `machines` | Machines | `Server` | always |
| `voice` | Dictation and voice | its existing header icon | always |
| `caches` | Caches | its existing header icon | personal space with a `cacheProjectId` |

- `useMachinePower` moves up from `ThisMac` into `SpaceSettings`. While the
  status loads, This Mac is in the rail and its pane is empty, so a caller that
  opens it never sees another pane first. A failed load keeps This Mac and shows
  the existing load-error alert in its pane. Only a loaded "unsupported" status
  removes it.
- The personal space's untitled update-status strip moves into the Space
  Settings header, beside the title.
- `SpaceSettings` takes `section: SpaceSettingsSectionId` and
  `onSectionChange(id)`.
- App state: one value replaces `spaceSettingsOpen` and `signInsTarget`:
  `spaceSettings: { section: SpaceSettingsSectionId; signIns?: { provider; host } } | null`.
  `null` means closed. Navigation away closes it, as today.

### Project Settings

Section list in `ProjectSettings.tsx`, today's order:

| id | Label | Icon |
| --- | --- | --- |
| `usage` | Usage | `Gauge` |
| `members` | Members | its existing header icon |
| `repos` | Repos | its existing header icon |
| `machines` | Machines | `Server` |
| `notifications` | Notifications | its existing header icon |
| `consolidation` | Nightly consolidation | its existing header icon |
| `agent-defaults` | Agent defaults | `Bot` |
| `skills` | Skills & workflows | its existing header icon |
| `cache` | Project cache | its existing header icon |

Presence follows each section's existing render condition.

- **Save bar.** The `settings-savebar` stays exactly as today: always rendered,
  page-level, below the pane on every pane, with Save disabled while clean.
  No per-section marks.
- `ProjectSettings` takes `section: string | null` and `onSectionChange(id)`.

### Project Settings route

The human asked for a link per section, so the section rides in the project
hash as `section=<id>` next to `view=settings`.

- **Owner.** App holds `projectSettingsSection: string | null` in React state.
  It is the source of truth; the hash mirrors it.
- **Write.** `ProjectHashSelection` gains `settingsSection?: string`.
  `projectViewHash` writes `section` only when the view is `settings` and the
  selection has it. `changeView(view, selection?: ProjectHashSelection)` passes
  the selection through to `projectHashAfterViewChange(hash, view, selection)`.
  Both use `history.replaceState`, as views do today. Back does not step through
  panes.
- **Read.** Every place App reads the project hash sets the state from
  `parseProjectHash(…).selection.settingsSection ?? null`: the `hashchange`
  handler, the initial project route, and `restoreProjectTabState`.
  `ProjectHashRoute` carries the field.
- **Reset.** Changing project resets it to `null`. Entering Settings from the
  tab strip calls `changeView("settings")` with no selection, which sets `null`
  and opens the first section, also when already on Settings.

### Entry points

| Caller | Today | With the rail |
| --- | --- | --- |
| Gear beside the identity menu (`App.tsx`, `ProjectLanding`) | opens Space Settings | `appearance` |
| `MachinePowerWarning` (local function in `ProjectLanding.tsx`) | opens Space Settings | `this-mac` |
| `openSignIns` (`App.tsx`) | Space Settings plus sign-in target | `machines`, target kept |
| `VoicePanel` `onOpenSettings` (`voiceSurface` in `App.tsx`) | opens Space Settings | `voice` |
| Consolidation inbox blocker (`App.tsx`) | Project Settings, then scrolls to `CONSOLIDATION_SETTINGS_ANCHOR` | `changeView("settings", { settingsSection: "consolidation" })` |
| `GraphViews` notice (includes `ComputeRouteNotice`; one call site in `App.tsx`) | Project Settings | `changeView("settings", { settingsSection: "machines" })` |

- `ProjectLanding`'s `onOpenSpaceSettings` becomes
  `(section?: SpaceSettingsSectionId) => void`. No argument means
  `appearance`.
- `CONSOLIDATION_SETTINGS_ANCHOR` is deleted from `projects/consolidation.ts`,
  its import in `App.tsx`, and the `id` in `ProjectConsolidation.tsx`. The
  scroll call goes. Nothing else uses it.

## Alternatives considered

- **Keep the Project Settings section only in React state, without the hash.**
  Fewer files. Rejected: the human chose a link per section, and project panels
  already name themselves in the route.
- **Remember the last pane per page.** Rejected: an extra stored value, and
  links already cover "go to X".
- **Unmount hidden panes.** Rejected: loses drafts and refetches on every
  switch, which breaks the smooth-motion rule.
- **Push a history entry per pane.** Rejected: views use `replaceState`, and
  Back should leave Settings rather than step through it.
- **A list page with chevrons on phones.** Rejected: it needs a "no section"
  state and its own Back. A scrolling strip reuses an existing pattern.
- **Mark sections with unsaved edits in the rail.** Rejected: it needs the
  single dirty fingerprint split in two, and the always-visible save bar
  already shows unsaved state.
- **Override `--display` with the pixel font.** Rejected: `--editorial` derives
  from it and styles chat and node text.
- **Draw the woods as a React component.** Rejected: it puts theme branching in
  `ProjectLanding`. Two small SVG files keep the theme in CSS.
- **No snowfall.** Rejected: the human chose the direction with it. It is cheap
  (transform on one layer) and stops under reduced motion.

## Risks

- **Contrast.** Covered by the pair list above, asserted in `theme.test.mjs`.
- **Lost entry points.** A caller that still scrolls to an anchor would land on
  the wrong pane. The entry-point table lists every caller; tests cover each.
- **Animation cost in the desktop webview.** Transform-only snowfall on its own
  layer. Check CPU on the served app with the space page left open.
- **Pre-paint drift.** The inline script repeats `normalizeThemeChoice`. A test
  runs the script against each stored theme and checks that it stamps the value
  `normalizeThemeChoice` returns.
- **Paper surfaces.** They follow Snowbound only through the five `--aqua-*`
  tokens. The served-app journey opens Paper and the coach in Snowbound.

## Build order

Each phase leaves the app working.

1. **Snowbound.** `theme.ts`, `index.html`, `main.tsx` (font import),
   `themes/snowbound.css`, two woods SVGs, `styles.css`, `SpaceRuns.tsx`
   palette, `package.json` and the lockfile. The picker is still in the
   identity menu, so the theme is usable.
2. **Rail and Space Settings.** `ui/SettingsRail.tsx` and `.css`,
   `SpaceSettings.tsx` (section list, `useMachinePower` lift, Appearance pane,
   header strip, `section` props), `App.tsx` (merged `spaceSettings` state,
   picker removal), `LandingIdentityMenu.tsx`, `ProjectLanding.tsx`
   (`onOpenSpaceSettings(section?)`, `MachinePowerWarning`),
   `AppearancePicker.tsx`.
3. **Project Settings.** `ProjectSettings.tsx` (section list, `section` props),
   `core/graphTarget.ts`, `experiments/experimentBoardModel.ts`,
   `graph/useGraphSelection.ts`, `App.tsx` (section state, hash read and write,
   entry points), `projects/consolidation.ts`,
   `projects/ProjectConsolidation.tsx`.
4. **Docs.** Move the rules into the two specs: the Snowbound grammar in prose,
   the heading-font exception, the motion sentence, the rail, and Settings
   ownership (display preferences now live in Space Settings). Delete this file.

## Tests

Existing tests to update:

- `theme.test.mjs`: `resolveToken` and the contrast test learn Snowbound; add
  the pair list and the pre-paint parity check.
- `spaceRuns.test.mjs`: the palette-key assertion gains the two Snowbound keys.
- `theme.browser.test.mjs` and `fixtures/appearance.tsx`: the fixture renders
  Space Settings so the test opens Appearance there. Its project-Settings test
  selects the Machines and Agent defaults panes before using Probe and the
  ceiling input.
- `spaceSettings.test.mjs`: `sectionClasses` asserts panes instead of every
  section at once. "The identity menu holds Display" reverses.
- `providerLogins.browser.test.mjs`: the sign-in fold is on the Machines pane.
- `settingsRequestRaces.browser.test.mjs`, `externalJobs.browser.test.mjs`, and
  their fixtures: select the pane before using the ceiling input, Repos,
  Members, cache, or machine tiles.
- `landingIdentity.test.mjs` and `fixtures/agentThreads.tsx`: stop passing
  appearance props.
- `breakpoints.test.mjs`: scans `SettingsRail.css` and `snowbound.css`.

New, behavior only (no wording assertions):

- Choosing a rail item shows only that pane, and a draft in another pane
  survives the switch.
- `section=<id>` selects that pane on Project Settings. An unknown id shows the
  first section. Changing project resets it.
- Each entry-point caller lands on its pane.
- Reopening Space Settings from the gear shows Appearance.
- Opening This Mac while its status loads shows its empty pane, not another pane.

Served-app journey at 1280px and 390px, light and dark, Aqua and Snowbound:

- space page (project view and Pip's thread view), Research, Paper with the
  coach, every Space Settings pane, and every Project Settings pane;
- the identity menu without the picker;
- console and network clean;
- the woods band never overlaps content and never shows in Pip's thread view;
- reduced motion stops the snowfall.

Desktop build: Pixelify Sans renders offline, and Text size works from
Appearance.
