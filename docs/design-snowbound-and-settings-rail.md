# Design: Snowbound theme and the Settings rail

Status: design for review, in PR #368. Delete this file before merge. Its rules
move into `docs/specs/interface-and-visual-design.md` and
`docs/specs/api-web-and-desktop-projections.md` ("Paper, Settings, and History").

## Goal

1. Add a third theme, **Snowbound**: an 8-bit console look of a cabin in snowy
   woods. Light mode is a snowy day; dark mode is a starry night.
2. Move the display preferences out of the identity menu into Space Settings.
3. Give both settings pages a section rail that shows one pane at a time.

The human chose Snowbound over two other pixel directions (a 16-bit cabin
interior and a four-shade handheld LCD) from mockups rendered on the real app.
The human also chose the picker's new home, one pane at a time, and the rail on
both settings pages.

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
- It restyles the surfaces Aqua restyles. That includes Aqua's rules outside
  `aqua.css`: `styles/10-settings.css`, `styles/12-operations.css`, and
  `ui/AppearancePicker.css`.

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
| `--mustard` | `#c88a00` | `#ffd23f` |
| `--coral` | `#d0442c` | `#ff7a5c` |
| `--indigo` | `#3a4fa0` | `#9fb0ff` |
| `--plum` | `#7a3a8a` | `#e09bf0` |
| `--forest` | `#1c6b3c` | `#6ee07a` |
| `--moss` | `#4f7a22` | `#b8e05a` |
| `--slate` | `#48668c` | `#8fb0e0` |
| `--rose-paper` | `#f6dcdc` | `#3a2a40` |
| `--red-soft` | `#f6dcdc` | `#3a2a40` |
| `--rule` | `#8ea4c4` | `#3a4f86` |
| `--shadow-rgb` | `23 32 58` | `0 0 0` |
| frame | `#17203a` | `#04081a` |
| drop shadow | `#17203a` | `#04081a` |
| raised control | `#ffffff` | `#2b3f72` |
| field | `#eef3fa` | `#101c3e` |
| lit square | `#ffe9a8` | `#3a3416` |

`theme.test.mjs` already checks contrast for Classic and Aqua. It covers both
Snowbound palettes the same way. Values that fail are adjusted, not excused.

### Type

- Headings (`--display`) use **Pixelify Sans**, weight 600. Body text, labels,
  and data keep the existing UI and mono faces. Node text, chat, and tables read
  as they do today.
- The desktop app runs offline, so the font is bundled. Add
  `@fontsource/pixelify-sans` 5.3.0 to `web/package.json` and the lockfile.
  `snowbound.css` imports `@fontsource/pixelify-sans/600.css`. This is the only
  new dependency.

### The space page scene

The space page's project view (`.landing-shell` without `.agent-mode`) shows the
scene. Pip's thread view, project pages, and the DAG canvas stay plain so work
surfaces stay calm.

- **Woods band.** A pixel strip of snowy pines and a cabin (lit window, chimney
  smoke) sits at the bottom of the space page, after the content. It is pure
  CSS: `.landing-shell:not(.agent-mode)::after` with a background SVG, and
  padding on `.landing-shell` so it never covers content. The cabin is centered
  at every width and the pines repeat to fill the sides. Two checked-in files,
  `themes/snowbound-woods-light.svg` and `themes/snowbound-woods-dark.svg`, each
  under 20 KB, using `shape-rendering="crispEdges"`. No component, no theme
  branching in React.
- **Snow dots.** A sparse 2px dot pattern as a `background-image` on
  `.landing-shell`. Color tokens stay plain hex.
- **Snowfall.** Pixel snowflakes drift slowly behind the space page. A fixed
  pseudo-element on its own layer animates `transform`, not
  `background-position`, so the page is not repainted each frame.
  `prefers-reduced-motion: reduce` stops it. This is the theme's one continuous
  animation. The spec's smooth-motion rule gains one sentence: ambient
  decoration may move continuously when it sits behind content, never shifts
  layout, and stops under reduced motion.

## Part 2: Display preferences move to Space Settings

- `AppearancePicker` (Mode, Theme, and the desktop-only Text size) leaves
  `LandingIdentityMenu`. It becomes the **Appearance** pane of Space Settings.
- `SpaceSettings` gains `appearance: AppearancePickerProps` and
  `textScale?: TextScaleControl` props, passed from `App.tsx`.
- Both `LandingIdentityMenu` call sites drop the picker props: `App.tsx` (the
  project header menu) and `ProjectLanding.tsx`. `ProjectLanding` stops taking
  appearance props.
- Storage does not change: it is still a per-device preference in local storage.
- Space Settings opens from the gear beside the identity menu, on the space page
  and inside projects.
- Project Settings has no Appearance pane. Each preference has one home.

## Part 3: the Settings rail

### Shared component

`web/src/ui/SettingsRail.tsx` plus `web/src/ui/SettingsRail.css`.

```ts
interface SettingsSection {
  id: string;          // fixed, owned by the page's list
  label: string;
  icon: ReactNode;
  visible: boolean;    // decided by the page from data it already has
  render: () => ReactNode;
}
// <SettingsRail sections={…} selected={id} onSelect={(id) => …} marks={Set<id>} />
```

- Left: a rail of the visible sections, icon and label, no descriptions.
  Right: the panes.
- **Panes stay mounted.** Each pane renders once and non-selected panes get
  `hidden`. Drafts (new-machine form, Agent defaults, Skills), open dialogs, and
  loaded data survive a pane switch. Switching never flickers or refetches.
- The selected item uses the theme's segmented-selected look: raised in Aqua,
  lit square in Snowbound, plain sheet in Classic.
- `marks` puts a small dot on rail items with unsaved edits.
- If `selected` names a missing or invisible section, the first visible section
  is shown.
- At ≤560px the rail becomes a horizontal strip at the top of the page that
  scrolls within its own width, like the Artifacts Files | Paper switch. The pane
  is below it. One state model; no list page and no extra Back.

### Space Settings

Section list in `SpaceSettings.tsx`, in this order:

| id | Label | Icon | Visible when |
| --- | --- | --- | --- |
| `appearance` | Appearance | `Palette` | always |
| `server` | Team server | its existing header icon | team space |
| `this-mac` | This Mac | `HardDrive` | `showMachinePowerCard(spaceKind, status)` |
| `machines` | Machines | `Server` | always |
| `voice` | Dictation and voice | its existing header icon | always |
| `caches` | Caches | its existing header icon | personal space with a `cacheProjectId` |

- `useMachinePower` moves up from `ThisMac` into `SpaceSettings` so the list
  knows whether This Mac exists. Until its status loads, This Mac is hidden. A
  caller that targets it falls back to the first section until then.
- The personal space's untitled update-status strip moves into the Space
  Settings header, beside the title.
- App state: one value replaces `spaceSettingsOpen` and `signInsTarget`:
  `spaceSettings: { section: string; signIns?: { provider; host } } | null`.
  `null` means closed. Every open sets a section; the gear opens `appearance`.
  Closing resets it, as `signInsTarget` resets today.

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
| `lessons` | Lessons | `NotebookPen` |
| `agent-defaults` | Agent defaults | `Bot` |
| `skills` | Skills & workflows | its existing header icon |
| `cache` | Project cache | its existing header icon |

Visibility follows each section's existing render condition.

- **Save bar.** The single `settings-savebar` stays page-level, below the pane,
  on every pane. It shows while Agent defaults or Skills are dirty and for the
  status messages it already reports (Repos writable toggle, cache clear).
  The rail marks `agent-defaults` and `skills` while they hold unsaved edits.
- **Route.** The section rides in the project hash as `section=<id>`, next to
  `view=settings`:
  - `ProjectHashSelection` gains `settingsSection?: string`.
    `projectViewHash` writes it; `parseProjectHash` and `ProjectHashRoute`
    read it.
  - `projectHashAfterViewChange` keeps it only when the view stays `settings`.
  - `changeView` takes an optional selection:
    `changeView(view, { settingsSection })`.
  - A pane change uses `history.replaceState`, like view changes today. Browser
    Back does not step through panes.
  - `ProjectSettings` takes `section` and `onSectionChange` props from `App`.
  - Entering Settings from the tab strip, or through `webmcp/view.ts`
    `PROJECT_TABS.settings`, opens the first section. Settings does not
    remember the last section.

### Entry points

| Caller | Today | With the rail |
| --- | --- | --- |
| Gear beside the identity menu (`App.tsx`, `ProjectLanding`) | opens Space Settings | `appearance` |
| `MachinePowerWarning` (space page) | opens Space Settings | `this-mac` |
| `openSignIns` (`App.tsx`) | Space Settings plus sign-in target | `machines`, target kept |
| `VoicePanel` | opens Space Settings | `voice` |
| Consolidation inbox blocker (`App.tsx`) | Project Settings, then scrolls to `CONSOLIDATION_SETTINGS_ANCHOR` | `section=consolidation`; the scroll goes. The anchor id stays only if another caller uses it. |
| `GraphViews` notice (includes `ComputeRouteNotice`; one call site in `App.tsx`) | Project Settings | `section=machines` |

## Alternatives considered

- **Remember the last pane per page.** Rejected: an extra stored value, and
  deep links already cover "go to X".
- **Unmount hidden panes.** Rejected: loses drafts and refetches on every
  switch, which breaks the smooth-motion rule.
- **Push a history entry per pane.** Rejected: views use `replaceState`; panes
  should behave like views, and Back should leave Settings rather than step
  through it.
- **A list page with chevrons on phones.** Rejected after review: it needs a
  "no section" state and its own Back. A scrolling strip reuses an existing
  pattern.
- **Draw the woods as a React component with CSS-variable fills.** Rejected:
  it puts theme branching in `ProjectLanding`. Two small SVG files keep the
  theme in CSS.
- **Load Pixelify Sans from Google Fonts.** Rejected: the desktop app runs
  offline.
- **No snowfall.** Considered, since it is the one continuous animation. Kept
  because it is the scene's single moving element and the human chose the
  direction with it. It is cheap (transform on one layer) and stops under
  reduced motion.

## Risks

- **Contrast.** Gold-on-red and amber-on-navy pairs can fail. The contrast
  test covers both palettes.
- **Lost entry points.** A caller that still scrolls to an anchor would land
  on the wrong pane. The entry-point table lists every caller; tests cover each
  one.
- **Animation cost in the desktop webview.** Transform-only snowfall on its own
  layer. Check CPU on the served app with the space page left open.
- **Pre-paint drift.** The inline script repeats `normalizeThemeChoice`. A test
  runs the script against each stored theme and checks that it stamps the same
  value `normalizeThemeChoice` returns.

## Build order

Each phase leaves the app working.

1. **Snowbound.** `theme.ts`, `index.html`, `themes/snowbound.css`, two woods
   SVGs, `styles.css`, `SpaceRuns.tsx` palette, `package.json` and the
   lockfile. Tests: `theme.test.mjs` (normalize, contrast, pre-paint parity).
   The picker is still in the identity menu, so the theme is usable.
2. **Rail and Space Settings.** `ui/SettingsRail.tsx` and `.css`,
   `SpaceSettings.tsx` (section list, `useMachinePower` lift, Appearance pane,
   header strip), `App.tsx` (merged `spaceSettings` state, picker removal),
   `LandingIdentityMenu.tsx`, `ProjectLanding.tsx`, `MachinePowerWarning`,
   `VoicePanel`.
3. **Project Settings.** `ProjectSettings.tsx` (section list, save bar,
   marks), `core/graphTarget.ts`, `experiments/experimentBoardModel.ts`,
   `graph/useGraphSelection.ts`, `App.tsx` (hash route, entry points),
   `projects/ProjectConsolidation.tsx`, `webmcp/view.ts`.
4. **Docs.** Move the rules into the two specs (grammar in prose, rail and
   Settings ownership, the motion sentence). Delete this file.

## Tests

Existing tests to update:

- `theme.browser.test.mjs` opens Display from the identity menu, and its
  project-Settings test uses Probe (Machines) and the ceiling input (Agent
  defaults). It opens Space Settings › Appearance and selects those panes.
- `spaceSettings.test.mjs`: `sectionClasses` expects every section at once.
  It asserts panes instead. "The identity menu holds Display" reverses.
- `providerLogins.browser.test.mjs`: the sign-in fold is on the Machines pane.
- `theme.test.mjs`: `resolveToken` and the contrast test learn Snowbound.

New, behavior only (no wording assertions):

- Choosing a rail item shows only that pane, and a draft in another pane
  survives the switch.
- `section=<id>` selects that pane on Project Settings. An unknown or invisible
  id shows the first visible section.
- Each entry-point caller lands on its pane.
- Reopening Space Settings from the gear shows Appearance.
- The save bar appears on any pane while Agent defaults is dirty.

Served-app journey at 1280px and 390px, light and dark, Aqua and Snowbound:

- space page (project view and Pip's thread view), Research, every Space
  Settings pane, and every Project Settings pane;
- the identity menu without the picker;
- console and network clean;
- the woods band never overlaps content and never shows in Pip's thread view;
- reduced motion stops the snowfall.

Desktop build: Pixelify Sans renders offline, and Text size works from
Appearance.
