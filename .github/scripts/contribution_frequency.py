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
    dark: bool = False,
) -> str:
    width, height = 1100, 384
    left, right, top, bottom = 56, 18, 58, 36
    plot_w = width - left - right
    plot_h = height - top - bottom

    bg = "#0d1117" if dark else "#ffffff"
    text = "#e6edf3" if dark else "#24292f"
    muted = "#8b949e" if dark else "#57606a"
    line = "#f0f0f0" if dark else "#24292f"
    grid = "#30363d" if dark else "#d8dee4"
    fill = "#8b949e" if dark else "#57606a"
    dot_stroke = "#0d1117" if dark else "#ffffff"
    tip_bg = "#161b22" if dark else "#ffffff"
    tip_border = "#30363d" if dark else "#d0d7de"

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

    total = sum(contributions)
    avg = (total / n) if n else 0.0
    best = max_value
    best_label = ""
    if n and best > 0:
        best_i = contributions.index(best)
        best_label = week_starts[best_i].strftime("%b %d, %Y")
    safe_user = xml_escape(str(user))

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">'
    )
    parts.append(f"<title id=\"title\">{safe_user} GitHub contribution frequency</title>")
    parts.append(
        f'<desc id="desc">Weekly GitHub contributions over the trailing {WEEKS} weeks. '
        f"Currently {total} contributions in this window.</desc>"
    )
    parts.append(
        "<style>"
        "text{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;}"
        ".pt .tip{opacity:0;transition:opacity .15s ease-in-out;pointer-events:none;}"
        ".pt:hover .tip,.pt:focus .tip{opacity:1;}"
        ".pt .halo{opacity:0;transition:opacity .15s ease-in-out;}"
        ".pt:hover .halo,.pt:focus .halo{opacity:0.28;}"
        ".pt{outline:none;}"
        "</style>"
    )
    parts.append(f'<rect width="{width}" height="{height}" fill="{bg}"/>')

    # Header: visible title + numbered summary so the chart is readable without hover.
    parts.append(
        f'<text x="{left}" y="26" font-size="16" font-weight="600" fill="{text}">'
        f"Contribution frequency · trailing {n} weeks</text>"
    )
    subtitle = f"{total:,} contributions · avg {avg:.1f}/week"
    if best_label:
        subtitle += f" · best {best:,} ({xml_escape(best_label)})"
    parts.append(
        f'<text x="{left}" y="44" font-size="12.5" fill="{muted}">{xml_escape(subtitle)}</text>'
    )

    # Y gridlines + numbered tick labels.
    for tick in ticks:
        yy = y(tick)
        strong = tick == 0
        parts.append(
            f'<line x1="{left}" y1="{yy:.2f}" x2="{width - right}" y2="{yy:.2f}" '
            f'stroke="{grid}" stroke-width="1" opacity="{1.0 if strong else 0.9}"/>'
        )
        parts.append(
            f'<text x="{left - 10}" y="{yy + 4:.2f}" font-size="11.5" fill="{muted}" '
            f'text-anchor="end">{_fmt(tick)}</text>'
        )
    parts.append(
        f'<text x="14" y="{top + plot_h / 2:.2f}" font-size="11" fill="{muted}" '
        f'text-anchor="middle" transform="rotate(-90 14 {top + plot_h / 2:.2f})">contributions / week</text>'
    )

    # X month labels (numbered by calendar date, thinned to avoid collisions).
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
                f'<text x="{x(i):.2f}" y="{top + plot_h + 21:.2f}" font-size="11.5" '
                f'fill="{muted}" text-anchor="middle">{xml_escape(label)}</text>'
            )

    parts.append(
        f'<path d="{area}" fill="{fill}" opacity="0.17"/>' if area else ""
    )
    parts.append(
        f'<path d="{curve}" fill="none" stroke="{line}" stroke-width="3.5" '
        f'stroke-linecap="round" stroke-linejoin="round"/>' if curve else ""
    )

    # Hoverable points: native <title> tooltip + visible number badge on hover.
    for i, value in enumerate(contributions):
        cx, cy = points[i]
        ws = week_starts[i]
        we = ws + timedelta(days=6)
        if value == 1:
            count_text = "1 contribution"
        else:
            count_text = f"{value:,} contributions"
        date_text = f"{ws.strftime('%b %d')} – {we.strftime('%b %d, %Y')}"
        full = f"{date_text}: {count_text}"
        num = f"{value:,}"
        half = max(21.0, 7.5 * len(num) + 13.0)
        # Keep the hover badge inside the plot; show it below unusually high points.
        above = (cy - 44.0) >= (top - 6.0)
        if above:
            rect_y = cy - 38.0
            text_y = cy - 23.0
        else:
            rect_y = cy + 16.0
            text_y = cy + 31.0
        half_step = (plot_w / max(1, n - 1) / 2) if n > 1 else plot_w / 2
        hit_w = min(26.0, max(12.0, half_step + 2.0))
        parts.append(
            f'<g class="pt" tabindex="0" aria-label="{xml_escape(full)}">'
            f"<title>{xml_escape(full)}</title>"
            f'<rect x="{cx - hit_w:.2f}" y="{top}" width="{hit_w * 2:.2f}" height="{plot_h}" fill="transparent"/>'
            f'<circle class="halo" cx="{cx:.2f}" cy="{cy:.2f}" r="9" fill="{line}"/>'
            f'<circle class="dot" cx="{cx:.2f}" cy="{cy:.2f}" r="3.8" fill="{line}" stroke="{dot_stroke}" stroke-width="2"/>'
            f'<g class="tip" transform="translate({cx:.2f},{0:.2f})">'
            f'<rect x="{-half:.1f}" y="{rect_y:.2f}" width="{half * 2:.1f}" height="22" rx="6" '
            f'fill="{tip_bg}" stroke="{tip_border}" stroke-width="1"/>'
            f'<text x="0" y="{text_y:.2f}" font-size="12" font-weight="700" fill="{text}" text-anchor="middle">{num}</text>'
            "</g>"
            "</g>"
        )

    # Small footer so readers know the numbers refresh from live data.
    parts.append(
        f'<text x="{width - right}" y="{height - 8}" font-size="11" fill="{muted}" text-anchor="end">'
        "hover any point for its weekly count · refreshed from live GitHub data</text>"
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
        render_svg(user, contributions, week_starts, dark=False), encoding="utf-8"
    )
    (out / "contribution-frequency-dark.svg").write_text(
        render_svg(user, contributions, week_starts, dark=True), encoding="utf-8"
    )

    print(f"Generated {len(contributions)} weekly points from live GitHub data")
    print(f"Total contributions in displayed window: {sum(contributions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
