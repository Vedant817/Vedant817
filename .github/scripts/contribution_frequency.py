#!/usr/bin/env python3
import json
import math
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
    return datetime.combine(d, time.max, tzinfo=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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

    days = []
    calendar = user_data["contributionsCollection"]["contributionCalendar"]
    for week in calendar["weeks"]:
        for item in week["contributionDays"]:
            d = date.fromisoformat(item["date"])
            if start <= d <= end:
                days.append((d, int(item["contributionCount"])))
    return days


def fetch_weekly_commits(token: str, user: str, starts: list[date], end: date) -> list[int]:
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          totalCommitContributions
        }
      }
    }
    """
    result = []
    for week_start in starts:
        week_end = min(end, week_start + timedelta(days=6))
        data = graphql(
            token,
            query,
            {"login": user, "from": iso_start(week_start), "to": iso_end(week_end)},
        )
        user_data = data.get("user")
        if not user_data:
            raise RuntimeError(f"GitHub user {user!r} was not found.")
        result.append(int(user_data["contributionsCollection"]["totalCommitContributions"]))
    return result


def aggregate_weekly(days: list[tuple[date, int]], start: date, weeks: int) -> list[int]:
    totals = [0] * weeks
    for d, count in days:
        index = (d - start).days // 7
        if 0 <= index < weeks:
            totals[index] += count
    return totals


def nice_ceiling(value: int) -> int:
    if value <= 5:
        return 5
    magnitude = 10 ** int(math.floor(math.log10(value)))
    normalized = value / magnitude
    if normalized <= 1:
        step = 1
    elif normalized <= 2:
        step = 2
    elif normalized <= 5:
        step = 5
    else:
        step = 10
    return int(step * magnitude)


def esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_svg(user: str, starts: list[date], commits: list[int], contributions: list[int], dark: bool) -> str:
    width, height = 900, 340
    left, right, top, bottom = 62, 28, 76, 56
    plot_w = width - left - right
    plot_h = height - top - bottom

    bg = "#0d1117" if dark else "#ffffff"
    text = "#e6edf3" if dark else "#24292f"
    muted = "#8b949e" if dark else "#57606a"
    grid = "#21262d" if dark else "#d8dee4"
    commit_color = "#a371f7" if dark else "#8250df"
    contrib_color = "#3fb950" if dark else "#2da44e"
    area_color = "#238636" if dark else "#2da44e"

    ymax = nice_ceiling(max(max(commits, default=0), max(contributions, default=0), 1))

    def x(i: int) -> float:
        return left + (plot_w * i / max(1, len(starts) - 1))

    def y(v: int) -> float:
        return top + plot_h - (plot_h * v / ymax)

    def points(values: list[int]) -> str:
        return " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))

    contrib_points = points(contributions)
    commit_points = points(commits)
    area_points = f"{left:.1f},{top + plot_h:.1f} {contrib_points} {x(len(starts)-1):.1f},{top + plot_h:.1f}"

    month_labels = []
    last_month = None
    for i, d in enumerate(starts):
        if d.month != last_month:
            month_labels.append((i, d.strftime("%b")))
            last_month = d.month
    if len(month_labels) > 12:
        month_labels = month_labels[-12:]

    total_commits = sum(commits)
    total_contributions = sum(contributions)
    last_commits = commits[-1] if commits else 0
    last_contributions = contributions[-1] if contributions else 0

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{esc(user)} contribution frequency</title>',
        '<desc id="desc">Weekly commits and all GitHub contributions over the trailing 52 weeks, generated from GitHub GraphQL.</desc>',
        '<defs>',
        f'<linearGradient id="area" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="{area_color}" stop-opacity="0.24"/><stop offset="100%" stop-color="{area_color}" stop-opacity="0.02"/></linearGradient>',
        '</defs>',
        f'<rect width="{width}" height="{height}" rx="12" fill="{bg}"/>',
        f'<text x="{left}" y="30" fill="{text}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="17" font-weight="600">Contribution frequency</text>',
        f'<text x="{left}" y="51" fill="{muted}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="12">trailing 52 weeks · weekly totals · UTC</text>',
        f'<circle cx="{width-280}" cy="29" r="4" fill="{commit_color}"/><text x="{width-268}" y="33" fill="{muted}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="12">commits {total_commits}</text>',
        f'<circle cx="{width-160}" cy="29" r="4" fill="{contrib_color}"/><text x="{width-148}" y="33" fill="{muted}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="12">all {total_contributions}</text>',
    ]

    for tick in range(5):
        value = int(round(ymax * tick / 4))
        yy = y(value)
        parts.append(f'<line x1="{left}" y1="{yy:.1f}" x2="{width-right}" y2="{yy:.1f}" stroke="{grid}" stroke-width="1"/>')
        parts.append(f'<text x="{left-12}" y="{yy+4:.1f}" text-anchor="end" fill="{muted}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="11">{value}</text>')

    parts.append(f'<polygon points="{area_points}" fill="url(#area)"/>')
    parts.append(f'<polyline points="{contrib_points}" fill="none" stroke="{contrib_color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
    parts.append(f'<polyline points="{commit_points}" fill="none" stroke="{commit_color}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>')

    if starts:
        parts.append(f'<circle cx="{x(len(starts)-1):.1f}" cy="{y(last_contributions):.1f}" r="4" fill="{bg}" stroke="{contrib_color}" stroke-width="2"/>')
        parts.append(f'<circle cx="{x(len(starts)-1):.1f}" cy="{y(last_commits):.1f}" r="4" fill="{bg}" stroke="{commit_color}" stroke-width="2"/>')

    for i, label in month_labels:
        parts.append(f'<text x="{x(i):.1f}" y="{height-25}" text-anchor="middle" fill="{muted}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="11">{label}</text>')

    parts.append(
        f'<text x="{width-right}" y="{height-8}" text-anchor="end" fill="{muted}" '
        'font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif" font-size="10">'
        'source: GitHub contribution data</text>'
    )
    parts.append('</svg>')
    return ''.join(parts)


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
    starts = [start + timedelta(weeks=i) for i in range(WEEKS)]

    print(f"Fetching contribution data for {user}: {start} through {today}")
    daily = fetch_calendar(token, user, start, today)
    contributions = aggregate_weekly(daily, start, WEEKS)
    commits = fetch_weekly_commits(token, user, starts, today)

    out = Path("dist")
    out.mkdir(parents=True, exist_ok=True)
    (out / "contribution-frequency.svg").write_text(
        render_svg(user, starts, commits, contributions, dark=False), encoding="utf-8"
    )
    (out / "contribution-frequency-dark.svg").write_text(
        render_svg(user, starts, commits, contributions, dark=True), encoding="utf-8"
    )

    print(f"Generated {len(starts)} weekly points")
    print(f"Commits: {sum(commits)} | All contributions: {sum(contributions)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
