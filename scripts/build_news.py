name: Daily news

on:
  schedule:
    # GitHub often delays or skips scheduled runs, so we try several times each morning.
    # The script does nothing if today's news already exists.
    - cron: "17 3 * * *"
    - cron: "47 4 * * *"
    - cron: "17 6 * * *"
    - cron: "17 8 * * *"
  workflow_dispatch:       # lets you run it by hand

permissions:
  contents: write
  pages: write

concurrency:
  group: daily-news
  cancel-in-progress: false

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Create app icons (only if missing)
        run: python scripts/make_icons.py

      - name: Build today's news
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          FORCE: ${{ github.event_name == 'workflow_dispatch' && '1' || '' }}
        run: python scripts/build_news.py

      - name: Commit changes
        run: |
          git config user.name "news-bot"
          git config user.email "news-bot@users.noreply.github.com"
          git add data icon-*.png
          if git diff --cached --quiet; then
            echo "No changes"
          else
            git commit -m "Daily news $(date -u +%F)"
            git push
          fi

      - name: Ask GitHub Pages to rebuild
        continue-on-error: true
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api -X POST "repos/${{ github.repository }}/pages/builds"
