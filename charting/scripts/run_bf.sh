cd /srv/skew/em-charts
P="nice -n 19 ionice -c3 /srv/skew/em-galaxy/.venv/bin/python scripts/hist_backfill.py"
$P arbitrum && $P robinhood
