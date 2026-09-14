#!/usr/bin/env python3
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
            "User-Agent": "github-profile-contribution-frequency",
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
    daily: list[tuple[date, int]],
    contributions: list[int],
    week_starts: list[date],
) -> dict:
    """Derive recruiter-friendly productivity stats from daily + weekly data."""
    n = len(contributions)
    total = sum(contributions)
    avg_week = (total / n) if n else 0.0
    best = max(contributions, default=0)
    best_label = ""
    if n and best > 0:
        best_label = week_starts[contributions.index(best)].strftime("%b %d, %Y")

    active_weeks = sum(1 for v in contributions if v > 0)
    consistency = (100.0 * active_weeks / n) if n else 0.0

    ordered = sorted(daily, key=lambda t: t[0])
    active_days = sum(1 for _, c in ordered if c > 0)

    longest = 0
    run = 0
    for _, c in ordered:
        if c > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0

    # Current streak ends today; if today is still blank, measure through yesterday
    # so an in-progress day does not zero out an active streak.
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

    above_avg = sum(1 for v in contributions if v > avg_week) if n else 0

    return {
        "total": total,
        "avg_week": avg_week,
        "best": best,
        "best_label": best_label,
        "active_weeks": active_weeks,
        "weeks": n,
        "consistency": consistency,
        "active_days": active_days,
        "longest_streak": longest,
        "current_streak": current,
        "last4": last4,
        "prev4": prev4,
        "trend_pct": trend_pct,
        "above_avg": above_avg,
    }


def smooth_path(points: list[tuple[float, float]], tension: float = 0.22) -> str:
    """Return a smooth cubic Bezier path that still passes through every data point."""
    if not points:
        return ""
    if len(points) == 1:
        x, y = points[0]
        return f"M {x:.2f} {y:.2f}"

    parts = [f"M {points[0][0]:.2f} {points[0][1]:.2f}"]

    for i in range(len(points) - 1):
        p0 = points[i - 1] if i > 0 else points[i]
        p1 = points[i]
        p2 = points[i + 1]
        p3 = points[i + 2] if i + 2 < len(points) else p2

        c1x = p1[0] + (p2[0] - p0[0]) * tension
        c1y = p1[1] + (p2[1] - p0[1]) * tension
        c2x = p2[0] - (p3[0] - p1[0]) * tension
        c2y = p2[1] - (p3[1] - p1[1]) * tension

        # Keep control points inside the local vertical range so smoothing does not
        # invent exaggerated peaks or dips between real weekly values.
        local_min = min(p1[1], p2[1])
        local_max = max(p1[1], p2[1])
        c1y = min(max(c1y, local_min), local_max)
        c2y = min(max(c2y, local_min), local_max)

        parts.append(
            f"C {c1x:.2f} {c1y:.2f}, {c2x:.2f} {c2y:.2f}, {p2[0]:.2f} {p2[1]:.2f}"
        )

    return " ".join(parts)


def _nice_ticks(max_value: int, target: int = 4) -> tuple[list[int], int]:
    """Return human-friendly y ticks and the axis top for a given max value."""
    if max_value <= 0:
        return [0], 1
    if max_value <= target:
        top = target
        return list(range(top + 1)), top
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
    """Smooth productivity curve with a crisp dot on every spike.

    Dots are small hollow markers drawn on top of the smoothed line; each week
    also owns an invisible hover band that reveals a guide line, a focus ring
    and a tooltip card only while hovered/focused.
    """
    width, height = 1100, 484
    left, right, top, bottom = 58, 22, 144, 56
    radius = 16
    plot_w = width - left - right
    plot_h = height - top - bottom

    bg = "#0d1117" if dark else "#ffffff"
    text = "#e6edf3" if dark else "#24292f"
    muted = "#8b949e" if dark else "#57606a"
    grid = "#30363d" if dark else "#d8dee4"
    accent = "#3fb950" if dark else "#1f883d"
    tip_bg = "#161b22" if dark else "#ffffff"
    tip_border = "#30363d" if dark else "#d0d7de"
    border = "#30363d" if dark else "#d8dee4"

    n = len(contributions)
    max_value = max(contributions, default=0)
    ticks, y_top = _nice_ticks(max_value, target=4)
    if y_top <= 0:
        y_top = 1

    if week_starts is None or len(week_starts) != n:
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())
        start = monday - timedelta(weeks=max(0, n - 1))
        week_starts = [start + timedelta(weeks=i) for i in range(n)]

    stats = compute_stats(daily or [], contributions, week_starts)
    total = stats["total"]
    avg_week = stats["avg_week"]
    best = stats["best"]
    best_label = stats["best_label"]
    safe_user = xml_escape(str(user))

    def x(i: int) -> float:
        if n <= 1:
            return left + plot_w / 2
        return left + (plot_w * i / (n - 1))

    def y(value: float) -> float:
        return top + plot_h - (plot_h * value / y_top)

    points = [(x(i), y(value)) for i, value in enumerate(contributions)]
    curve = smooth_path(points)

    baseline = top + plot_h
    if points:
        area = f"{curve} L {points[-1][0]:.2f} {baseline:.2f} L {points[0][0]:.2f} {baseline:.2f} Z"
    else:
        area = ""

    if n and week_starts:
        window = f"{week_starts[0].strftime('%b %Y')} - {week_starts[-1].strftime('%b %Y')}"
    else:
        window = f"trailing {WEEKS} weeks"

    if stats["prev4"] > 0 or stats["last4"] > 0:
        pct = stats["trend_pct"]
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
    parts.append(f"<title id=\"title\">{safe_user} GitHub contribution activity</title>")
    parts.append(
        f'<desc id="desc">Smooth weekly GitHub contribution curve over {window}. '
        f"Currently {total} contributions in this window.</desc>"
    )
    parts.append(
        "<defs>"
        f'<linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{accent}" stop-opacity="0.30"/>'
        f'<stop offset="1" stop-color="{accent}" stop-opacity="0.02"/>'
        "</linearGradient>"
        "</defs>"
    )
    parts.append(
        "<style>"
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;}"
        ".mono{font-family:ui-monospace,SFMono-Regular,SF Mono,Menlo,Consolas,monospace;}"
        ".pt .tip,.pt .guide,.pt .focus{opacity:0;transition:opacity .15s ease-in-out;}"
        ".pt:hover .tip,.pt:focus .tip,.pt:hover .guide,.pt:focus .guide,"
        ".pt:hover .focus,.pt:focus .focus{opacity:1;}"
        ".pt .tip{pointer-events:none;}"
        ".pt{outline:none;}"
        "</style>"
    )
    parts.append(f'<rect width="{width}" height="{height}" rx="{radius}" fill="{bg}"/>')
    parts.append(
        f'<rect x="0.75" y="0.75" width="{width - 1.5:.2f}" height="{height - 1.5:.2f}" '
        f'rx="{radius - 1}" fill="none" stroke="{border}" stroke-width="1.5"/>'
    )

    # ---- Header: title + productivity KPIs (the recruiter-facing story) ----
    parts.append(
        f'<text x="{left}" y="32" font-size="15" font-weight="700" fill="{text}" class="mono">'
        "$ commit_stream --trailing 52w</text>"
    )
    parts.append(
        f'<text x="{width - right}" y="32" font-size="12" fill="{muted}" text-anchor="end">'
        f"{xml_escape(window)} · live GitHub data</text>"
    )

    kpi = [
        (f"{total:,}", "TOTAL CONTRIBUTIONS"),
        (f"{avg_week:.1f}/wk", "WEEKLY AVERAGE"),
        (
            f"{stats['active_weeks']}/{n} wks",
            f"ACTIVE WEEKS · {stats['consistency']:.0f}%",
        ),
        (
            f"{stats['current_streak']}d streak",
            f"DAY STREAK · BEST {stats['longest_streak']}d",
        ),
    ]
    for idx, (value, label) in enumerate(kpi):
        kx = left + idx * (plot_w / 4)
        parts.append(
            f'<text x="{kx:.2f}" y="68" font-size="21" font-weight="800" fill="{text}">'
            f"{xml_escape(value)}</text>"
        )
        parts.append(
            f'<text x="{kx:.2f}" y="86" font-size="10.5" letter-spacing="1" fill="{muted}">'
            f"{xml_escape(label)}</text>"
        )

    insight = (
        f"Best week {best:,} ({xml_escape(best_label)}) · " if best_label else ""
    )
    insight += f"Last 4 weeks {stats['last4']:,} ({trend_str}) · {stats['above_avg']} weeks above average"
    parts.append(
        f'<text x="{left}" y="110" font-size="12.5" fill="{muted}">{insight}</text>'
    )
    parts.append(
        f'<line x1="{left}" y1="122" x2="{width - right}" y2="122" '
        f'stroke="{grid}" stroke-width="1" opacity="0.9"/>'
    )

    # ---- Axes (numbered) ----
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
        f'text-anchor="middle" transform="rotate(-90 14 {top + plot_h / 2:.2f})">contributions / week</text>'
    )

    last_labeled = -10
    for i, ws in enumerate(week_starts):
        label = ""
        if i == 0:
            label = ws.strftime("%b %Y")
        elif ws.month != week_starts[i - 1].month and i - last_labeled >= 4:
            label = ws.strftime("%b")
        elif i == n - 1 and i - last_labeled >= 3:
            label = ws.strftime("%b %d")
        if label:
            last_labeled = i
            parts.append(
                f'<text x="{x(i):.2f}" y="{top + plot_h + 24:.2f}" font-size="11.5" '
                f'fill="{muted}" text-anchor="middle">{xml_escape(label)}</text>'
            )

    # ---- Smooth curve + gradient wash ----
    if area:
        parts.append(f'<path d="{area}" fill="url(#areaFill)"/>')
    if curve:
        parts.append(
            f'<path d="{curve}" fill="none" stroke="{accent}" stroke-width="8" '
            f'stroke-linecap="round" stroke-linejoin="round" opacity="0.14"/>'
        )
        parts.append(
            f'<path d="{curve}" fill="none" stroke="{accent}" stroke-width="3" '
            f'stroke-linecap="round" stroke-linejoin="round"/>'
        )

    # ---- Dots on every spike: small crisp hollow markers over the smooth line ----
    for cx, cy in points:
        parts.append(
            f'<circle class="dot" cx="{cx:.2f}" cy="{cy:.2f}" r="3.4" '
            f'fill="{bg}" stroke="{accent}" stroke-width="2.2"/>'
        )

    # One permanent annotation for the peak week (a label, not a dot field).
    if n and best > 0:
        bi = contributions.index(best)
        bx, by = points[bi]
        pill = f"BEST · {best:,}"
        pill_w = 8.2 * len(pill) + 22.0
        pill_cx = min(max(bx, left + pill_w / 2 + 2), width - right - pill_w / 2 - 2)
        above_space = (by - 52.0) >= top
        pill_y = by - 48.0 if above_space else by + 22.0
        stem_y2 = pill_y + 18.0 if above_space else pill_y
        stem_y1 = by - 6.0 if above_space else by + 6.0
        parts.append(
            f'<line x1="{bx:.2f}" y1="{stem_y1:.2f}" x2="{bx:.2f}" y2="{stem_y2:.2f}" '
            f'stroke="{accent}" stroke-width="1.5" stroke-dasharray="3 3" opacity="0.8"/>'
        )
        parts.append(
            f'<rect x="{pill_cx - pill_w / 2:.2f}" y="{pill_y:.2f}" width="{pill_w:.1f}" height="20" rx="10" '
            f'fill="{tip_bg}" stroke="{accent}" stroke-width="1.2"/>'
        )
        parts.append(
            f'<text x="{pill_cx:.2f}" y="{pill_y + 14:.2f}" font-size="11" font-weight="800" '
            f'fill="{accent}" text-anchor="middle">{xml_escape(pill)}</text>'
        )

    # ---- Invisible hover bands: guide + focus ring + tooltip appear on hover only ----
    for i, value in enumerate(contributions):
        cx, cy = points[i]
        ws = week_starts[i]
        we = ws + timedelta(days=6)
        if value == 1:
            count_text = "1 contribution"
        else:
            count_text = f"{value:,} contributions"
        date_text = f"{ws.strftime('%b %d')} - {we.strftime('%b %d, %Y')}"
        full = f"{date_text}: {count_text}"
        if avg_week > 0 and value > 0:
            ratio = value / avg_week
            if abs(ratio - 1.0) < 0.05:
                ctx = "around avg"
            elif ratio >= 1.0:
                ctx = f"{ratio:.1f}x weekly avg"
            else:
                ctx = "below avg"
        elif value == 0:
            ctx = "quiet week"
        else:
            ctx = "active week"
        line2_len = len(f"{value:,}  {ctx}")
        card_w = min(236.0, max(158.0, 7.0 * line2_len + 34.0))
        card_h = 48.0
        tx = min(max(cx, left + card_w / 2 + 2), width - right - card_w / 2 - 2)
        above = (cy - (card_h + 16.0)) >= (top - 4.0)
        rect_y = cy - card_h - 14.0 if above else cy + 16.0
        half_step = (plot_w / max(1, n - 1) / 2) if n > 1 else plot_w / 2
        hit_w = min(26.0, max(12.0, half_step + 2.0))
        parts.append(
            f'<g class="pt" tabindex="0" aria-label="{xml_escape(full)}">'
            f"<title>{xml_escape(full)}</title>"
            f'<rect x="{cx - hit_w:.2f}" y="{top}" width="{hit_w * 2:.2f}" height="{plot_h}" fill="transparent"/>'
            f'<line class="guide" x1="{cx:.2f}" y1="{top}" x2="{cx:.2f}" y2="{baseline:.2f}" '
            f'stroke="{accent}" stroke-width="1" stroke-dasharray="3 4" opacity="0.65"/>'
            f'<circle class="focus" cx="{cx:.2f}" cy="{cy:.2f}" r="5.5" fill="{bg}" stroke="{accent}" stroke-width="3"/>'
            f'<g class="tip" transform="translate({tx:.2f},{0:.2f})">'
            f'<rect x="{-card_w / 2:.1f}" y="{rect_y:.2f}" width="{card_w:.1f}" height="{card_h}" rx="8" '
            f'fill="{tip_bg}" stroke="{tip_border}" stroke-width="1"/>'
            f'<text x="0" y="{rect_y + 18:.2f}" font-size="11" fill="{muted}" text-anchor="middle">'
            f"{xml_escape(date_text)}</text>"
            f'<text x="0" y="{rect_y + 35:.2f}" font-size="13" font-weight="800" fill="{text}" text-anchor="middle">'
            f"{xml_escape(f'{value:,}')} "
            f'<tspan font-size="11" font-weight="400" fill="{muted}">· {xml_escape(ctx)}</tspan>'
            "</text>"
            "</g>"
            "</g>"
        )

    parts.append(
        f'<text x="{width - right}" y="{height - 12}" font-size="11" fill="{muted}" text-anchor="end">'
        "hover any week for exact counts · refreshes every 12 hours from live GitHub data</text>"
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
