#!/usr/bin/env bash
# Agent containers (one per conversation): limits and current use, CPU as % of their own limit.
ids=$(docker ps -q --filter label=cot.agent=1)
[[ -n "$ids" ]] || { echo "no agent containers running"; exit 0; }
printf '%-34s %8s %9s %9s %16s\n' CONTAINER MAX_CPU CPU MAX_RAM RAM
docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' $ids |
while IFS=$'\t' read -r name cpu mem; do
    nano=$(docker inspect -f '{{.HostConfig.NanoCpus}}' "$name")
    cpus=$(awk -v n="$nano" 'BEGIN{printf "%g", n/1e9}')
    pct=$(awk -v c="${cpu%\%}" -v n="$cpus" 'BEGIN{printf "%.0f%%", (n>0 ? c/n : c)}')
    used=${mem% / *}; max=${mem#* / }
    printf '%-34s %8s %9s %9s %16s\n' "$name" "$cpus" "$pct" "$max" "$used"
done
