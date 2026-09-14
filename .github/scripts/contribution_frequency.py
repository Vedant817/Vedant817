#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

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


def render_svg(user: str, contributions: list[int], dark: bool) -> str:
    width, height = 1100, 320
    left, right, top, bottom = 0, 0, 12, 6
    plot_w = width - left - right
    plot_h = height - top - bottom

    bg = "#0d1117" if dark else "#ffffff"
    line = "#f0f0f0" if dark else "#24292f"
    grid = "#30363d" if dark else "#d8dee4"
    fill = "#8b949e" if dark else "#57606a"

    max_value = max(contributions, default=1)
    if max_value <= 0:
        max_value = 1

    # Keep a little vertical breathing room so the highest point never clips.
    y_max = max_value * 1.12

    def x(i: int) -> float:
        return left + (plot_w * i / max(1, len(contributions) - 1))

    def y(value: int) -> float:
        return top + plot_h - (plot_h * value / y_max)

    points = [(x(i), y(value)) for i, value in enumerate(contributions)]
    curve = smooth_path(points)

    baseline = top + plot_h
    if points:
        area = f"{curve} L {points[-1][0]:.2f} {baseline:.2f} L {points[0][0]:.2f} {baseline:.2f} Z"
    else:
        area = ""

    # Three quiet horizontal guides, matching the reference aesthetic.
    guides = []
    for fraction in (0.0, 1 / 3, 2 / 3, 1.0):
        yy = top + plot_h * (1 - fraction)
        guides.append(
            f'<line x1="0" y1="{yy:.2f}" x2="{width}" y2="{yy:.2f}" '
            f'stroke="{grid}" stroke-width="1" opacity="0.9"/>'
        )

    total = sum(contributions)

    return "".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
            f'<title id="title">{user} GitHub contribution frequency</title>',
            f'<desc id="desc">Smooth weekly GitHub contribution frequency over the trailing {WEEKS} weeks. '
            f'The graph is generated from live GitHub contribution data and currently represents {total} contributions.</desc>',
            f'<rect width="{width}" height="{height}" fill="{bg}"/>',
            *guides,
            (
                f'<path d="{area}" fill="{fill}" opacity="0.17"/>'
                if area
                else ""
            ),
            (
                f'<path d="{curve}" fill="none" stroke="{line}" stroke-width="4" '
                f'stroke-linecap="round" stroke-linejoin="round"/>'
                if curve
                else ""
            ),
            "</svg>",
        ]
    )


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

    out = Path("dist")
    out.mkdir(parents=True, exist_ok=True)

    (out / "contribution-frequency.svg").write_text(
        render_svg(user, contributions, dark=False), encoding="utf-8"
    )
    (out / "contribution-frequency-dark.svg").write_text(
        render_svg(user, contributions, dark=True), encoding="utf-8"
    )

    print(f"Generated {len(contributions)} weekly points from live GitHub data")
    print(f"Total contributions in displayed window: {sum(contributions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
