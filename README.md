# Franky Frontier

An AI news hub and at-home opportunity radar that lives entirely on the web. It gathers headlines from about 70 AI sources, files them by desk, scores what you could actually use at home, and publishes it all as a website.

**Live site: <https://papasmurf0098.github.io/Franky-Frontier-Web/>**

There's nothing to install or keep running. GitHub Actions does the scan every 3 hours, a branch in this repo keeps the history, and GitHub Pages serves the site. The page saves nothing on your device.

Agents working on this repo should read [AGENT.md](AGENT.md) first.

## Set up your own copy

You only need a GitHub account.

1. Publish this repo to GitHub as a **public** repository. Free GitHub Pages only works on public repos, and everything here is public news anyway.
2. Go to **Actions > Scan and publish > Run workflow**. This scans and creates the `gh-pages` branch. It may fail at its last step, **Ask GitHub Pages to rebuild**, because Pages isn't on yet.
3. Go to **Settings > Pages**. Under **Source** choose **Deploy from a branch**, then pick `gh-pages`.
4. Run the workflow once more.

The site appears at `https://<your-username>.github.io/<repo-name>/`, and it updates itself from then on.

## What's on the page

- **Four moving banners:** the latest headlines across the top, the top of the radar in large type, labs and research, and community and releases. Each has its own pause button, and **Pause banners** in the nav bar stops them all.
- **Front page:** the lead story and **Just in**, the newest headlines from every desk.
- **The news desks:** Labs, Industry, Research, Builders, Community and Releases, newest first, with a button to show more.
- **The radar:** the headlines scored for something you can use at home, with filters, plus the scan memo, watchlist, tools and source health.

The search box in the nav bar searches every headline and the radar at once.

An open page checks for new data every 5 minutes, and whenever you come back to its tab.

## What it reads

About 70 sources, and none needs an account or API key. Each one files its headlines under a desk:

| Desk | Sources |
|---|---|
| Labs | OpenAI, Anthropic, Google, Google DeepMind, Mistral, NVIDIA, Meta Engineering |
| Industry | TechCrunch, The Verge, Ars Technica, MIT Technology Review, WIRED, The Guardian, The Decoder, ZDNET and others |
| Research | Hugging Face daily papers, Google Research, Microsoft Research, Apple, Berkeley, MIT News, Nature |
| Builders | Hugging Face, Replicate, Zapier, n8n, Ollama, AWS, GitHub, Cloudflare, Simon Willison, Latent Space and others |
| Community | Hacker News, new GitHub repos |
| Releases | Trending models, free OpenRouter models, and new versions of Ollama, Open WebUI, ComfyUI and others |

Most sources are **wire only**: their headlines appear on the desks and banners but aren't scored for the radar. Franky shows a headline and a short summary from the publisher's own feed and links to the publisher. It never opens the articles of wire-only sources.

Anyone can post to community sources, so on the radar they rank lower, their cards show a caution, and they need a lot of attention to reach High. Check any project or model yourself before installing it.

## How a scan decides

Each source has its own schedule, from 2 hours for the press to 24 for slow blogs. A scan reads what's new, and everything newer than 60 days goes on the news wire. Headlines stay on the wire for 30 days. Sources that aren't wire only are also scored on freshness, source trust, usefulness, and hype.

| Result | Where it goes |
|---|---|
| High or Medium | Findings |
| Low, including corporate news | Watchlist |
| Speculative, such as "coming soon" or a waitlist | Findings or watchlist, plus Frontier Watch |
| Forum post with no sign you could use it | Set aside, and counted in the memo |
| Older than 60 days | Dropped |

Scoring uses plain keyword rules, so it's predictable but it does make mistakes. A source that fails is shown in Source health and doesn't stop the others. After 3 failed checks in a row, it pauses for 24 hours.

## Change what it scans

Edit [config/sources.json](config/sources.json) on GitHub or anywhere else. Committing to `main` starts a new run. Feeds only need a URL. Page sources also need `article_path`, a pattern that matches their article links.

These optional settings help cut noise:

| Setting | What it does |
|---|---|
| `exclude_keywords` | Drops results containing these words |
| `require_keywords` | Keeps only results containing these words |
| `exclude_title_pattern` | Drops results whose title matches this pattern |
| `require_usable` | Sets aside results with no sign you could use them |
| `desk` | Which desk the headlines file under: Labs, Industry, Research, Builders, Community or Releases |
| `wire_only` | Headlines only. Nothing from this source is scored for the radar |
| `caution` | Adds a warning to each card |

To add a new API, see [AGENT.md](AGENT.md).

## Run a scan right now

Go to **Actions > Scan and publish > Run workflow**, or use the link in the Source health panel on the site. Only the repo owner can do this. Turn on **force** to check every source, even ones that aren't due.

## If the site stops updating

The page shows a warning when its data is more than 12 hours old. Check these in order:

1. **Actions tab:** look for a failed run and open its log.
2. **Scheduled runs paused:** GitHub turns off schedules in public repos after 60 days without activity. Open **Actions > Scan and publish** and press **Enable workflow**.
3. **Delays:** GitHub can delay scheduled runs when it's busy, so a run or two may come late.

A cancelled **pages build and deployment** beside a successful one is normal, and doesn't need fixing.

If one source fails, Source health shows why. Some sites block cloud servers, and that source may simply not work from GitHub.

## Where the data lives

| What | Where |
|---|---|
| Scan history and reports | The `scan-state` branch, as readable text, so you can see what changed and when |
| The published site | The `gh-pages` branch, replaced on every run, which GitHub Pages serves |
| Code | The `main` branch |

All of it is public, and it's built only from public sources. Nothing here needs a secret, and no secrets should ever be added.

## Cost

Nothing, within GitHub's limits. Actions is free for standard runners on public repos. Pages has a soft limit of 100 GB of traffic a month and a 1 GB site size, and this site is under 1 MB. GitHub Pages isn't meant for running a commercial service.

## For developers

Tests never touch the internet and need nothing installed:

```bash
python3 -m unittest discover -s tests -t .
```

## License

Provided as-is.
