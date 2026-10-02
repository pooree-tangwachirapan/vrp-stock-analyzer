# Putting the whole thing online

The analyzer already works three ways: the hosted page on GitHub Pages with
yesterday's numbers, a CSV or inputs file you load by hand, and `python vrp.py`
on your own machine for live data. This is the fourth: the same `vrp.py`
running on a host somewhere, so **any ticker, live, from a phone, with nothing
installed**.

## Why this is the cheap answer

A page on GitHub Pages cannot read CBOE or Yahoo. Browsers refuse cross-origin
requests to hosts that do not send CORS headers, and that is a rule about where
the code runs, not about which language it is written in — Python compiled to
WebAssembly in a browser tab is refused exactly the same way.

Streamlit apps never hit this, and the reason is worth understanding: the Python
runs on a server and the browser only shows the result. The browser never talks
to Yahoo at all.

So the fix is to put *our* Python on a server too. `vrp.py` already serves the
page and the API from one file, so hosting it changes nothing else. No proxy,
no second implementation, no duplicated logic to keep in step.

The only code change this needed was honouring `PORT` from the environment.

## Render — simplest, free, no card

1. Go to **render.com** and sign in with GitHub.
2. **New + → Blueprint.** Not "Web Service" — a Blueprint is what reads
   `render.yaml`, and picking Web Service means filling the same settings in by
   hand.
3. Click **Connect** beside `vrp-stock-analyzer`. Give the Blueprint any name,
   leave the branch on `main` and the Blueprint Path empty — Render looks for
   `render.yaml` at the repo root, which is where it is.
4. Render shows what it is about to create. Click **Deploy Blueprint**.
5. The first build takes a few minutes. The URL looks like
   `https://vrp-analyzer.onrender.com`.

Open it and the ticker box works for anything. The page looks for its helper on
its own origin and finds it, so it behaves exactly as it does on localhost.

Python is pinned to 3.12.10 on purpose. Render now defaults to 3.14, which is
recent enough that numpy and pandas may ship no wheels for it, and pip would
fall back to compiling them during the build.

The settings in `render.yaml` were rehearsed before committing: a clean
virtualenv with only `numpy pandas yfinance` imports everything, the start
command binds `0.0.0.0` on whatever `PORT` says, `/api/health` answers so the
health check passes, and a live RKLB lookup came back with 501 price rows. It
should come up on the first attempt.

**The catch:** free services sleep after about 15 minutes of no traffic, and the
next visit waits roughly 50 seconds while it wakes. For a tool consulted a few
times a day that is a nuisance, not a blocker. Paid tiers remove it.

**One more thing worth knowing:** Render's filesystem is thrown away on each
deploy. Live lookups still append to `fetch/iv_history`, but those rows vanish
with the next build. The history that matters is the one the daily GitHub job
commits, which arrives with the code.

### Deploying on every push

Render's own GitHub integration went quiet here: two commits in a row left the
service on an older build while Auto-Deploy read **On Commit**. Rather than keep
guessing at it, `.github/workflows/deploy.yml` asks Render directly.

One-time setup:

1. Render, your service, **Settings**, find **Deploy Hook** and copy the URL.
2. GitHub, the repo, **Settings → Secrets and variables → Actions → New
   repository secret**. Name it `RENDER_DEPLOY_HOOK` and paste the URL.

The URL carries its own key, which is why it is a secret rather than a line in
the workflow. Without the secret the job prints what to do and exits clean, so
the repo still works for anyone with no Render service.

## Hugging Face Spaces — slower to sleep

1. **huggingface.co → New Space**, SDK **Docker**, visibility public.
2. Push this repo to the Space, or point the Space at the GitHub repo.
3. The `Dockerfile` is already here and listens on 7860, which is what Spaces
   expects.

Spaces stay awake longer than Render's free tier and the free allowance is
generous. The same `Dockerfile` works on Koyeb, Fly.io and Cloud Run.

## GitHub Codespaces — when you want a machine, not a service

`.devcontainer/devcontainer.json` is set up, so **Code → Codespaces → Create**
gives a container with the dependencies installed and port 8765 forwarded
publicly. Good for running the real thing from a borrowed laptop, or for
poking at the Python.

It idles out after 30 minutes, the URL changes each time, and the free
allowance is 60 core-hours a month. A way to run the helper, not a service.

## Which to use

| | Live any ticker | Needs anything running | Wakes instantly |
|---|---|---|---|
| GitHub Pages | no, watchlist only | nothing | yes |
| Render / Spaces | **yes** | the host, free | after a cold start |
| Codespaces | yes | you start it | no |
| `python vrp.py` | yes | your own machine | yes |

The hosted page is still the right default for a daily look: the board is
already scored and loads instantly. Reach for a deployment when you want a name
that is not on the watchlist, or intraday numbers.

## A note on what gets exposed

A public deployment will fetch quotes for anyone who finds the URL. There are no
credentials in it and nothing to write to, so the risk is bandwidth rather than
data. If that matters, Render and Spaces both support private access, or add the
ticker to `fetch/watchlist.txt` instead and let the daily job publish it.
