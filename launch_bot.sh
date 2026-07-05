#!/bin/bash
# Auto-restart loop for a Freqtrade trading bot.
# Press Ctrl+C during the 60s countdown to stop completely.

if [ -z "$1" ]; then
    echo "Usage: $0 <config_file.json>"
    echo "The config file should exist in live_configs/"
    exit 1
fi

config_file="$1"

# Stagger startup to avoid thundering herd on ftcache daemon.
# Hash bot config name → deterministic delay (stable across restarts).
# Dry-run configs get longer delay (20-60s) to yield to live bots (0-15s).
hash_val=$(echo -n "$config_file" | md5sum | cut -c1-4)
hash_dec=$((16#$hash_val))
if grep -q '"dry_run": true' "live_configs/$config_file" 2>/dev/null; then
    stagger=$((20 + hash_dec % 40))
else
    stagger=$((hash_dec % 15))
fi

first_start=true

while true
do
    if $first_start && [ "$stagger" -gt 0 ]; then
        echo "Startup stagger: waiting ${stagger}s before first launch (rate limit protection)..."
        sleep "$stagger"
        first_start=false
    fi
    logfile="user_data/logs/${config_file%.json}.log"
    freqtrade trade --config live_configs/"$config_file" --logfile "$logfile"
    echo "You have 60 seconds to press Ctrl+C to stop the bot."
    echo "Restarting in:"
    for i in 60 50 40 30 20 10
    do
        echo "$i..."
        sleep 10
    done
    echo "Restarting!"
done
