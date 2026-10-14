name: Alphaliner Weekly Update

on:
  schedule:
    - cron: '0 0 * * 1'
  workflow_dispatch:

permissions:
  contents: write

jobs:
  update:
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Setup Chrome
        uses: browser-actions/setup-chrome@v1

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: Run Crawler
        run: |
          python alphaliner.py --cron

      # DB 파일과 함께 생성된 index.html을 Git에 커밋 & 푸시
      - name: Commit and Push DB and HTML
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "github-actions[bot]@users.noreply.github.com"
          git add alphaliner_history.db index.html
          git diff --quiet && git diff --staged --quiet || (git commit -m "auto: update alphaliner_history.db and index.html" && git push)
