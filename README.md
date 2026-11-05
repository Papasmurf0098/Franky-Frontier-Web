# Franky Frontier

An at-home AI opportunity radar that lives entirely on the web. It checks a short list of AI sources, keeps what you could actually use at home, ranks it, and publishes it as a website.

**Live site: <https://papasmurf0098.github.io/Franky-Frontier-Web/>**

There's nothing to install or keep running. GitHub Actions does the scan every 6 hours, a branch in this repo keeps the history, and GitHub Pages serves the site. The page saves nothing on your device.

Agents working on this repo should read [AGENT.md](AGENT.md) first.

## Set up your own copy

You only need a GitHub account.

1. Publish this repo to GitHub as a **public** repository. Free GitHub Pages only works on public repos, and everything here is public news anyway.
2. Go to **Actions > Scan and publish > Run workflow**. This scans and creates the `gh-pages` branch.
3. Go to **Settings > Pages**. Under **Source** choose **Deploy from a branch**, then pick `gh-pages`. A branch pushed by a workflow may not switch Pages on by itself, so check this even if the site seems close.
4. Run the workflow once more.

The site appears at `https://<your-username>.github.io/<repo-name>/`, and it updates itself from then on.

## What's on the page

Headlines scroll across the top, and the button at the end of that banner pauses them. Below it are the top stories, then the full feed with search and filters, and a side rail with the scan memo, watchlist, tools and source health.

An open page checks for new data every 5 minutes, and whenever you come back to its tab.

## What it reads

Thirteen sources, and none needs an account or API key:

- **Official channels:** OpenAI, Anthropic, Google, Hugging Face, Replicate, Zapier, and Ollama releases.
- **Community:** Hacker News, GitHub, trending GGUF models on Hugging Face, and DEV.
- **Free models:** the OpenRouter model list. Listing is open, but calling a model needs an OpenRouter account.

Anyone can post to community sources, so they rank lower, their cards show a caution, and they need a lot of attention to reach High. Check any project or model yourself before installing it. Franky never opens the pages these results link to.

## How a scan decides

Each source has a schedule of 12 or 24 hours. A scan reads what's new and scores each item on freshness, source trust, usefulness, and hype.

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
| `caution` | Adds a warning to each card |

To add a new API, see [AGENT.md](AGENT.md).

## Run a scan right now

Go to **Actions > Scan and publish > Run workflow**, or use the link in the Source health panel on the site. Only the repo owner can do this. Turn on **force** to check every source, even ones that aren't due.

## If the site stops updating

The page shows a warning when its data is more than 18 hours old. Check these in order:

1. **Actions tab:** look for a failed run and open its log.
2. **Scheduled runs paused:** GitHub turns off schedules in public repos after 60 days without activity. Open **Actions > Scan and publish** and press **Enable workflow**.
3. **Delays:** GitHub can delay scheduled runs when it's busy, so a run or two may come late.

If one source fails, Source health shows why. Some sites block cloud servers, and that source may simply not work from GitHub.

## Where the data lives

| What | Where |
|---|---|
| Scan history and reports | The `scan-state` branch, as readable text, so you can see what changed and when |
| The published site | The `gh-pages` branch, replaced on every run, which GitHub Pages serves |
| Code | The `main` branch |

All of it is public, and it's built only from public sources. Nothing here needs a secret, and no secrets should ever be added.

## Cost

Nothing, within GitHub's limits. Actions is free for standard runners on public repos. Pages has a soft limit of 100 GB of traffic a month and a 1 GB site size, and this site is about 150 KB. GitHub Pages isn't meant for running a commercial service.

## For developers

Tests never touch the internet and need nothing installed:

```bash
python3 -m unittest discover -s tests -t .
```

## License

Provided as-is.
