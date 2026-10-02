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

1. Go to **render.com**, sign in with GitHub.
2. **New → Web Service**, pick `vrp-stock-analyzer`.
3. Render reads `render.yaml`; accept it and **Create Web Service**.
4. First build takes a few minutes. The URL looks like
   `https://vrp-analyzer.onrender.com`.

Open it and the ticker box works for anything — the page finds the helper on its
own origin, so it behaves exactly as it does on localhost.

**The catch:** free services sleep after about 15 minutes of no traffic, and the
next visit waits roughly 50 seconds while it wakes. For a tool consulted a few
times a day that is a nuisance, not a blocker. Paid tiers remove it.

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
