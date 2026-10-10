#!/usr/bin/env python3
"""Variant A: GitHub-style daily contribution heatmap calendar.

Familiar, readable at a glance, and honest about daily granularity: 53 week
columns x 7 day rows with a five-level green scale, month/day labels, legend,
and native per-day tooltips.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

API_URL = "https://api.github.com/graphql"
WEEKS = 52
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

PALETTE = {
    False: {
        "bg": "#ffffff",
        "text": "#24292f",
        "muted": "#57606a",
        "border": "#d8dee4",
        "levels": ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"],
    },
    True: {
        "bg": "#0d1117",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "border": "#30363d",
        "levels": ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"],
    },
}


def iso_start(d: date) -> str:
    return datetime.combine(d, time.min, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_end(d: date) -> str:
    return (
        datetime.combine(d, time.max, tzinfo=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def graphql(token: str, query: str, variables: dict) -> dict:
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "github-profile-contribution-heatmap",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub GraphQL HTTP {exc.code}: {detail}") from exc
    if body.get("errors"):
        raise RuntimeError("GitHub GraphQL error: " + json.dumps(body["errors"]))
    return body["data"]


def fetch_calendar(token: str, user: str, start: date, end: date) -> list[tuple[date, int]]:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    data = graphql(
        token,
        query,
        {"login": user, "from": iso_start(start), "to": iso_end(end)},
    )
    user_data = data.get("user")
    if not user_data:
        raise RuntimeError(f"GitHub user {user!r} was not found.")
    days: list[tuple[date, int]] = []
    calendar = user_data["contributionsCollection"]["contributionCalendar"]
    for week in calendar["weeks"]:
        for item in week["contributionDays"]:
            d = date.fromisoformat(item["date"])
            if start <= d <= end:
                days.append((d, int(item["contributionCount"])))
    return days


def compute_stats(daily: list[tuple[date, int]]) -> dict:
    ordered = sorted(daily, key=lambda t: t[0])
    counts = {d: c for d, c in ordered}
    total = sum(counts.values())
    active_days = sum(1 for c in counts.values() if c > 0)
    n_days = len(counts)

    longest = run = 0
    for d, c in ordered:
        if c > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0

    current = 0
    idx = len(ordered) - 1
    if idx >= 0 and ordered[idx][1] == 0:
        idx -= 1
    while idx >= 0 and ordered[idx][1] > 0:
        current += 1
        idx -= 1

    busiest_day = max(counts.values(), default=0)
    by_weekday = [0] * 7
    for d, c in ordered:
        by_weekday[d.weekday()] += c
    busiest_weekday = by_weekday.index(max(by_weekday)) if total else 0

    monthly: dict[str, int] = {}
    for d, c in ordered:
        monthly[d.strftime("%b %Y")] = monthly.get(d.strftime("%b %Y"), 0) + c
    best_month = max(monthly, key=monthly.get, default="")
    best_month_total = monthly.get(best_month, 0)

    return {
        "total": total,
        "active_days": active_days,
        "days": n_days,
        "consistency": (100.0 * active_days / n_days) if n_days else 0.0,
        "longest_streak": longest,
        "current_streak": current,
        "best_day": busiest_day,
        "busiest_weekday": WEEKDAYS[busiest_weekday],
        "best_month": best_month,
        "best_month_total": best_month_total,
    }


def intensity_levels(daily: list[tuple[date, int]]) -> tuple[int, int, int]:
    """GitHub-style thresholds: split the user's own active days into quartiles."""
    positives = sorted(c for _, c in daily if c > 0)
    if not positives:
        return (1, 1, 1)

    def quantile(data: list[int], q: float) -> int:
        pos = q * (len(data) - 1)
        lo = int(pos)
        hi = min(lo + 1, len(data) - 1)
        frac = pos - lo
        return int(round(data[lo] * (1 - frac) + data[hi] * frac))

    return (quantile(positives, 0.25), quantile(positives, 0.5), quantile(positives, 0.75))


def render_svg(
    user: str,
    contributions: list[int] | None = None,
    week_starts: list[date] | None = None,
    daily: list[tuple[date, int]] | None = None,
    dark: bool = False,
) -> str:
    if daily is None:
        raise ValueError("heatmap renderer needs daily contribution data")
    counts = {d: c for d, c in daily}
    if not counts:
        raise ValueError("no contribution data to render")

    theme = PALETTE[bool(dark)]
    levels = theme["levels"]
    bg, text, muted, border = theme["bg"], theme["text"], theme["muted"], theme["border"]

    first_day = min(counts)
    last_day = max(counts)
    grid_start = first_day - timedelta(days=(first_day.weekday() + 1) % 7)  # back to Sunday
    span_days = (last_day - grid_start).days + 1
    columns = (span_days + 6) // 7

    stats = compute_stats(daily)
    safe_user = xml_escape(str(user))
    q1, q2, q3 = intensity_levels(daily)

    def level(value: int) -> int:
        if value <= 0:
            return 0
        if value <= q1:
            return 1
        if value <= q2:
            return 2
        if value <= q3:
            return 3
        return 4

    # ---- Geometry ----
    width, height = 1100, 336
    left, right = 46, 14
    radius = 16
    chip_row = 112
    grid_top = 140
    pitch = (width - left - right) / columns
    cell = pitch - 4.0
    grid_left = left
    grid_w = pitch * columns
    baseline_row = grid_top + 7 * pitch

    def cell_xy(d: date) -> tuple[float, float]:
        col = (d - grid_start).days // 7
        row = ((d - grid_start).days) % 7
        return grid_left + col * pitch + 2.0, grid_top + row * pitch + 2.0

    window = f"{first_day.strftime('%b %Y')} - {last_day.strftime('%b %Y')}"
    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
    )
    parts.append(f'<title id="title">{safe_user} GitHub contribution activity</title>')
    parts.append(
        f'<desc id="desc">Daily contribution heatmap from {window}. '
        f"{stats['total']:,} total contributions on {stats['active_days']} active days.</desc>"
    )
    parts.append(
        "<style>"
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;}"
        ".mono{font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;}"
        ".cell{transition:stroke .12s ease-out;}"
        ".cell:hover{stroke:#0969da;stroke-width:1.5;}"
        "</style>"
    )
    parts.append(f'<rect width="{width}" height="{height}" rx="{radius}" fill="{bg}"/>')
    parts.append(
        f'<rect x="0.75" y="0.75" width="{width - 1.5:.2f}" height="{height - 1.5:.2f}" '
        f'rx="{radius - 1}" fill="none" stroke="{border}" stroke-width="1.5"/>'
    )

    # ---- Header ----
    parts.append(
        f'<text x="{left}" y="34" font-size="15" font-weight="700" fill="{text}" class="mono">'
        "$ commit_stream --last-year</text>"
    )
    parts.append(
        f'<text x="{width - right}" y="34" font-size="12" fill="{muted}" text-anchor="end">'
        f"{xml_escape(window)} · live GitHub data</text>"
    )

    chips = [
        (f"{stats['total']:,}", "TOTAL CONTRIBUTIONS"),
        (
            f"{stats['active_days']} / {stats['days']} days",
            f"ACTIVE DAYS · {stats['consistency']:.0f}%",
        ),
        (
            f"{stats['current_streak']}d streak",
            f"CURRENT · BEST {stats['longest_streak']}d",
        ),
        (f"{stats['best_day']:,}", "BUSIEST SINGLE DAY"),
    ]
    chip_w = (width - left - right) / 4
    for idx, (value, label) in enumerate(chips):
        cx = left + idx * chip_w
        parts.append(
            f'<text x="{cx:.2f}" y="70" font-size="21" font-weight="800" fill="{text}">'
            f"{xml_escape(value)}</text>"
        )
        parts.append(
            f'<text x="{cx:.2f}" y="88" font-size="10.5" letter-spacing="1" fill="{muted}">'
            f"{xml_escape(label)}</text>"
        )

    parts.append(
        f'<text x="{left}" y="{chip_row}" font-size="12.5" fill="{muted}">'
        f"Busiest month {xml_escape(stats['best_month'])} ({stats['best_month_total']:,}) · "
        f"most active on {xml_escape(stats['busiest_weekday'])}s</text>"
    )

    # ---- Month labels above the grid (first week of each month changes label) ----
    last_labeled = None
    for col in range(columns):
        week = [grid_start + timedelta(days=col * 7 + r) for r in range(7)]
        if col == 0:
            label_month = week[0].month
        else:
            label_month = next((d.month for d in week if 1 <= d.day <= 7), None)
        if label_month is None or label_month == last_labeled:
            continue
        lx = grid_left + col * pitch + 2.0 + cell / 2
        if lx + 24 <= width - right:
            parts.append(
                f'<text x="{lx:.2f}" y="{grid_top - 10}" font-size="11.5" '
                f'fill="{muted}" text-anchor="middle">{MONTHS[label_month - 1]}</text>'
            )
            last_labeled = label_month

    # ---- Day labels ----
    for row, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        parts.append(
            f'<text x="{grid_left - 8}" y="{grid_top + row * pitch + cell / 2 + 4:.2f}" '
            f'font-size="10.5" fill="{muted}" text-anchor="end">{name}</text>'
        )

    # ---- The calendar ----
    for row in range(7):
        for col in range(columns):
            d = grid_start + timedelta(days=col * 7 + row)
            if not (first_day <= d <= last_day):
                continue
            value = counts.get(d, 0)
            x, y = cell_xy(d)
            fill = levels[level(value)]
            stamp = f"{WEEKDAYS[d.weekday()]}, {d.strftime('%b')} {d.day}, {d.year}"
            if value:
                full = f"{value:,} contributions on {stamp}"
            else:
                full = f"No contributions on {stamp}"
            parts.append(
                f'<rect class="cell" x="{x:.2f}" y="{y:.2f}" width="{cell:.2f}" '
                f'height="{cell:.2f}" rx="3" fill="{fill}"><title>{full}</title></rect>'
            )

    # ---- Legend ----
    legend_y = baseline_row + 26
    parts.append(
        f'<text x="{width - right}" y="{legend_y + 8:.2f}" font-size="11" '
        f'fill="{muted}" text-anchor="end">Less</text>'
    )
    legend_x = width - right - 44
    for i, fill in enumerate(levels):
        parts.append(
            f'<rect x="{legend_x + i * 13}" y="{legend_y}" width="10" height="10" rx="2.5" '
            f'fill="{fill}"/>'
        )
    parts.append(
        f'<text x="{legend_x + 5 * 13 + 6}" y="{legend_y + 8:.2f}" font-size="11" '
        f'fill="{muted}">More</text>'
    )

    parts.append(
        f'<text x="{left}" y="{height - 14}" font-size="11" fill="{muted}">'
        "hover any day for exact counts · refreshes every 12 hours from live GitHub data</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    user = os.environ.get("GITHUB_REPOSITORY_OWNER") or os.environ.get("GITHUB_USER")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 2
    if not user:
        print("GITHUB_REPOSITORY_OWNER or GITHUB_USER is required", file=sys.stderr)
        return 2

    today = datetime.now(timezone.utc).date()
    current_week_start = today - timedelta(days=today.weekday())
    start = current_week_start - timedelta(weeks=WEEKS - 1)

    print(f"Fetching contribution data for {user}: {start} through {today}")
    daily = fetch_calendar(token, user, start, today)

    out = Path("dist")
    out.mkdir(parents=True, exist_ok=True)
    (out / "contribution-frequency.svg").write_text(
        render_svg(user, daily=daily, dark=False), encoding="utf-8"
    )
    (out / "contribution-frequency-dark.svg").write_text(
        render_svg(user, daily=daily, dark=True), encoding="utf-8"
    )

    stats = compute_stats(daily)
    print(f"Generated heatmap from live GitHub data ({stats['total']} contributions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
