#!/usr/bin/env bash
D="$(cd "$(dirname "$0")" && pwd)"
limit="${2:-40}"; i=0
while [ $i -lt "$limit" ]; do
  [ -f "$D/$1.done" ] && break
  sleep 10; i=$((i+1))
done
if [ -f "$D/$1.done" ]; then echo "$1 exit=$(cat "$D/$1.done")"; else echo "$1 STILL RUNNING"; fi
echo "--- notable ---"
grep -iE 'hypopg|^E:|Unable to locate|Err:|error' "$D/$1.log" 2>/dev/null | tail -6
echo "--- tail ---"
tail -6 "$D/$1.log" 2>/dev/null
