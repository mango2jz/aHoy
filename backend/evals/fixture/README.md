# speak-lab (fixture slice)

A vendored slice of a speaking-practice web app: the live examiner session flow
(`src/hooks/liveSession`), the end-of-session gate (`src/utils/endGate.ts`),
pacing metrics (`src/utils/speechMetrics.ts`) and the browser audio helpers
(`src/utils/audio`). Learners talk to an AI examiner; the app measures how long
and how fast they speak and decides when the session may end.

## Tests

No install needed. Tests run on the Node built-in runner:

```bash
npm test        # node --test
```

`test-support/vitest.ts` is a tiny shim so the original vitest-style tests run
on `node:test`.

## Layout

- `src/hooks/liveSession/` session state, flow gate, audio codec, player
- `src/utils/` pure helpers (flow state, end gate, closing detection)
- `src/utils/audio/` PCM, WebM duration, pause metrics, mime selection
- `src/lib/`, `src/libs/` sentence splitting, constants, scenario parsing
