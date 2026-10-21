# e2e — showcase render verification (three tiers)

The showcase build produces static HTML; e2e verifies it **renders correctly** (paths,
images, interactions, layout), not merely that "the build runs". Three tiers, different purposes.

## Tier 1 — static-assert (`e2e_static.py`) · BLOCKS CI

Pure Python, no browser, deterministic. Parses `public/**/*.html`:

- every internal `href`/`src` → the file exists in the deployed `public/`
- no unresolved `@key@` tokens left (a leftover link protocol)
- html/img counts

Classification, **hard** (blocks CI, exit 1) vs **soft** (reported, not blocking):
- **hard**: assets (css/js/img/lib), page links `.html`, dangling `@key@` — inside a
  multipage site.
- **soft**: links to source files `.md`/`.yaml`/`.yml`/`.json` (cross-doc to a document the
  site does not host — a content decision); every ref inside the portable file
  `vsf-aio-platform-showcase.html` (self-contained, different asset model).

```
python3 e2e_static.py [--public public] [--strict] [--quiet]
```

`--public` defaults to the top-level `public/` (the real Pages deploy, with `diagram/png`
copied in). `--strict` makes the soft class block too. Wired into the `.gitlab-ci.yml`
`pages` job.

> Note: run it AFTER `build_all.py`/`build_showcase.py` (Step C copies `_diagram/png` →
> `public/diagram/png`).
> Running e2e against `docs/public/` (the intermediate, build.py-only output) misses the
> PNGs → false hard failures.

## Tier 2 — headless Chrome (`e2e_browser.mjs`) · LOCAL PRE-SHIP

puppeteer-core + the system `google-chrome` (no chromium download). Serves `public/` over
a temporary http server, then per page:

- every `<img>` has `naturalWidth>0` (the image really loaded)
- the hero `canvas#net` and the lang toggle (`#btn-en`/`#btn-vi`) exist
- toggling EN/VI switches `html.lang-en`/`.lang-vi`
- sidebar router: clicking a link navigates, no 404
- `console.error == 0` (except benign WebGL noise from the headless GPU-less
  environment — already filtered)

```
# one-time install (gitignored):
npm install --no-save --prefix harness/state/e2e-node puppeteer-core
# run:
NODE_PATH=harness/state/e2e-node/node_modules \
  node e2e_browser.mjs --public public [--shots <dir>]
```

Run it locally before shipping; CI runners usually lack chrome, so CI does not run it. `--shots <dir>` saves a screenshot per page (a manual-inspection artifact).

## Tier 3 — multi-viewport layout (`render_check.mjs` + `render_check.py`) · inside `build_all.py`

Tier 2 is functional (images, lang, router) at ONE viewport.
This tier scans for **horizontal overflow** (`document.documentElement.scrollWidth > viewport width`), the most common sign of a broken responsive layout, at SEVERAL viewports (default `375x667,768x1024,1440x900`) on every page.

`render_check.py` is the preflight and launcher: it measures whether the machine has `node`, a headless browser (`google-chrome`/`chromium`) and an installed `puppeteer-core` BEFORE choosing how to run.

```
python3 render_check.py [--public public] [--viewports 375x667,768x1024,1440x900] [--shots <dir>]
```

Exit: `0` clean · `1` layout findings · `2` Chrome failed to start · `3` this machine CANNOT run it (no node, browser or puppeteer-core), with a one-line reason on `stderr`, never silent.

`build_all.py` runs it as step 5/5, after `build_pdf.py`.
An `exit 3` is reported as `[SKIP]`: not a build failure (a machine without a browser is not a showcase bug), and never read as "checked, clean".
`0`/`1`/`2` count like every other step (OK/FAIL).

## When to run

- **static**: every build (CI + local `_selfcheck`).
- **browser**: before ship / after changing theme/assets/router/lang.
- **render-check**: every `build_all.py` run on a machine with a headless browser; SKIP (non-blocking) elsewhere.
