"""Health of the machine the bot runs on (Linux /proc), for /server."""

import asyncio
import os
import shutil
from pathlib import Path


class Unavailable(Exception):
    pass


def _cpu_times() -> list[int]:
    # user nice system idle iowait irq softirq steal
    return [int(x) for x in Path("/proc/stat").read_text().split("\n", 1)[0].split()[1:9]]


def _proc_times() -> dict[int, tuple[str, int]]:
    """Name and CPU ticks used so far, per process."""
    procs = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:  # gone in the meantime
            continue
        name = stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2 :].split()
        procs[int(d.name)] = (name, int(fields[11]) + int(fields[12]))  # utime + stime
    return procs


async def _sample(top: int = 5) -> tuple[dict[str, float], list[str]]:
    """CPU split and busiest processes over one second."""
    a, pa = _cpu_times(), _proc_times()
    await asyncio.sleep(1)
    b, pb = _cpu_times(), _proc_times()
    d = [y - x for x, y in zip(a, b, strict=True)]
    total = sum(d) or 1
    user, nice, system, idle, iowait, irq, softirq, steal = (100 * x / total for x in d)
    cpu = {"busy": user + nice + system + irq + softirq, "iowait": iowait, "steal": steal, "idle": idle}

    # Ticks per process against ticks of one core over the same interval, as top shows it.
    one_core = total / (os.cpu_count() or 1)
    used = sorted(
        ((ticks - pa[pid][1], name) for pid, (name, ticks) in pb.items() if pid in pa),
        reverse=True,
    )
    rows = [f"{100 * t / one_core:>4.0f}%  {name}" for t, name in used[:top] if t > 0]
    return cpu, rows


def _meminfo() -> dict[str, int]:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, _, value = line.partition(":")
        info[name] = int(value.split()[0]) * 1024
    return info


def _gb(n: float) -> str:
    return f"{n / 1024**3:.1f}G"


def _uptime() -> str:
    secs = int(float(Path("/proc/uptime").read_text().split()[0]))
    days, rest = divmod(secs, 86400)
    return f"{days}d {rest // 3600}h" if days else f"{rest // 3600}h {rest % 3600 // 60}m"


def _verdict(cpu: float, mem_avail: float, disk_free: float) -> str:
    if cpu >= 90 or mem_avail < 0.05 or disk_free < 0.02:
        return "🔴 overloaded"
    if cpu >= 70 or mem_avail < 0.15 or disk_free < 0.10:
        return "🟡 busy"
    return "🟢 fine"


async def _containers() -> list[str]:
    """Agent containers with their limits and current use (CPU as % of their own limit)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "--filter", "label=cot.agent=1", "--format", "{{.Names}}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return []
    names = (await proc.communicate())[0].decode().split()
    if not names:
        return []
    proc = await asyncio.create_subprocess_exec(
        "docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *names,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stats = (await proc.communicate())[0].decode().splitlines()
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.Name}} {{.HostConfig.NanoCpus}}", *names,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    inspected = (await proc.communicate())[0].decode().splitlines()
    cpus = {name.lstrip("/"): int(nano) / 1e9 for name, nano in (line.split() for line in inspected)}
    rows = []
    for line in stats:
        name, cpu, mem = line.split("\t")
        limit = cpus.get(name) or 0
        pct = float(cpu.rstrip("%")) / (limit or 1)
        used, _, max_mem = mem.partition(" / ")
        rows.append(f"{name.removeprefix('cot-')}: cpu {pct:.0f}% of {limit:g}, ram {used} / {max_mem}")
    return rows


async def render(paths: list[Path]) -> str:
    if not Path("/proc/stat").exists():
        raise Unavailable("only supported on Linux")
    cores = os.cpu_count() or 1
    load1, load5, load15 = os.getloadavg()
    (cpu, top), agents = await asyncio.gather(_sample(), _containers())
    mem = _meminfo()
    avail, total = mem["MemAvailable"], mem["MemTotal"]
    swap_used = mem["SwapTotal"] - mem["SwapFree"]

    disks, seen, worst = [], set(), 1.0
    for p in paths:
        dev = p.stat().st_dev
        if dev in seen:  # same filesystem
            continue
        seen.add(dev)
        du = shutil.disk_usage(p)
        worst = min(worst, du.free / du.total)
        disks.append(f"disk {p}: {du.used / du.total * 100:.0f}% used, {_gb(du.free)} free")

    lines = [
        # Judged on the CPU right now: the load average lags by minutes.
        f"🖥 Server: {_verdict(100 - cpu['idle'], avail / total, worst)}",
        f"load: {load1:.1f} / {load5:.1f} / {load15:.1f} on {cores} cores",
        f"cpu: {cpu['busy']:.0f}% busy, {cpu['iowait']:.0f}% io wait"
        + (f", {cpu['steal']:.0f}% stolen by the host" if cpu["steal"] >= 1 else ""),
        f"memory: {_gb(total - avail)} / {_gb(total)} used"
        + (f", swap {_gb(swap_used)}" if swap_used > 64 * 1024**2 else ""),
        *disks,
        f"up {_uptime()}",
    ]
    if agents:
        lines += ["", "agent containers:", *agents]
    if top:
        lines += ["", "busiest processes (% of one core):", *top]
    return "\n".join(lines)
