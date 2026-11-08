name: dtek-odessa
on:
  schedule:
    - cron: "*/5 * * * *"   # GitHub может задерживать запуск на несколько минут
  workflow_dispatch:

concurrency: { group: dtek, cancel-in-progress: true }

jobs:
  fetch:
    runs-on: ubuntu-latest
    timeout-minutes: 6
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12", cache: pip }
      - run: pip install -r parser/requirements.txt
      - name: Cache Chromium
        uses: actions/cache@v4
        with: { path: ~/.cache/ms-playwright, key: pw-1.48.0 }
      - run: python -m playwright install --with-deps chromium
      - name: Fetch DTEK
        working-directory: parser
        run: xvfb-run -a python fetch.py --headed > ../schedule.json
      - name: Push to elmon.arestov.net
        run: |
          curl -sS --fail -X POST "https://elmon.arestov.net/api/push.php" \
            -H "X-Elmon-Token: ${{ secrets.ELMON_TOKEN }}" \
            -H "Content-Type: application/json" \
            --data-binary @schedule.json
