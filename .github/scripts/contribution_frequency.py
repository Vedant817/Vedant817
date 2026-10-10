#!/usr/bin/env python3
"""Variant B: weekly contribution bars + 4-week moving average.

Weekly totals are drawn as honest vertical bars (no invented smoothing), with
a 4-week moving-average line on top for trend, a peak-week annotation, and
hover tooltips carrying the per-week counts.
"""
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

API_URL = "https://api.github.com/graphql"
WEEKS = 52
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

PALETTE = {
    False: {
        "bg": "#ffffff",
        "text": "#24292f",
        "muted": "#57606a",
        "grid": "#d8dee4",
        "border": "#d8dee4",
        "accent": "#2da44e",
        "accent_strong": "#1a7f37",
        "quiet": "#c8d1da",
        "trend": "#0969da",
        "tip_bg": "#ffffff",
        "tip_border": "#d0d7de",
    },
    True: {
        "bg": "#0d1117",
        "text": "#e6edf3",
        "muted": "#8b949e",
        "grid": "#30363d",
        "border": "#30363d",
        "accent": "#3fb950",
        "accent_strong": "#39d353",
        "quiet": "#3d444d",
        "trend": "#58a6ff",
        "tip_bg": "#161b22",
        "tip_border": "#30363d",
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
            "User-Agent": "github-profile-contribution-bars",
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


def aggregate_weekly(days: list[tuple[date, int]], start: date, weeks: int) -> list[int]:
    totals = [0] * weeks
    for d, count in days:
        index = (d - start).days // 7
        if 0 <= index < weeks:
            totals[index] += count
    return totals


def moving_average(values: list[int], window: int = 4) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i >= window - 1:
            chunk = values[i - window + 1 : i + 1]
            out[i] = sum(chunk) / window
    return out


def compute_stats(
    daily: list[tuple[date, int]], contributions: list[int], week_starts: list[date]
) -> dict:
    n = len(contributions)
    total = sum(contributions)
    avg_week = (total / n) if n else 0.0
    best = max(contributions, default=0)
    best_label = ""
    if n and best > 0:
        best_label = week_starts[contributions.index(best)].strftime("%b %d, %Y")
    active_weeks = sum(1 for v in contributions if v > 0)

    ordered = sorted(daily, key=lambda t: t[0])
    longest = run = 0
    for _, c in ordered:
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

    last4 = sum(contributions[-4:]) if n else 0
    prev4 = sum(contributions[-8:-4]) if n >= 8 else 0
    if prev4 > 0:
        trend_pct = 100.0 * (last4 - prev4) / prev4
    elif last4 > 0:
        trend_pct = 100.0
    else:
        trend_pct = 0.0

    return {
        "total": total,
        "avg_week": avg_week,
        "best": best,
        "best_label": best_label,
        "active_weeks": active_weeks,
        "weeks": n,
        "longest_streak": longest,
        "current_streak": current,
        "last4": last4,
        "prev4": prev4,
        "trend_pct": trend_pct,
        "above_avg": sum(1 for v in contributions if v > avg_week) if n else 0,
    }


def _nice_ticks(max_value: int, target: int = 4) -> tuple[list[int], int]:
    if max_value <= 0:
        return [0], 1
    raw_step = max_value / target
    magnitude = 10 ** math.floor(math.log10(raw_step))
    norm = raw_step / magnitude
    if norm < 1.5:
        step = 1 * magnitude
    elif norm < 3.5:
        step = 2 * magnitude
    elif norm < 7.5:
        step = 5 * magnitude
    else:
        step = 10 * magnitude
    step = int(step) if step >= 1 else step
    top = int(math.ceil(max_value / step) * step)
    count = int(round(top / step))
    ticks = [int(i * step) if step >= 1 else round(i * step, 2) for i in range(count + 1)]
    return ticks, top


def _fmt(n) -> str:
    if isinstance(n, float) and not n.is_integer():
        return f"{n:g}"
    return f"{int(n):,}"


def render_svg(
    user: str,
    contributions: list[int],
    week_starts: list[date] | None = None,
    daily: list[tuple[date, int]] | None = None,
    dark: bool = False,
) -> str:
    theme = PALETTE[bool(dark)]
    bg, text, muted = theme["bg"], theme["text"], theme["muted"]
    grid, border = theme["grid"], theme["border"]
    accent, accent_strong = theme["accent"], theme["accent_strong"]
    quiet, trend = theme["quiet"], theme["trend"]

    n = len(contributions)
    if week_starts is None or len(week_starts) != n:
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=max(0, n - 1))
        week_starts = [start + timedelta(weeks=i) for i in range(n)]

    stats = compute_stats(daily or [], contributions, week_starts)
    ma = moving_average(contributions)
    safe_user = xml_escape(str(user))

    width, height = 1100, 404
    left, right, top, bottom = 54, 20, 148, 56
    radius = 16
    plot_w = width - left - right
    plot_h = height - top - bottom
    step = plot_w / n
    bar_w = min(15.0, step * 0.68)

    max_value = max(contributions, default=0)
    ticks, y_top = _nice_ticks(max_value, target=4)
    if y_top <= 0:
        y_top = 1

    def x(i: int) -> float:
        return left + step * (i + 0.5)

    def y(value: float) -> float:
        return top + plot_h - (plot_h * value / y_top)

    baseline = top + plot_h
    bars: list[tuple[float, float, float, int]] = []  # (x0, y0, h, value)
    for i, value in enumerate(contributions):
        bx = x(i)
        if value > 0:
            bh = max(3.0, (plot_h * value / y_top))
            bars.append((bx - bar_w / 2, baseline - bh, bh, value))
        else:
            bars.append((bx - bar_w / 2, baseline - 2.0, 2.0, value))

    ma_points = [(x(i), y(v)) for i, v in enumerate(ma) if v is not None]
    window = f"{week_starts[0].strftime('%b %Y')} - {week_starts[-1].strftime('%b %Y')}"

    pct = stats["trend_pct"]
    if stats["prev4"] > 0 or stats["last4"] > 0:
        if abs(pct) < 0.5:
            trend_str = "steady vs prior 4 weeks"
        elif pct > 0:
            trend_str = f"+{pct:.0f}% vs prior 4 weeks"
        else:
            trend_str = f"{pct:.0f}% vs prior 4 weeks"
    else:
        trend_str = "no recent trend"

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
    )
    parts.append(f'<title id="title">{safe_user} GitHub contribution activity</title>')
    parts.append(
        f'<desc id="desc">Weekly contribution bars with a 4-week moving average over '
        f"{window}. {stats['total']:,} contributions in this window.</desc>"
    )
    parts.append(
        "<style>"
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;}"
        ".mono{font-family:ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,monospace;}"
        ".pt .tip,.pt .guide{opacity:0;transition:opacity .15s ease-in-out;}"
        ".pt:hover .tip,.pt:focus .tip,.pt:hover .guide,.pt:focus .guide{opacity:1;}"
        ".pt .tip{pointer-events:none;}"
        ".pt{outline:none;}"
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
        "$ commit_stream --trailing 52w</text>"
    )
    parts.append(
        f'<text x="{width - right}" y="34" font-size="12" fill="{muted}" text-anchor="end">'
        f"{xml_escape(window)} · live GitHub data</text>"
    )

    chips = [
        (f"{stats['total']:,}", "TOTAL CONTRIBUTIONS"),
        (f"{stats['avg_week']:.1f}/wk", "WEEKLY AVERAGE"),
        (
            f"{stats['active_weeks']}/{n} wks",
            f"ACTIVE WEEKS · {stats['current_streak']}d streak",
        ),
        (f"{stats['best']:,}", "BEST WEEK · " + stats["best_label"]),
    ]
    chip_w = plot_w / 4
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

    insight = (
        f"Last 4 weeks {stats['last4']:,} ({trend_str}) · "
        f"{stats['above_avg']} of {n} weeks above average"
    )
    parts.append(f'<text x="{left}" y="112" font-size="12.5" fill="{muted}">{insight}</text>')

    # ---- Legend ----
    legend_y = 134
    lx = width - right
    parts.append(
        f'<text x="{lx:.2f}" y="{legend_y + 4}" font-size="11" fill="{muted}" '
        f'text-anchor="end">4-week average</text>'
    )
    lx -= 14 + 6.2 * len("4-week average")
    parts.append(
        f'<path d="M {lx - 30:.2f} {legend_y} L {lx:.2f} {legend_y}" stroke="{trend}" '
        f'stroke-width="2.5" stroke-linecap="round"/>'
    )
    parts.append(
        f'<text x="{lx - 38:.2f}" y="{legend_y + 4}" font-size="11" fill="{muted}" '
        f'text-anchor="end">weekly total</text>'
    )
    parts.append(f'<rect x="{lx - 66}" y="{legend_y - 5}" width="10" height="10" rx="2.5" fill="{accent}"/>')

    # ---- Grid + y ticks ----
    for tick in ticks:
        yy = y(tick)
        strong = tick == 0
        parts.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width - right}" y2="{yy:.2f}" '
            f'stroke="{grid}" stroke-width="{1.25 if strong else 1}" '
            f'opacity="{1 if strong else 0.55}"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{yy + 4:.2f}" font-size="11.5" fill="{muted}" '
            f'text-anchor="end">{_fmt(tick)}</text>'
        )
    parts.append(
        f'<text x="14" y="{top + plot_h / 2:.2f}" font-size="11" fill="{muted}" '
        f'text-anchor="middle" transform="rotate(-90 14 {top + plot_h / 2:.2f})">'
        "contributions / week</text>"
    )

    # ---- Bars ----
    best_idx = contributions.index(max(contributions)) if n else -1
    for i, (bx, by, bh, value) in enumerate(bars):
        if value > 0:
            fill = accent_strong if i == best_idx else accent
            parts.append(
                f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bar_w:.2f}" height="{bh:.2f}" '
                f'rx="3" fill="{fill}"/>'
            )
        else:
            parts.append(
                f'<rect x="{bx:.2f}" y="{by:.2f}" width="{bar_w:.2f}" height="{bh:.2f}" '
                f'rx="1.25" fill="{quiet}"/>'
            )

    # ---- Moving average ----
    if len(ma_points) > 1:
        path = f"M {ma_points[0][0]:.2f} {ma_points[0][1]:.2f}"
        for px, py in ma_points[1:]:
            path += f" L {px:.2f} {py:.2f}"
        parts.append(
            f'<path d="{path}" fill="none" stroke="{trend}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        ex, ey = ma_points[-1]
        parts.append(f'<circle cx="{ex:.2f}" cy="{ey:.2f}" r="4" fill="{trend}"/>')

    # ---- Peak annotation ----
    if n and max_value > 0 and best_idx >= 0:
        bx = x(best_idx)
        by = y(max_value)
        parts.append(
            f'<text x="{bx:.2f}" y="{by - 10:.2f}" font-size="12" font-weight="800" '
            f'fill="{accent_strong}" text-anchor="middle">{max_value:,}</text>'
        )

    # ---- X labels ----
    last_labeled = -10
    for i, ws in enumerate(week_starts):
        label = ""
        if i == 0:
            label = ws.strftime("%b %Y")
        elif ws.month != week_starts[i - 1].month and i - last_labeled >= 4:
            label = ws.strftime("%b")
        elif i == n - 1 and i - last_labeled >= 4:
            label = ws.strftime("%b %d")
        if label:
            last_labeled = i
            anchor = "middle"
            if x(i) - left < 34:
                anchor = "start"
            elif x(i) + 34 > width - right:
                anchor = "end"
            parts.append(
                f'<text x="{x(i):.2f}" y="{baseline + 24:.2f}" font-size="11.5" '
                f'fill="{muted}" text-anchor="{anchor}">{xml_escape(label)}</text>'
            )

    # ---- Hover tooltips ----
    for i, value in enumerate(contributions):
        cx, cy = x(i), y(value)
        ws = week_starts[i]
        we = ws + timedelta(days=6)
        if value == 1:
            count_text = "1 contribution"
        else:
            count_text = f"{value:,} contributions"
        date_text = f"{ws.strftime('%b %d')} - {we.strftime('%b %d, %Y')}"
        full = f"week of {date_text}: {count_text}"
        avg_here = ma[i]
        if avg_here:
            full += f"; 4-week average {avg_here:.0f}"
        card_w = 210.0
        card_h = 44.0
        tx = min(max(cx, left + card_w / 2 + 2), width - right - card_w / 2 - 2)
        above = (cy - (card_h + 14.0)) >= top
        rect_y = cy - card_h - 12.0 if above else cy + 14.0
        parts.append(
            f'<g class="pt" tabindex="0" aria-label="{xml_escape(full)}">'
            f"<title>{xml_escape(full)}</title>"
            f'<rect x="{cx - step / 2:.2f}" y="{top}" width="{step:.2f}" height="{plot_h}" '
            f'fill="transparent"/>'
            f'<line class="guide" x1="{cx:.2f}" y1="{top}" x2="{cx:.2f}" y2="{baseline:.2f}" '
            f'stroke="{accent}" stroke-width="1" stroke-dasharray="3 4" opacity="0.6"/>'
            f'<g class="tip" transform="translate({tx:.2f},{0:.2f})">'
            f'<rect x="{-card_w / 2:.1f}" y="{rect_y:.2f}" width="{card_w:.1f}" height="{card_h}" '
            f'rx="8" fill="{theme["tip_bg"]}" stroke="{theme["tip_border"]}" stroke-width="1"/>'
            f'<text x="0" y="{rect_y + 17:.2f}" font-size="11" fill="{muted}" '
            f'text-anchor="middle">{xml_escape(date_text)}</text>'
            f'<text x="0" y="{rect_y + 34:.2f}" font-size="13" font-weight="800" fill="{text}" '
            f'text-anchor="middle">{value:,}'
            + (
                f'<tspan font-size="11" font-weight="400" fill="{muted}"> · avg {avg_here:.0f}</tspan>'
                if avg_here
                else ""
            )
            + "</text>"
            "</g>"
            "</g>"
        )

    parts.append(
        f'<text x="{width - right}" y="{height - 14}" font-size="11" fill="{muted}" '
        f'text-anchor="end">hover any week for exact counts · refreshes every 12 hours '
        f"from live GitHub data</text>"
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
    contributions = aggregate_weekly(daily, start, WEEKS)
    week_starts = [start + timedelta(weeks=i) for i in range(WEEKS)]

    out = Path("dist")
    out.mkdir(parents=True, exist_ok=True)
    (out / "contribution-frequency.svg").write_text(
        render_svg(user, contributions, week_starts, daily, dark=False), encoding="utf-8"
    )
    (out / "contribution-frequency-dark.svg").write_text(
        render_svg(user, contributions, week_starts, daily, dark=True), encoding="utf-8"
    )

    print(f"Generated {len(contributions)} weekly points from live GitHub data")
    print(f"Total contributions in displayed window: {sum(contributions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
