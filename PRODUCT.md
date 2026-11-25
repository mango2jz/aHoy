# Product

<!-- impeccable:product-schema 1 -->

> Drafted from SPEC.md v4 on 2026-11-24 without an interview, at the user's request. Updated for SPEC v5 (hosted, GitHub sign-in, sharing) the same day.
> Lines marked **[Assumption]** are inferred, not confirmed. Everything else restates SPEC.md.

## Platform

web

A hosted web app (D2): a browser UI talking over HTTPS to a Python backend. Users sign in with GitHub (D13). Desktop browser first. **[Assumption]** Desktop only for the MVP. The hold-and-drag wheel gesture assumes a mouse or trackpad, and no tablet or phone layout is planned.

## Stack

Settled by SPEC.md, not delegated: the frontend is React 19 + TypeScript + Vite, the graph is drawn with Cytoscape.js + `cytoscape-fcose`, the wheel is a custom SVG, markdown is rendered with `react-markdown` and code with `shiki`. Design sketches live in `design/sketch/` as static HTML/CSS/SVG and never ship.

## Users

Many users, each signed in with their own GitHub account (D7). Each is the **captain** of their own sessions: a developer reading an unfamiliar small to medium codebase on GitHub (≤500 source files, D8) at their own pace.

- A secondary role is the **viewer**: a teammate who opens a session the owner shared. They need GitHub read access to the same repo, can read everything, and cannot steer (D14).

- **[Assumption]** They are technically literate but often new to the repo's language or domain. The spec's example user does not know what a "list" is, and the system prompt asks the model to define jargon and offer analogies. Explanations must work for a learner.
- **[Assumption]** Sessions are long and quiet: one person at a desk, reading for 30–90 minutes, branching and coming back to earlier nodes. This is reading time, not a monitoring shift.

## Product Purpose

aHoy is a steerable agent harness for reading and explaining a codebase. The human is the captain and the model is the crew. The model does the work (it reads files, searches and runs allowed commands), and the captain decides every direction.

Success means the captain understands the repo through a tree of explanations they chose themselves. They can return to any point and branch again, and the tree is still there after a restart.

## Positioning

Most coding agents are a chat transcript, a single line of turns where the model decides when to stop and ask. aHoy is the opposite:

- **The session is a branchable tree of knowledge nodes ("waypoints"), not a transcript.** Each node's context is only its own path from the root, so sibling branches never leak into each other.
- **The model never stops to ask.** A step always ends in exactly one waypoint or one error node (D9). The only stopping point is a finished waypoint, and the captain picks what comes next.
- **Steering is a physical gesture.** The captain holds a node for at least 300 ms, a radial wheel opens, they drag toward a direction and release. The model's work stays visible: every tool call is a dot on the route.

## Operating Context

- **Sign in:** "Sign in with GitHub". First-time users install the aHoy GitHub App on the repos they choose (read-only access).
- **Launch:** from the session list, "New session" → pick a repo and branch (or paste a tag/SHA). The session is pinned to that commit, and the root "Overview" node appears with no captain input. Private repos show a one-time notice that code goes to Google.
- **Share:** the owner can switch a session to "Shared"; teammates with repo access open the URL view-only.
- **Steer loop:** hold a waypoint, the wheel opens, pick a wedge (Deeper, Zoom out, Show code, Simpler, Custom…, plus up to 4 moves the model proposes, plus Allow blocked… when it applies). A step runs, route dots stream in, and a new waypoint lands.
- **Read:** clicking a waypoint opens the side panel. It shows the title, a plain-language summary, key points, and `file:line` code references that open the cited lines.
- **Command mode:** a per-user switch in the top bar (Read-only / Safe / Auto, cycled with Shift+Tab). The default is Safe. Auto's badge is red.
- **Always on:** the "Remote: Gemini" badge. Everything in the context is sent to Google.

## Capabilities and Constraints

- **Node kinds:** root, waypoint, tool (route dot), blocked (grey dot), error (red). Nodes are append-only and never mutated or deleted.
- **Wheel:** at most 8 wedges. Five fixed verbs, 0–4 model moves (trimmed first), and "Allow blocked…" only when the step had blocked commands. Esc, or releasing in the centre, cancels.
- **Steps:** no modal dialogs during a step. The steered node pulses while dots stream in.
- **Waypoint content:** a title of 8 words or fewer, a summary of 150 words or fewer (markdown allowed), 2–5 key points, code refs (invalid ones get a ⚠ badge), and a confidence of high, medium or low.
- **Safety facts the UI must keep visible:** the mode recorded on each step, blocked commands as visible dots plus a "N command(s) blocked" badge, the remote provider badge, the pinned commit, and "View only" for viewers.
- **Commands run in an isolated sandbox** with no network. Auto mode cannot install dependencies.
- **Access follows GitHub.** If a user loses read access to a repo on GitHub, its sessions disappear for them within minutes.
- **Settings screen** (SPEC → UI): command mode, Safe-mode allowlist, model, sessions you can no longer open (delete only), and account deletion.
- **Undecided:** detailed layout of the session list, repo picker and settings screen, and the code viewer beyond "cited lines highlighted" plus "Open on GitHub".

## Brand Commitments

- **Name:** aHoy, with the lowercase a and capital H as written in SPEC.md.
- **Vocabulary is nautical and binding in copy:** captain, crew, steer, waypoint, route. **[Assumption]** The metaphor lives in words and in the chart-like reading of the graph, never in ornament (no anchors, ropes, ship wheels as decoration, or pirate voice).
- **[Assumption]** Voice: plain, calm and precise. It reads like a good senior engineer explaining things, as the system prompt draft describes.

## Evidence on Hand

- `SPEC.md` (v5.1 draft) and `tasks/` (plan and checklist). No code, screenshots, logos or brand assets exist yet.
- Demonstration content in sketches is synthetic and must be labelled as such. There are no users, testimonials or benchmarks, and none may be invented.

## Product Principles

1. **The captain decides; the crew never interrupts.** Nothing in the UI asks the captain a question mid-step.
2. **Every action of the model is visible.** Tool calls, blocked commands and errors are drawn, not hidden in logs.
3. **A branch is a place, not a version.** Earlier nodes are never edited, so the tree can be trusted as a record.
4. **Risk is stated plainly.** The command mode and the remote provider are always on screen, and Auto looks different from Safe.
5. **Explanations are for learners.** Plain language first, and jargon defined on first use.
6. **Access is never wider than GitHub's.** aHoy only shows code to people who can already read it on GitHub, and only reads repos the user chose.

## Accessibility & Inclusion

- **[Assumption]** Aim for WCAG 2.2 AA.
- The hold gesture needs a keyboard equivalent. **[Assumption]** Focus a node, then press Space or Enter to open the wheel, move with the arrow keys and confirm with Enter, and Esc cancels.
- Node state must never rely on colour alone. Blocked, error and the Auto mode each carry a shape or a label as well.
