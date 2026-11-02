# Design: Snowbound theme and the Settings rail

Status: design for review, in PR #368. Delete this file before merge. Its rules
move into `docs/specs/interface-and-visual-design.md` and
`docs/specs/api-web-and-desktop-projections.md` ("Paper, Settings, and History").

## Goal

1. Add a third theme, **Snowbound**: an 8-bit console look of a cabin in snowy
   woods. Light mode is a snowy day; dark mode is a starry night.
2. Move the display preferences out of the identity menu into Space Settings.
3. Give both settings pages a section rail that shows one pane at a time.

The human chose, from mockups rendered on the real app:

- Snowbound, with its falling snow, over a 16-bit cabin interior and a
  four-shade handheld LCD.
- The picker's new home in Settings, one pane at a time, and the rail on both
  settings pages.
- No link per section: the open section lives in memory, and a reload opens
  the first section.
- The woods strip also shows behind Pip's chat, and the chat window overlays
  it at full height.
- The pixel font on short titles only.

## Non-goals

- The other two pixel directions.
- The woods scene on project pages or the DAG canvas.
- An Appearance pane in Project Settings. Each preference has one home.
- Routes or links for settings sections, remembering the last section, or
  search in Settings.
- Renaming Aqua's `--aqua-*` material tokens.

## Part 1: Snowbound

Extend the theme mechanism from two themes to three. `web/src/ui/theme.ts` owns
the choice. CSS owns the look.

### Theme choice

- `ThemeChoice` becomes `"classic" | "aqua" | "snowbound"`. `THEME_CHOICES`
  lists all three in that order. `themeChoiceLabel` returns "Snowbound".
- `normalizeThemeChoice` keeps each known value and maps unknown values to Aqua.
  Aqua stays the default. Mode stays independent.
- The pre-paint script in `web/index.html` accepts `"snowbound"`. Today it maps
  everything but `"classic"` to Aqua, which would flash Aqua on load. The
  existing parity test in `theme.test.mjs` loops over `THEME_CHOICES`, so it
  covers the new value.

### How Snowbound reaches every surface

Snowbound reuses Aqua's structure and swaps its materials, so no rule set is
copied and nothing drifts:

1. **Shared structure.** Every Aqua selector rule widens from
   `:root[data-theme="aqua"]` to
   `:root:is([data-theme="aqua"], [data-theme="snowbound"])`. That covers
   `themes/aqua.css` and Aqua's rules in `styles/10-settings.css`,
   `styles/12-operations.css`, `styles/14-agent.css`, and
   `ui/AppearancePicker.css`. Aqua's look does not change.
   Aqua's dark rules widen the same way.
2. **Snowbound materials.** `themes/snowbound.css` (imported after `aqua.css`)
   sets the base palette and every `--aqua-*` material token to pixel values.
   Paper (`styles/07-paper.css`, `paper/paper-coach.css`,
   `paper/paper-preview.css`) already reads those tokens, so it follows with no
   edits.
3. **Snowbound overrides.** A short list in `snowbound.css` replaces what Aqua
   hard-codes: gradients become flat fills, and the notched frame and hard
   shadow replace bevels.
4. **Square corners everywhere.** One rule:
   `:root[data-theme="snowbound"] :is(*, ::before, ::after) { border-radius: 0 !important; }`.
   The app has about 370 hard-coded radii in 21 files and no radius token, so
   one theme-wide rule is the only rule that reaches them all. It is the one
   sanctioned `!important` in the theme.

Dark rules use `:root[data-theme="snowbound"][data-color-mode="dark"]`, which
outranks the base `:root[data-color-mode="dark"]`.

### Grammar

- Square corners. Frames are 2px with the four corner pixels knocked out.
- Shadows are hard and unblurred: 4px for controls, 6px for panels and
  popovers, offset down and right.
- Buttons rise 1px on hover and press into their shadow on click, with the
  app's normal short transitions. No stepped timing on controls.
- The selected tab, segment, or rail item is the "lit square": `--px-select`
  inside the notched frame.
- Focus is a 2px dashed outline in the accent color, offset 4px.

### Tokens

These are the build inputs. They live in `snowbound.css`. The spec gets the
grammar in prose, not these values.

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
| `--rose-paper`, `--red-soft` | `#f6dcdc` | `#3a2a40` |
| `--rule` | `#8ea4c4` | `#3a4f86` |
| `--shadow-rgb` | `23 32 58` | `0 0 0` |
| `--px-frame` | `#17203a` | `#04081a` |
| `--px-drop` | `#17203a` | `#04081a` |
| `--px-raise` | `#ffffff` | `#2b3f72` |
| `--px-field` | `#eef3fa` | `#101c3e` |
| `--px-select` | `#ffe9a8` | `#3a3416` |

Derived values, the same in both modes:

```css
--px-notch: 0 -2px 0 0 var(--px-frame), 0 2px 0 0 var(--px-frame),
  -2px 0 0 0 var(--px-frame), 2px 0 0 0 var(--px-frame);
--aqua-surface: var(--sheet);
--aqua-toolbar: var(--sheet);
--aqua-field: var(--px-field);
--aqua-canvas: var(--paper-deep);
--aqua-selected: var(--px-select);
--aqua-selected-border: var(--px-frame);
--aqua-raised: var(--px-notch), 4px 4px 0 2px var(--px-drop);
--aqua-hover: var(--px-notch), 5px 5px 0 2px var(--px-drop);
--aqua-inset: var(--px-notch), inset 2px 2px 0 rgb(var(--shadow-rgb) / 18%);
--aqua-highlight: transparent;
--aqua-sheen: transparent;
--aqua-glow: transparent;
--aqua-focus: var(--oxblood);
--shadow: 6px 6px 0 var(--px-drop);
--segmented-track: var(--px-field);
--segmented-track-shadow: var(--px-notch);
--segmented-selected: var(--px-select);
--segmented-selected-shadow: var(--px-notch);
```

`SPACE_RUN_BADGE_PALETTE` (`experiments/SpaceRuns.tsx`) gains two entries.
Every pair is at least 7:1.

| Tone | `snowbound-light` bg / fg | `snowbound-dark` bg / fg |
| --- | --- | --- |
| running, completed | `#d6efe0` / `#155232` | `#12352a` / `#8ee6a6` |
| waiting, stopping | `#fbefc4` / `#5c4300` | `#3a3416` / `#ffd36e` |
| degraded, actionable | `#f6dcdc` / `#8c1a1e` | `#3e1d24` / `#ff9c84` |
| stopped | `#dfe7f2` / `#33415e` | `#1f2d55` / `#b8c6ea` |

Contrast. `theme.test.mjs` asserts these pairs for both Snowbound modes, at
4.5:1 or better: ink on paper, ink on sheet, soft ink on sheet, accent ink on
the accent, ink on the lit square, and each semantic accent on sheet. The lowest
is 5.45:1.

### Type

- **Pixelify Sans** 600 applies to short titles only, through one allowlist rule
  in `snowbound.css`: the space title (`.space-group-title`), page section
  titles (`.space-runs-header h2`), project tile titles, Settings section
  titles (`.settings-section > header h2`), and the Settings rail labels.
- Everything else stays in Figtree and JetBrains Mono: Overview questions,
  node text, chat (including markdown headings), Paper, and data. `--display`
  and `--editorial` do not change.
- The font is bundled like Figtree: add `@fontsource/pixelify-sans` 5.3.0 to
  `web/package.json` and the lockfile, and import
  `@fontsource/pixelify-sans/600.css` in `main.tsx` beside the Figtree import.
  This is the only new dependency.
- The spec's font sentence ("Every theme sets interface and display text in
  Figtree…") gains the exception.

### The space page scene

`.landing-shell` gets `isolation: isolate`, so the scene layers sit between its
paper and its content.

- **Woods band.** Checked-in art: `themes/snowbound-woods-light.svg` and
  `themes/snowbound-woods-dark.svg` (320×56 pixel grid, about 15 KB each,
  `shape-rendering="crispEdges"`). It renders at 3× (960×168), repeats
  horizontally, and is positioned so the cabin is centered at every width.
  It is pure CSS on `.landing-shell::after`. There is no component and no theme
  branching in React.
  - Projects view: the band follows the content. `.landing-shell` has 176px of
    bottom padding, so the band never covers cards or Runs.
  - Pip's view (`.agent-mode`): the band is fixed to the bottom of the viewport
    behind the chat window. The chat window keeps its full height and overlays
    the band. There is no padding.
- **Snowfall.** Pixel snowflakes drift slowly behind the content. A fixed
  `::before` holds one 96px tile of 2px flakes and animates `transform` on its
  own layer, not `background-position`, so the page is not repainted each
  frame. It runs in both views. `prefers-reduced-motion: reduce` stops it.
  The spec's smooth-motion rule gains one sentence: ambient decoration may move
  continuously when it sits behind content, never shifts layout, and stops
  under reduced motion.

## Part 2: Display preferences move to Space Settings

- `AppearancePicker` (Mode, Theme, and the desktop-only Text size) leaves
  `LandingIdentityMenu`. It becomes the **Appearance** pane of Space Settings.
  Its region label becomes "Appearance" and its doc comment names its new home.
- `SpaceSettings` gains `appearance: AppearancePickerProps` and
  `textScale?: TextScaleControl` props, passed from `App.tsx`.
- Both `LandingIdentityMenu` call sites drop the picker props: the project
  header menu in `App.tsx` and `ProjectLanding.tsx`. `ProjectLanding` keeps
  `palette`, which `SpaceRuns` needs, and drops the rest.
- Storage does not change: it is still a per-device preference in local storage.

## Part 3: the Settings rail

### Shared component

`web/src/ui/SettingsRail.tsx` plus `web/src/ui/SettingsRail.css`.

```ts
interface SettingsSection {
  id: string;
  label: string;
  icon: ReactNode;
  content: ReactNode;
}
interface SettingsRailProps {
  sections: SettingsSection[]; // only the sections that exist
  selected: string | null;     // null, unknown, or absent: the first section
  onSelect: (id: string) => void;
  children?: ReactNode;        // page-level footer, below the pane (save bar)
}
```

- Left: the rail, icon and label, no descriptions. Right: the panes, then
  `children`.
- **Panes stay mounted.** Every pane renders once, and non-selected panes get
  `hidden`. `SettingsRail.css` has `.settings-pane[hidden] { display: none }`,
  so no wrapper `display` overrides it. Drafts (new-machine form, Agent
  defaults, Skills), open dialogs, and loaded data survive a switch, and
  switching never refetches.
- Page-level dialogs (`SkillPackageInspector`, the clear-all-caches and
  lid-closed confirmations) stay where each page renders them today, outside
  the rail.
- The selected item uses `--segmented-selected` and
  `--segmented-selected-shadow`. That is raised in Aqua, the lit square in
  Snowbound, and a plain sheet in Classic.
- At ≤560px the rail becomes a horizontal strip at the top of the page that
  scrolls within its own width, like the Artifacts Files | Paper switch. The pane
  sits below it. There is no list page and no extra Back.
- Scroll: the page that owns the scroll container resets it to the top when
  the section changes. Project Settings scrolls in App's project panel
  (`panelRef`). Space Settings scrolls in its own shell.

### Space Settings

Section list in `SpaceSettings.tsx`, in this order:

| id | Label | Icon | Present when |
| --- | --- | --- | --- |
| `appearance` | Appearance | `Palette` | always |
| `server` | Team server | its existing header icon | team space |
| `this-mac` | This Mac | `Laptop` | `showMachinePowerCard`, or its status failed to load |
| `machines` | Machines | `Server` | always |
| `voice` | Dictation and voice | its existing header icon | always |
| `caches` | Caches | `Database` | personal space with a `cacheProjectId` |

- **This Mac.** `useMachinePower` moves up into `App`, which loads it once at
  startup and passes the status to both `ProjectLanding` (its warning) and
  `SpaceSettings`. The status is therefore known before Settings opens. A failed
  load keeps This Mac in the rail and shows the existing load-error alert in its
  pane. Accepted risk: if Settings opens in the first moment after startup,
  before the status arrives, This Mac can appear in the rail a beat later.
- The personal space's untitled update-status strip moves into the Space
  Settings header, beside the title.
- `SpaceSettings` takes `section` and `onSectionChange(id)`.
- **App state.** One value replaces `spaceSettingsOpen` and `signInsTarget`:
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

- **Save bar.** The `settings-savebar` is passed as the rail's `children`. It
  stays exactly as today: always rendered, below the pane on every pane, with
  Save disabled while clean. When Save is disabled because a field on another
  pane is invalid (the Agent defaults ceiling), the bar's status shows that
  field's existing error with a button that opens its pane. Errors stay
  explicit under the no-commentary rule.
- **State.** App holds `projectSettingsSection: string | null` and passes it
  with `onSectionChange` to `ProjectSettings`. It is not in the URL.
  - App's helper `openProjectSettings(section?)` sets the state, then calls
    `changeView("settings")`. Entry points use this helper.
  - Rail clicks call `onSectionChange`, which sets the state only.
  - It resets to `null` when the view leaves `settings` and when the project
    changes. So the tab strip (`openTab` → `changeView("settings")`) and
    WebMCP always open the first section, and nothing carries over between
    projects.

### Entry points

| Caller | Today | With the rail |
| --- | --- | --- |
| Gear beside the identity menu (`App.tsx`, `ProjectLanding.tsx`) | opens Space Settings | `appearance` |
| `MachinePowerWarning` (local function in `ProjectLanding.tsx`) | opens Space Settings | `this-mac` |
| `openSignIns` (`App.tsx`) | Space Settings plus sign-in target | `machines`, target kept |
| `VoicePanel` `onOpenSettings` (`voiceSurface` in `App.tsx`) | opens Space Settings | `voice` |
| Consolidation inbox blocker (`App.tsx`) | Project Settings, then scrolls to `CONSOLIDATION_SETTINGS_ANCHOR` | `openProjectSettings("consolidation")` |
| `ExecutionView` `onOpenSettings` (`App.tsx`, covers `ComputeRouteNotice` and the graph notice) | Project Settings | `openProjectSettings("machines")` |

- `ProjectLanding`'s `onOpenSpaceSettings` becomes
  `(section?: SpaceSettingsSectionId) => void`. The gear wraps it,
  `onClick={() => onOpenSpaceSettings()}`, so the click event is never taken as
  a section.
- `CONSOLIDATION_SETTINGS_ANCHOR` is deleted from `projects/consolidation.ts`,
  its import in `App.tsx`, and the `id` in `ProjectConsolidation.tsx`, along with
  the scroll call. Nothing else uses it.

## Alternatives considered

- **A link per section in the project hash.** Rejected by the human: it touches
  the shared routing code in three files for reload persistence nobody asked
  for.
- **Copy Aqua's rules into `snowbound.css`.** Rejected: about 69 rule blocks in
  two copies would drift. Widening Aqua's selectors and swapping tokens keeps one
  structure.
- **Unmount hidden panes.** Rejected: it loses drafts and refetches on every
  switch, which breaks the smooth-motion rule.
- **A list page with chevrons on phones.** Rejected: it needs a "no section"
  state and its own Back. A scrolling strip reuses an existing pattern.
- **Mark sections with unsaved edits in the rail.** Rejected: it needs the
  single dirty fingerprint split in two. The always-visible save bar already
  shows unsaved state.
- **Pixel font on every heading, or through `--display`.** Rejected: long
  research questions read slower in it, and `--editorial` derives from
  `--display` for chat and node text.
- **Draw the woods as a React component.** Rejected: it puts theme branching in
  `ProjectLanding`. Two small SVG files keep the theme in CSS.
- **No snowfall.** Rejected: the human chose the direction with it. It is cheap
  (transform on one layer) and stops under reduced motion.

## Risks

- **Contrast.** Covered by the pair list, asserted in `theme.test.mjs` and
  `spaceRuns.test.mjs`.
- **Aqua regressions from widened selectors.** Widening changes only which
  themes match, never Aqua's declarations. The served-app journey runs Aqua too.
- **The universal `!important` radius rule.** It also squares avatars and pills.
  That is intended in a pixel theme. The journey checks that nothing clips.
- **Animation cost in the desktop webview.** The snowfall is transform-only on
  its own layer. Check CPU on the served app with the space page left open.
- **This Mac appearing late.** Accepted, as described above.

## Build order

Each phase leaves the app working.

1. **Snowbound.** `theme.ts`, `index.html`, `main.tsx` (font import), Aqua
   selector widening in the five CSS files, `themes/snowbound.css`, the two
   woods SVGs (already on this branch), `styles.css`, `SpaceRuns.tsx` palette,
   `package.json` and the lockfile. The picker is still in the identity menu,
   so the theme is usable.
2. **Rail and Space Settings.** `ui/SettingsRail.tsx` and `.css`,
   `SpaceSettings.tsx` (section list, Appearance pane, header strip, `section`
   props), `App.tsx` (`useMachinePower` lift, merged `spaceSettings` state,
   picker removal), `LandingIdentityMenu.tsx`, `ProjectLanding.tsx`
   (`onOpenSpaceSettings(section?)`, `MachinePowerWarning`, status prop),
   `AppearancePicker.tsx`.
3. **Project Settings.** `ProjectSettings.tsx` (section list, save bar as
   children, invalid-field jump), `App.tsx` (`projectSettingsSection`,
   `openProjectSettings`, resets, entry points), `projects/consolidation.ts`,
   `projects/ProjectConsolidation.tsx`.
4. **Docs.** Move the rules into the two specs: the Snowbound grammar in prose,
   the title-font exception, the motion sentence, the rail, and Settings
   ownership (display preferences now live in Space Settings). Delete this file.

## Tests

Existing tests to update:

- `theme.test.mjs`: `resolveToken` and the contrast test learn Snowbound; add
  the pair list. The parity loop picks up the new choice.
- `spaceRuns.test.mjs`: the palette-key assertion gains the two Snowbound keys.
- `theme.browser.test.mjs` and `fixtures/appearance.tsx`:
  - The fixture keeps `ProjectLanding` for the badge, card-menu, and narrow
    identity-menu tests. The identity menu still exists, without the picker.
  - The badge test switches theme through stored appearance and a reload,
    instead of the picker. It expects six badge color sets.
  - Picker tests render `SpaceSettings` on its Appearance pane in the same
    fixture.
  - The project-Settings test selects the Machines and Agent defaults panes
    before it uses Probe and the ceiling input.
- `spaceSettings.test.mjs`: `sectionClasses` asserts panes instead of every
  section at once. "The identity menu holds Display" reverses.
- `settingsRequestRaces.browser.test.mjs`, `externalJobs.browser.test.mjs`, and
  their fixtures select the pane before they use the ceiling input, Repos,
  Members, cache, or machine tiles.
- `landingIdentity.test.mjs` and `fixtures/agentThreads.tsx` stop passing
  appearance props.
- `breakpoints.test.mjs` scans `SettingsRail.css` and `snowbound.css`.

New, behavior only, with no wording assertions. They go in
`web/tests/settingsRail.browser.test.mjs` with `fixtures/settingsRail.tsx`, which
renders `SpaceSettings` and `ProjectSettings` against the existing stub API:

- Choosing a rail item shows only that pane, and a draft in another pane
  survives the switch.
- `selected` that is `null` or unknown shows the first section.
- An invalid ceiling on Agent defaults, seen from another pane, shows its error
  in the save bar, and the button opens Agent defaults.

Entry points and resets are App wiring, so the served-app journey checks them.

Served-app journey at 1280px and 390px, light and dark, Aqua and Snowbound:

- the space page (projects view and Pip's view), Research, Paper with the
  coach, every Space Settings pane, and every Project Settings pane;
- each entry point lands on its pane; reopening from the gear shows Appearance;
  switching projects or tabs opens Project Settings on its first section;
- the identity menu without the picker;
- console and network clean;
- the woods band never covers content in the projects view and sits behind the
  chat in Pip's view;
- reduced motion stops the snowfall.

Desktop build: Pixelify Sans renders offline, and Text size works from
Appearance.
