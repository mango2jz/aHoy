---
name: aHoy
description: A steerable agent harness drawn as a journal phylogeny figure of a branching argument.
colors:
  chart: "oklch(97.2% 0.006 232)"
  chart-grid: "oklch(93.6% 0.008 232)"
  paper: "oklch(99.4% 0.002 240)"
  tint: "oklch(95.6% 0.006 245)"
  code: "oklch(96.6% 0.005 245)"
  highlight: "oklch(91.6% 0.014 250)"
  ink: "oklch(24% 0.028 258)"
  ink-2: "oklch(40% 0.022 258)"
  ink-3: "oklch(49% 0.018 258)"
  ink-4: "oklch(72% 0.012 258)"
  ink-5: "oklch(82% 0.01 258)"
  rule: "oklch(89.5% 0.008 250)"
  route: "oklch(47% 0.165 260)"
  route-tint: "oklch(94.2% 0.032 260)"
  red: "oklch(52% 0.19 27)"
  red-tint: "oklch(95.6% 0.028 27)"
  slate: "oklch(60% 0.012 258)"
  syntax-keyword: "oklch(44% 0.09 205)"
  syntax-string: "oklch(46% 0.1 150)"
  syntax-number: "oklch(50% 0.12 45)"
typography:
  headline:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "22px"
    fontWeight: 700
    lineHeight: 1.22
    letterSpacing: "-0.012em"
  title:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.58
  body-list:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 400
  caption:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "11.5px"
    fontWeight: 400
    lineHeight: 1.45
  mono:
    fontFamily: "Atkinson Hyperlegible Mono, ui-monospace, SF Mono, Menlo, monospace"
    fontSize: "11.5px"
    fontWeight: 400
    lineHeight: 1.6
    fontFeature: "tnum"
  wordmark:
    fontFamily: "Atkinson Hyperlegible Next, ui-sans-serif, system-ui, sans-serif"
    fontSize: "18px"
    fontWeight: 700
    letterSpacing: "-0.01em"
rounded:
  xs: "4px"
  sm: "6px"
  md: "7px"
  lg: "8px"
  pill: "999px"
spacing:
  xxs: "4px"
  xs: "6px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "20px"
  xxl: "28px"
components:
  segmented-track:
    backgroundColor: "{colors.tint}"
    rounded: "{rounded.lg}"
    padding: "2px"
  segmented-option:
    textColor: "{colors.ink-2}"
    rounded: "{rounded.sm}"
    height: "26px"
    padding: "0 12px"
  segmented-option-selected:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
  segmented-option-auto-selected:
    backgroundColor: "{colors.red}"
    textColor: "{colors.paper}"
  icon-button:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink-2}"
    rounded: "{rounded.md}"
    size: "30px"
  pill-auto:
    backgroundColor: "{colors.red-tint}"
    textColor: "{colors.red}"
    height: "18px"
    padding: "0 7px"
  inline-code:
    backgroundColor: "{colors.tint}"
    textColor: "{colors.ink}"
    rounded: "{rounded.xs}"
    padding: "1px 4px"
  code-block:
    backgroundColor: "{colors.code}"
    textColor: "{colors.ink}"
    typography: "{typography.mono}"
    rounded: "{rounded.sm}"
    padding: "8px 0"
  code-line-cited:
    backgroundColor: "{colors.highlight}"
  wheel-wedge:
    backgroundColor: "{colors.paper}"
    textColor: "{colors.ink}"
  wheel-wedge-hover:
    backgroundColor: "{colors.route-tint}"
    textColor: "{colors.route}"
---

# Design System: aHoy

## Overview

**Creative North Star: "The Phylogeny Figure"**

The session is a scientific figure of a branching argument, not a chat log and not a dashboard. The chart is a cool paper ground ruled with a faint graticule; waypoints are inked discs on hairline branches, and each tool call is a small dot along the branch it belongs to. One route, root to the current waypoint, is overprinted in cobalt, the way an orienteering course is printed over terrain without changing it. Everything else on the screen is ink in five weights on two papers.

The figure language carries the chrome. A key in the upper left names the marks (active route, waypoints, tool calls, blocked, error) with the same glyphs the chart uses. Other states are drawn as lettered inset panels (a to e), as a journal figure would. The reading panel has no cards or boxes: type, hairline rules and the cobalt trail alone set hierarchy. The steering wheel is the one object that lifts off the page.

The density is that of a well-set figure caption: small, calm, legible type at 11.5 to 15px, tabular figures for every count, and generous air around the chart. It explicitly rejects the category default, a dark IDE with a purple-glowing node graph and a chat column, and it never uses nautical ornament; the metaphor lives in vocabulary and in the chart-like reading of the graph.

**Key Characteristics:**
- Cool paper chart ground (never cream) with a blue-black ink ramp.
- One reserved cobalt for the active route; one reserved red for error and Auto; slate with a slash for blocked.
- Atkinson Hyperlegible Next and Mono are the only faces.
- Hairline branches, 1px rules, no cards in the reading panel.
- The wheel is the only floating ply.
- Step numbers are shared by the chart, the path trail and the panel, always in tabular mono.

## Colors

A cool, low-chroma paper-and-ink palette in OKLCH, where saturated hue appears only as reserved signal.

### Primary
- **Course Cobalt** (route): the active route only. The root-to-current branch, its tool-call dots, the current and on-route waypoint discs, their step numbers, the path trail in the panel, the route-of-this-step bullets, the hovered wheel wedge's stroke and label, and the step-running inset.
- **Course Wash** (route-tint): the fill of the hovered wheel wedge and the halo stroke behind its label. Never a surface tint elsewhere.

### Secondary
- **Signal Red** (red): error waypoints (outline and exclamation), their labels and the error count, and the Auto command mode (selected segment fill, Auto pill border and text, the triangle glyph).
- **Signal Wash** (red-tint): the fill inside an error disc and the Auto pill. The error branch itself is a Signal Red hairline at 35% stroke opacity.
- **Blocked Slate** (slate): blocked command dots (open circle with a slash), the "N command blocked" badge outline, and the strike-through on an invalid code reference.

### Neutral
- **Chart Paper** (chart): page and canvas ground; also the knock-out stroke around dots, discs and SVG labels so they read cleanly over branches.
- **Graticule** (chart-grid): the 120px grid lines on the chart. Nothing else.
- **Sheet** (paper): the top bar, the reading panel and footer, the wheel face, the selected segment, icon buttons and keycaps.
- **Tint** (tint): the segmented track and inline code chips.
- **Code Paper** (code): code block ground.
- **Cited Line** (highlight): background of cited code lines and text selection.
- **Ink** (ink): primary text, the current waypoint's label, the cursor.
- **Ink 2** (ink-2): secondary text, metadata values, unselected segments, the unread ring.
- **Ink 3** (ink-3): captions, labels, line numbers, off-route waypoint labels and their numbers.
- **Ink 4** (ink-4): off-route waypoint discs and tool-call dots. Graphic only, not for reading text.
- **Ink 5** (ink-5): off-route branch hairlines, scrollbar thumb, link underline.
- **Rule** (rule): every 1px divider and control border, and the wheel's wedge separators.
- **Syntax keyword / string / number**: muted teal, green and amber for code only, held darker than the reserved signals so code never competes with cobalt or red.

### Named Rules
**The Course Layer Rule.** Cobalt marks the one active route and nothing else. If a mark is not on the root-to-current path, or the act of steering it, it is not cobalt.

**The Two Signals Rule.** Red means an error or Auto mode; slate with a slash means blocked. No other hue carries state.

**The Cool Paper Rule.** Every neutral sits at hue 230 to 258 with chroma at or below 0.028. No warm or cream grounds.

## Typography

**Display Font:** none; the wordmark and headline use the body family at weight 700.
**Body Font:** Atkinson Hyperlegible Next (with ui-sans-serif, system-ui)
**Label/Mono Font:** Atkinson Hyperlegible Mono (with ui-monospace, SF Mono, Menlo)

**Character:** One legibility-first family in two widths. The sans sets prose and labels; the mono sets every number, path, command and the figure letters, always with tabular figures. The sans loads as a variable font (weights 200 to 800); "semibold" throughout means 650, and bold means 700.

### Hierarchy
- **Wordmark** (700, 18px, -0.01em): "aHoy" in the top bar only.
- **Headline** (700, 22px, 1.22, -0.012em, balanced wrap): the selected waypoint's title. Inline code in it drops to 0.86em, weight 600.
- **Title** (700, 13.5px): panel section heads ("Key points", "Cited code", "Route of this step").
- **Body** (400, 15px, 1.58, max 66ch): the waypoint summary.
- **Body list** (400, 14px, 1.5): key points, with ink-3 markers.
- **Label** (400, 12.5px): status key, metadata row, chart labels, top-bar controls, footer.
- **Caption** (400, 11.5px, 1.45): inset text, the strip legend, wheel crew caption, sub-labels on the chart.
- **Mono** (400, 10.5 to 12px, tabular): step numbers, counts, times, file paths and line numbers, code (11.5px, 1.6).

### Named Rules
**The Fixed Columns Rule.** Every count, step number, line number and time is set in the mono with tabular figures.

**The One Number Rule.** A waypoint's step number is the same figure in the chart label, the path trail and the panel metadata.

## Layout

The only defined layout is a fixed 1440 by 900 desktop frame (the artifact pins the viewport at 1440). A 48px top bar spans the width. Below it, a 960px canvas column holds the chart (690px tall) above an inset strip, and a 480px reading panel sits to the right behind a 1px rule. The panel scrolls internally with 18px / 28px padding and keeps a footer pinned below.

Overlays sit on the canvas at 16px from its edges: the figure key (panel letter, marks and counts in one line) top left, and the zoom tools top right. The inset strip is three equal columns separated by hairlines, with a full-width caption.

Spacing is a loose 4px-based rhythm of 4, 6, 8, 12, 16, 20 and 28px; 28px is the panel's side gutter. The chart graticule is a 120px grid.

**Undefined:** responsive behavior, narrower windows and any breakpoints are not designed. The artifact is a fixed-size sketch.

## Elevation & Depth

The system is flat. Depth is conveyed by the two papers (chart and sheet), hairline rules, and knock-out strokes in the chart color around marks and labels. The steering wheel is the single lifted object, carried by a two-layer drop shadow so it reads as a dial laid over the figure.

### Shadow Vocabulary
- **Wheel lift** (SVG drop shadows: `0 8px 14px #1b2434 / 0.16` plus `0 1px 1.2px #1b2434 / 0.14`): the steering wheel only.

### Named Rules
**The One Ply Rule.** Only the wheel floats above the canvas. Panels, overlays, insets and buttons sit flat on the page.

## Shapes

Round marks, square-ish chrome. Waypoints are discs sized by weight (6 to 15px radius; the root gets an extra open ring at 55% cobalt). Tool calls are 2.4px dots (3px on the route). The unread waypoint carries a thin open ring until opened. Blocked calls are open slate circles with a diagonal slash; errors are red-washed discs with an exclamation.

Chrome corners are small and tight: 4px for code chips, keycaps and the focus ring; 6px for code blocks and segments; 7px for icon buttons and the key overlay; 8px for the segmented track. Badges, pills, switches and the avatar are fully rounded (pill, 999px; height over two). Keycaps have a 2px bottom border. Icons are 16px outlined strokes at 1.4px with round caps and joins.

## Components

### Command Mode Control
Calm segmented control, red only when it means it.
- **Track:** tint ground, 1px rule border, 8px corners, 2px inset padding.
- **Option:** 26px tall, 12px side padding, 13px ink-2 text, 6px corners; hover lifts text to ink.
- **Selected:** sheet ground, ink text semibold, a 1px rule ring.
- **Auto selected:** red fill with white text and a white triangle glyph. Auto always carries its triangle.
- Paired with a "Shift+Tab" keycap and a "Commands" label in ink-3.

### Icon Buttons
- 30px square, sheet ground, 1px rule border, 7px corners, ink-2 glyph; hover raises the glyph to ink and the border to ink-5.

### Remote Badge
- 30px tall, fully rounded (pill), sheet ground, 1px rule border, outlined cloud-upload glyph, "Remote: **Gemini**" in 12.5px. Always visible in the top bar.

### Session Owner Controls
- **Repo and commit:** `owner/repo` in 14px semibold ink, then `@ sha` in 12px ink-3 mono; the title names the branch the commit came from. The session is pinned to that commit for life.
- **Shared switch:** 32 by 18px pill track on tint with an ink-5 border and a sheet knob; on, the track fills ink. Label "Shared" in 12.5px ink-2. Owners only.
- **Avatar menu:** a 28px ink-2 disc with the user's initials in sheet, 11.5px semibold, plus a chevron; opens Settings and Sign out.
- The owner group sits at the right end of the top bar after a 1px rule.

### Status Pill
- **Queued:** 20px pill, sheet ground, ink-5 border, 11.5px semibold ink-2. Neutral: a queued step is waiting, not failing.
- **View only:** 22px pill, sheet ground, ink-2 border, 11.5px semibold ink with an outlined eye glyph. Shown to viewers of a shared session in place of the mode switch and share toggle, next to the owner's 22px initials disc and "Shared by <login>". Viewers never see the wheel. Neutral ink, never cobalt or red: viewing is a permission, not a state of risk.

### Waypoint Marks (signature)
- **Off-route:** ink-4 disc with a 2px chart knock-out, label in ink-3 with its step number.
- **On route:** cobalt disc; label in ink, weight 600, number in cobalt.
- **Current:** cobalt disc; label weight 700.
- **Error:** red-wash disc, red outline and exclamation; label and sub-label in red ("still steerable").
- **Unread:** an open ink-2 ring around the disc and a "New, not opened" sub-label.
- **Blocked badge:** sheet pill with slate outline, slash glyph and "N command blocked" in ink-2, weight 600.
- Labels sit on the chart with a 4px chart-colored halo (paint-order stroke).

### Steering Wheel (signature)
- A 172px-radius sheet dial with a 48px hub, split into up to eight wedges by rule hairlines. Model moves sit on the north arc under a thin ink-3 band captioned "Proposed by the crew for this waypoint"; fixed verbs (Deeper, Show code, Simpler, Custom…, Zoom out) fill the rest at 13.5px semibold.
- **Hover:** course-wash fill, 1.6px cobalt stroke, cobalt label semibold.
- **Hub:** chart-colored with a rule ring, the current waypoint disc and "Release here to cancel".
- The cursor rests in the hovered wedge, clear of its label; a two-line hint sits below the wheel.

### Path Trail (signature)
- The route as a sounding line: the chart's active route drawn vertically. A thin cobalt line (1.4px at 45% opacity) carries one bead per tool call (2.3px radius, cobalt, with a 1px sheet rim, 7.5px pitch), so each segment's count can be read and its length scales with the calls (4, 3, 6 in the sketch). Waypoints are 10px cobalt points with a 2px sheet ring; step numbers in cobalt mono; titles as links; the steering verb in ink-3 at the right. The current row is ink, semibold, with an extra cobalt ring.
- The context rule is stated once, in the panel footer: "The next step sees only this path. To steer, hold a node or press Space."

### Code Block
- Code-paper ground, 1px rule border, 6px corners, 11.5px mono at 1.6. A 40px line-number gutter in ink-3; cited lines get the highlight ground and an ink, weight 700 line number. Folded ranges show as an ink-3 sans line.

### Code References
- 16px file glyph, path in mono ink, note in ink-3. An invalid reference uses a warning glyph, strikes the path through in slate and says "Not in file … Flagged."

### Inline Code and Keycaps
- **Inline code:** 0.86em mono on tint, 4px corners, 1px 4px padding.
- **Keycap:** 11px mono ink-3 on sheet, 1px rule border with a 2px bottom, 4px corners.

### Figure Key and Insets
- **Key:** a translucent chart-colored overlay (88% chart over the canvas, 7px corners) with the figure letter in mono and each mark's glyph followed by its count.
- **Insets:** a lettered heading (mono letter, 12px semibold title), hairline column separators, and a caption stating the insets are alternate states.

### Focus
- Every focusable element shows a 2px ink outline at 2px offset with 4px corners.

## Do's and Don'ts

### Do:
- **Do** reserve cobalt (route) for the active route and the act of steering it.
- **Do** use red only for error waypoints and Auto mode, and always pair Auto with its triangle glyph.
- **Do** draw blocked commands as slate open circles with a slash, plus a "N command blocked" badge.
- **Do** set every count, step number, line number and time in Atkinson Hyperlegible Mono with tabular figures.
- **Do** carry hierarchy in the reading panel with type and 1px rule lines, not boxes.
- **Do** knock marks and labels out of the branches with a chart-colored stroke.
- **Do** keep the figure key and lettered insets as the way the screen explains itself.

### Don't:
- **Don't** use cobalt for links, buttons, selection or any off-route mark.
- **Don't** cast a shadow on anything but the steering wheel.
- **Don't** put cards or boxed containers in the reading panel.
- **Don't** use a warm, cream or dark ground; the chart is cool paper.
- **Don't** add a typeface beyond Atkinson Hyperlegible Next and Mono.
- **Don't** decorate with nautical ornament (anchors, ropes, ship wheels); the metaphor lives in words.
- **Don't** fall back to the category default of a dark IDE, a glowing purple node graph and a chat column.
