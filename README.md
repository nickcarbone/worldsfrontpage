# World's Front Page

Daily newsletter surfacing front-page stories from 84 publications across 50+ countries — the stories that made headlines somewhere in the world but probably didn't make yours.

## Architecture

```
GitHub Actions (4am ET daily)
  → scraper.py      — fetches headlines from 84 homepages
  → curator.py      — Claude API: translates, selects 10-15 unique stories, writes briefs
  → publisher.py    — assembles and posts draft to Substack
  → You             — review + publish by 6am ET
```

## Setup

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USERNAME/worldsfrontpage.git
cd worldsfrontpage
pip install -r requirements.txt
playwright install chromium
```

### 2. Set GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Value |
|--------|-------|
| `ANTHROPIC_API_KEY` | Your Claude API key from console.anthropic.com |
| `SUBSTACK_EMAIL` | Your Substack login email |
| `SUBSTACK_PASSWORD` | Your Substack password |

### 3. Test locally with dry run

```bash
export ANTHROPIC_API_KEY=your_key_here
cd src
python ../main.py --dry-run
```

This runs the full pipeline (scrape + curate) but prints to terminal instead of posting to Substack.

### 4. Run manually on GitHub

Actions tab → "World's Front Page — Daily Pipeline" → Run workflow → set dry_run = true to test

### 5. Go live

Once dry run looks good, push to main. The pipeline runs automatically at 4am ET every day.
Check your Substack drafts by 5:30am — your draft will be waiting.

## File Structure

```
worldsfrontpage/
├── main.py                    # Orchestration
├── requirements.txt
├── .github/workflows/
│   └── daily.yml              # GitHub Actions schedule
├── src/
│   ├── sources.py             # 84 sources, status labels, flags
│   ├── scraper.py             # Homepage scraping (requests + Playwright)
│   ├── curator.py             # Claude API: translate, select, brief
│   └── publisher.py           # Substack draft assembly + posting
└── logs/                      # Daily JSON logs (gitignored)
    └── YYYY-MM-DD.json
```

## Editorial Notes

- **Baseline sources** (NYT, WSJ, WaPo, FT, Guardian) are scraped for context but never appear as stories — they define what's already globally known
- **State media** sources are included with clear labels — the front page of People's Daily or Granma is itself a journalistic signal
- **Exile publications** (Meduza, The Irrawaddy, Iran International, El Nacional) are labeled as such
- **One story per country** per edition — the LLM enforces this
- **Geographic balance** is a soft preference, not a hard rule — if Southeast Asia is quiet, it's quiet

## Tuning the Curation

Edit the selection prompt in `src/curator.py → _select_stories()` to adjust editorial tone, story criteria, or minimum/maximum story count.

Edit the brief-writing prompt in `_write_single_brief()` to adjust voice, length, or framing style.
