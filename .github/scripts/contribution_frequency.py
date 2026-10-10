#!/usr/bin/env python3
"""Variant C: minimal weekly activity line.

Same information as the original smooth-curve card, minus the sloppiness:
straight (honest) segments instead of wobbly Bezier smoothing, no dot field,
no glow stroke, one calm area wash, and only two annotations - the peak week
and the latest week - plus a dashed weekly-average guide.
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

PALETTE = {
    False: {
        "bg": "#ffffff",
        "text": "#24292f",
        "muted": "#57606a",
        "grid": "#d8dee4",
        "border": "#d8dee4",
        "accent": "#2da44e",
        "accent_strong": "#1a7f37",
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
        "accent_strong": "#56d364",
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
            "User-Agent": "github-profile-contribution-line",
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
        "consistency": (100.0 * active_weeks / n) if n else 0.0,
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

    n = len(contributions)
    if week_starts is None or len(week_starts) != n:
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=max(0, n - 1))
        week_starts = [start + timedelta(weeks=i) for i in range(n)]

    stats = compute_stats(daily or [], contributions, week_starts)
    safe_user = xml_escape(str(user))

    width, height = 1100, 404
    left, right, top, bottom = 54, 26, 148, 56
    radius = 16
    plot_w = width - left - right
    plot_h = height - top - bottom

    max_value = max(contributions, default=0)
    ticks, y_top = _nice_ticks(max_value, target=4)
    if y_top <= 0:
        y_top = 1

    def x(i: int) -> float:
        if n <= 1:
            return left + plot_w / 2
        return left + (plot_w * i / (n - 1))

    def y(value: float) -> float:
        return top + plot_h - (plot_h * value / y_top)

    points = [(x(i), y(v)) for i, v in enumerate(contributions)]
    line = f"M {points[0][0]:.2f} {points[0][1]:.2f}" + "".join(
        f" L {px:.2f} {py:.2f}" for px, py in points[1:]
    )
    baseline = top + plot_h
    if len(points) > 1:
        area = (
            f"{line} L {points[-1][0]:.2f} {baseline:.2f} "
            f"L {points[0][0]:.2f} {baseline:.2f} Z"
        )
    else:
        area = ""

    avg_y = y(stats["avg_week"])
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
        f'<desc id="desc">Weekly contribution activity over {window}. '
        f"{stats['total']:,} contributions in this window.</desc>"
    )
    parts.append(
        "<defs>"
        f'<linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{accent}" stop-opacity="0.16"/>'
        f'<stop offset="1" stop-color="{accent}" stop-opacity="0.02"/>'
        "</linearGradient>"
        "</defs>"
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
            f"ACTIVE WEEKS · {stats['consistency']:.0f}%",
        ),
        (
            f"{stats['current_streak']}d streak",
            f"DAY STREAK · BEST {stats['longest_streak']}d",
        ),
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
        f"Best week {stats['best']:,} ({xml_escape(stats['best_label'])}) · "
        f"Last 4 weeks {stats['last4']:,} ({trend_str}) · "
        f"{stats['above_avg']} weeks above average"
    )
    parts.append(f'<text x="{left}" y="112" font-size="12.5" fill="{muted}">{insight}</text>')

    # ---- Grid + y ticks ----
    for tick in ticks:
        yy = y(tick)
        strong = tick == 0
        parts.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width - right}" y2="{yy:.2f}" '
            f'stroke="{grid}" stroke-width="1" opacity="{1.0 if strong else 0.55}"/>'
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

    # ---- Area + line ----
    if area:
        parts.append(f'<path d="{area}" fill="url(#areaFill)"/>')
    if n:
        parts.append(
            f'<path d="{line}" fill="none" stroke="{accent}" stroke-width="2.5" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # ---- Weekly-average guide ----
    if 0 < stats["avg_week"] < max_value:
        parts.append(
            f'<line x1="{left}" y1="{avg_y:.2f}" x2="{width - right}" y2="{avg_y:.2f}" '
            f'stroke="{muted}" stroke-width="1" stroke-dasharray="5 5" opacity="0.55"/>'
        )
        parts.append(
            f'<text x="{width - right - 2}" y="{avg_y - 7:.2f}" font-size="11" '
            f'fill="{muted}" text-anchor="end">avg {stats["avg_week"]:.1f}/wk</text>'
        )

    # ---- Two annotations only: peak + latest ----
    best_idx = contributions.index(max_value) if n and max_value > 0 else -1
    if best_idx >= 0:
        bx, by = points[best_idx]
        parts.append(
            f'<text x="{bx:.2f}" y="{by - 12:.2f}" font-size="12.5" font-weight="800" '
            f'fill="{accent_strong}" text-anchor="middle">{max_value:,}</text>'
        )
        parts.append(
            f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="4.5" fill="{bg}" '
            f'stroke="{accent_strong}" stroke-width="2.5"/>'
        )
    if n:
        lx, ly = points[-1]
        parts.append(
            f'<text x="{lx - 12:.2f}" y="{ly - 12:.2f}" font-size="12.5" font-weight="800" '
            f'fill="{accent_strong}" text-anchor="end">{contributions[-1]:,} this week</text>'
        )
        parts.append(
            f'<circle cx="{lx:.2f}" cy="{ly:.2f}" r="4.5" fill="{bg}" '
            f'stroke="{accent_strong}" stroke-width="2.5"/>'
        )

    # ---- Hover tooltips ----
    step = plot_w / max(1, n - 1)
    for i, value in enumerate(contributions):
        cx, cy = points[i]
        ws = week_starts[i]
        we = ws + timedelta(days=6)
        if value == 1:
            count_text = "1 contribution"
        else:
            count_text = f"{value:,} contributions"
        date_text = f"{ws.strftime('%b %d')} - {we.strftime('%b %d, %Y')}"
        full = f"week of {date_text}: {count_text}"
        card_w = 226.0
        card_h = 44.0
        tx = min(max(cx, left + card_w / 2 + 2), width - right - card_w / 2 - 2)
        above = (cy - (card_h + 14.0)) >= top
        rect_y = cy - card_h - 12.0 if above else cy + 14.0
        hit_w = min(16.0, max(9.0, step / 2 + 1.0))
        parts.append(
            f'<g class="pt" tabindex="0" aria-label="{xml_escape(full)}">'
            f"<title>{xml_escape(full)}</title>"
            f'<rect x="{cx - hit_w:.2f}" y="{top}" width="{hit_w * 2:.2f}" height="{plot_h}" '
            f'fill="transparent"/>'
            f'<line class="guide" x1="{cx:.2f}" y1="{top}" x2="{cx:.2f}" y2="{baseline:.2f}" '
            f'stroke="{muted}" stroke-width="1" stroke-dasharray="3 4" opacity="0.5"/>'
            f'<circle class="focus" cx="{cx:.2f}" cy="{cy:.2f}" r="5" fill="{bg}" '
            f'stroke="{accent}" stroke-width="2.5"/>'
            f'<g class="tip" transform="translate({tx:.2f},{0:.2f})">'
            f'<rect x="{-card_w / 2:.1f}" y="{rect_y:.2f}" width="{card_w:.1f}" height="{card_h}" '
            f'rx="8" fill="{theme["tip_bg"]}" stroke="{theme["tip_border"]}" stroke-width="1"/>'
            f'<text x="0" y="{rect_y + 17:.2f}" font-size="11" fill="{muted}" '
            f'text-anchor="middle">{xml_escape(date_text)}</text>'
            f'<text x="0" y="{rect_y + 34:.2f}" font-size="13" font-weight="800" fill="{text}" '
            f'text-anchor="middle">{value:,} contributions</text>'
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
