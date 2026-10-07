---
name: html-python-render
description: The pattern for a tool that is one HTML page plus one Python file, runnable on a laptop and deployable free to Render so it works from a phone with nothing installed. Use this whenever building or deploying a small web tool that needs to fetch data a browser cannot reach — any time a page must call an API that sends no CORS headers, any time someone asks to put a Python script online, or mentions Render, Hugging Face Spaces, Codespaces, a local helper, "run it on my phone", or GitHub Pages not being able to fetch something. Use it before writing a CORS proxy or porting Python logic to JavaScript, because this pattern usually removes the need for both.
---

# One page, one Python file, deployable anywhere

A tool built this way has two parts and no build step:

- `index.html` — the whole interface and all the logic that can run in a browser
- `app.py` — serves that page **and** exposes `/api/*`, in one file

The same file runs on a laptop and on a host. Nothing is duplicated between
them, there is no bundler, and the page keeps working when the Python is not
running at all.

`templates/` has working copies of everything below. Start from them rather than
retyping. A complete worked example is `Desktop\Cluade_Code\VRP` — see
[[vrp-stock-analyzer]].

## Why this shape and not a proxy

Browsers refuse cross-origin requests to hosts that send no CORS headers. This
is a rule about **where the code runs**, not about which language it is written
in: Python compiled to WebAssembly in a browser tab is refused in exactly the
same way as JavaScript.

That is why a Streamlit app never meets the problem. Its Python runs on a
server and the browser only renders what comes back — the browser never speaks
to the upstream at all.

So when a page cannot read an API, the cheap fix is almost never a CORS proxy
and a JavaScript rewrite of the fetching logic. It is to put the Python that
already works on a server. On one project that choice avoided porting 436 lines
and keeping two implementations in step forever.

## The page and the backend

The page probes for its backend and adapts. It never assumes.

```js
fetch("api/health")          // same origin, so it works on localhost and hosted
  .then(r => r.json())
  .then(j => { state.backendOk = j && j.service === "my-app"; render(); })
  .catch(() => { state.backendOk = false; render(); });
```

Three states, three behaviours, and the page is useful in all of them:

| Backend | What the button does |
|---|---|
| running | fetches live |
| absent, data published beside the page | opens the prepared copy |
| absent, nothing prepared | says what *will* work, naming it |

The last row matters more than it looks. A control that is visible but does
nothing, with a paragraph explaining a browser limitation, reads as an error on
a page where nothing has gone wrong. Make the button do something useful
instead, and keep the explanation for the case that genuinely has no answer.

Never disable the text input while a probe is pending. Someone who starts the
backend afterwards is then stuck until they reload, and they cannot even type
while waiting.

### Long work needs real progress

A frozen button for ten seconds reads as broken, and people click again. Run
the work as a job and let the page poll:

- `GET /api/start?…` → `{id}`, spawns a thread
- `GET /api/progress?id=…` → `{pct, stage, done, payload|error}`

Report actual progress. When downloading, count bytes. If the upstream sends no
`Content-Length` — many CDNs stream chunked — use `got / (got + k)`, which always
advances and never claims a total it does not know.

## The Python file

`templates/app.py` is the skeleton. Four things in it are not obvious:

**Honour `PORT`.** Every host sets it and expects you to listen on every
interface. This is usually the *only* change needed to make a local tool
deployable:

```python
env_port = os.environ.get("PORT")
hosted = bool(env_port)
host = "0.0.0.0" if (hosted or args.lan) else "127.0.0.1"
```

**Serve an allowlist, not a folder.** `SimpleHTTPRequestHandler` pointed at a
directory serves everything in it, including `.git` and a browsable index of
every subdirectory. Probing a live deployment found `/vrp.py`, `/render.yaml`
and `/.git/HEAD` all returning 200 and `/fetch/` listing the tree. Nothing was
secret, but a private file dropped in that folder later would be public the
moment it lands.

**Send `no-cache` for the page.** Otherwise the browser keeps showing the old
`index.html` after a deploy, which is indistinguishable from the deploy having
failed.

**Survive having no console.** Covered below, because it cost the most time.

## Deploying

`templates/render.yaml` is a Blueprint. In Render: **New + → Blueprint**, not
"Web Service" — only a Blueprint reads the file.

Rehearse before pushing. A clean virtualenv with only the listed dependencies,
then `PORT=10000 python app.py`, then check the health path and one real
request. It catches a missing dependency in seconds instead of after a failed
build.

`templates/Dockerfile` covers Hugging Face Spaces, Koyeb, Fly and Cloud Run.
Spaces sleeps less aggressively than Render's free tier, which sleeps after
about 15 minutes and takes roughly 50 seconds to wake.

## Six traps, each one measured

**1. `pythonw.exe` has no console, so `sys.stdout` and `sys.stderr` are `None`.**
Any write raises. If the only writes are in the request path, static files keep
serving while every `/api/` call dies with an empty response — which from the
page is indistinguishable from the backend being absent. It sent someone off
starting a server that was already running, twice. Redirect to a log file when
there is no console. **Testing from a shell will not reproduce it**, because the
shell passes a console through: use
`Start-Process -WindowStyle Hidden` to get the real condition.

**2. A process started by another program dies with it.** Running the server
through an editor or assistant makes it that program's child. Closing the
parent takes it down, and it looks as though the code depends on that program.
Give people a launcher they run themselves, and an autostart entry if they want
it always on.

**3. Render's auto-deploy may simply not fire.** Two pushes in a row left the
service on an older build with Auto-Deploy reading "On Commit". Do not depend
on it: `templates/deploy.yml` calls Render's deploy hook from GitHub Actions,
which is a side you control. Push to live took 75 seconds once wired.

**4. Pin the Python version.** Render now defaults to 3.14. Scientific packages
may ship no wheels for a version that new, turning a two-minute build into pip
compiling numpy from source, or failing. Pin the version you actually rehearsed.

**5. GitHub Pages runs Jekyll, which silently drops files starting with `_`.**
Three of fourteen symbols vanished online while working locally, because index
options are named `_SPX`. An empty `.nojekyll` at the root fixes it.

**6. `scrollIntoView` can do nothing at all.** Called directly on a deployed
page, `scrollY` stayed at 0 while `window.scrollTo` moved fine. Compute the
target and call `scrollTo`, leaving room for any sticky header, and check once
afterwards in case a smooth behaviour was ignored.

## Keeping data fresh without a server

If the tool reads public data and the backend may be asleep, a scheduled job
can publish answers next to the page. GitHub Actions runs Python on its own
machines, where no CORS rule applies, and commits the result; the page then
reads it same-origin and needs nothing running.

Two things to get right:

**Key rows by the session they describe, not by the clock.** GitHub's scheduler
is not punctual — a job set for 21:30 UTC has been seen starting at 01:01 the
next day, three and a half hours late. Derive the date from the data's own
timestamp so a late or weekend run files under the right day.

**Make the files merge cleanly.** Append-only CSV with `merge=union` in
`.gitattributes`, read into a dict keyed by date so a duplicated row collapses
on the next run. Then a scheduled run and a manual one never conflict.

## Before calling it done

Drive the deployed page, not just localhost. Check the health path, one real
request end to end, and the page at 375px wide with no horizontal scroll. Then
try it with the backend stopped, and read what the page says: if it blames the
user for something they have already done, it is wrong.
