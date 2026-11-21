#!/usr/bin/env python3
"""Profile stats cards — public AND private activity, rendered as themed SVGs.

Runs in GitHub Actions with a personal access token that belongs to the profile owner
(classic PAT with `repo` + `read:user`). Because the token is the owner's, GitHub returns
private commits and private repositories too, so every number here is public + private.

    GH_TOKEN=... python .github/scripts/profile_stats.py --out dist
    python .github/scripts/profile_stats.py --sample .github/scripts/sample_stats.json --out /tmp/x

Writes, for each theme (dark, light):
    stats-overview-<theme>.svg   headline numbers + streaks
    stats-years-<theme>.svg      commits per year, public vs private
    stats-languages-<theme>.svg  top languages across owned repos
and stats.json with the raw numbers.

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import urllib.request
from html import escape
from zoneinfo import ZoneInfo

API = "https://api.github.com/graphql"
TZ = ZoneInfo(os.environ.get("STATS_TZ", "Asia/Kolkata"))
EXCLUDED_LANGUAGES = {s.strip().lower() for s in os.environ.get("STATS_EXCLUDE_LANGS", "Jupyter Notebook").split(",") if s.strip()}

# --------------------------------------------------------------------------- data

PROFILE_QUERY = """
query($cursor: String) {
  viewer {
    login name createdAt
    pullRequests { totalCount }
    issues { totalCount }
    repositoriesContributedTo(includeUserRepositories: true,
                              contributionTypes: [COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]) { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER, isFork: false) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        isPrivate
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name color } } }
      }
    }
  }
}"""

YEAR_QUERY = """
query($from: DateTime!, $to: DateTime!) {
  viewer {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      commitContributionsByRepository(maxRepositories: 100) {
        repository { isPrivate }
        contributions { totalCount }
      }
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
  }
}"""


def gql(token: str, query: str, variables: dict) -> dict:
    req = urllib.request.Request(
        API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-stats"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GitHub GraphQL error: {body['errors']}")
    return body["data"]["viewer"]


def fetch(token: str) -> dict:
    """Return the raw API responses: profile (all repo pages merged) and one collection per year."""
    profile, cursor, repos = None, None, []
    while True:
        page = gql(token, PROFILE_QUERY, {"cursor": cursor})
        profile = profile or page
        repos += page["repositories"]["nodes"]
        info = page["repositories"]["pageInfo"]
        if not info["hasNextPage"]:
            break
        cursor = info["endCursor"]
    profile["repositories"]["nodes"] = repos

    now = dt.datetime.now(dt.timezone.utc)
    first_year = int(profile["createdAt"][:4])
    years = {}
    for year in range(first_year, now.year + 1):
        start = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc)
        end = min(dt.datetime(year, 12, 31, 23, 59, 59, tzinfo=dt.timezone.utc), now)
        years[str(year)] = gql(token, YEAR_QUERY, {"from": start.isoformat(), "to": end.isoformat()})["contributionsCollection"]
    return {"profile": profile, "years": years}


def compute(raw: dict, today: dt.date) -> dict:
    """Turn raw API responses into the numbers shown on the cards."""
    profile, years = raw["profile"], raw["years"]

    per_year, days = [], {}
    total_contrib = 0
    for year, c in sorted(years.items()):
        public = sum(r["contributions"]["totalCount"] for r in c["commitContributionsByRepository"]
                     if not r["repository"]["isPrivate"])
        # Everything that is not a public repo commit is private: commits in private repos the
        # token can see, plus restricted (private) contributions GitHub only reports as a count.
        private = max(c["totalCommitContributions"] - public, 0) + c["restrictedContributionsCount"]
        per_year.append({"year": int(year), "public": public, "private": private})
        total_contrib += c["contributionCalendar"]["totalContributions"]
        for week in c["contributionCalendar"]["weeks"]:
            for d in week["contributionDays"]:
                days[d["date"]] = days.get(d["date"], 0) + d["contributionCount"]

    # streaks
    active = {dt.date.fromisoformat(k) for k, v in days.items() if v > 0}
    longest = run = 0
    prev = None
    for d in sorted(active):
        run = run + 1 if prev and (d - prev).days == 1 else 1
        longest = max(longest, run)
        prev = d
    cursor = today if today in active else today - dt.timedelta(days=1)
    current = 0
    while cursor in active:
        current += 1
        cursor -= dt.timedelta(days=1)

    # languages across owned, non-fork repos (public + private)
    sizes, colors = {}, {}
    for repo in profile["repositories"]["nodes"]:
        for e in repo["languages"]["edges"]:
            name = e["node"]["name"]
            if name.lower() in EXCLUDED_LANGUAGES:
                continue
            sizes[name] = sizes.get(name, 0) + e["size"]
            colors[name] = e["node"]["color"] or "#8b949e"
    total_size = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    langs = [{"name": n, "pct": s / total_size * 100, "color": colors[n]} for n, s in ranked[:6]]
    other = sum(s for _, s in ranked[6:])
    if other:
        langs.append({"name": "Other", "pct": other / total_size * 100, "color": "#6e7681"})

    this_year = next((y for y in per_year if y["year"] == today.year), {"public": 0, "private": 0})
    repos = profile["repositories"]["nodes"]
    return {
        "login": profile["login"],
        "updated": today.isoformat(),
        "commits_public": sum(y["public"] for y in per_year),
        "commits_private": sum(y["private"] for y in per_year),
        "commits_this_year": this_year["public"] + this_year["private"],
        "contributions_total": total_contrib,
        "streak_current": current,
        "streak_longest": longest,
        "pull_requests": profile["pullRequests"]["totalCount"],
        "repos_contributed": profile["repositoriesContributedTo"]["totalCount"],
        "repos_owned": len(repos),
        "repos_private": sum(1 for r in repos if r["isPrivate"]),
        "per_year": per_year,
        "languages": langs,
    }

# --------------------------------------------------------------------------- rendering

THEMES = {
    "dark": dict(card="#161b22", card2="#1c222b", border="#30363d", text="#e6edf3", muted="#8b949e",
                 accent="#e0a458", public="#7fb5e6", private="#a371f7", track="#21262d", grid="#262c36"),
    "light": dict(card="#ffffff", card2="#f6f8fa", border="#d0d7de", text="#1f2328", muted="#59636e",
                  accent="#b26a00", public="#2f6fab", private="#6e40c9", track="#eaeef2", grid="#eaeef2"),
}
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
W = 860


def fmt(n: int) -> str:
    return f"{n:,}"


def nice_step(raw: float) -> int:
    """Round a gridline step up to 1, 2, 2.5 or 5 x 10^n so the axis shows round numbers."""
    if raw <= 1:
        return 1
    mag = 10 ** (len(str(int(raw))) - 1)
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return int(m * mag)
    return int(10 * mag)


def shell(h: int, t: dict, title: str, subtitle: str, body: str, label: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {h}" width="{W}" height="{h}" role="img" aria-label="{escape(label)}">
  <rect x="0.5" y="0.5" width="{W-1}" height="{h-1}" rx="12" fill="{t['card']}" stroke="{t['border']}"/>
  <text x="24" y="34" font-family="{SANS}" font-size="15" font-weight="600" fill="{t['text']}">{escape(title)}</text>
  <text x="{W-24}" y="34" text-anchor="end" font-family="{MONO}" font-size="11" fill="{t['muted']}">{escape(subtitle)}</text>
{body}
</svg>
"""


def overview(s: dict, t: dict) -> str:
    total = s["commits_public"] + s["commits_private"]
    pub_w = 248 * (s["commits_public"] / total) if total else 0
    year = s["updated"][:4]
    tiles = [
        (fmt(s["commits_this_year"]), f"commits in {year}"),
        (fmt(s["contributions_total"]), "total contributions"),
        (f"{fmt(s['streak_current'])} days", "current streak"),
        (f"{fmt(s['streak_longest'])} days", "longest streak"),
        (fmt(s["pull_requests"]), "pull requests"),
        (fmt(s["repos_contributed"]), "repos contributed to"),
    ]
    tile_svg = []
    for i, (value, label) in enumerate(tiles):
        col, row = i % 3, i // 3
        x, y = 320 + col * 176, 56 + row * 72
        tile_svg.append(
            f'  <rect x="{x}" y="{y}" width="164" height="60" rx="8" fill="{t["card2"]}" stroke="{t["border"]}"/>\n'
            f'  <text x="{x+14}" y="{y+28}" font-family="{SANS}" font-size="20" font-weight="700" fill="{t["text"]}">{escape(value)}</text>\n'
            f'  <text x="{x+14}" y="{y+47}" font-family="{SANS}" font-size="11" fill="{t["muted"]}">{escape(label)}</text>'
        )
    body = f"""  <text x="24" y="104" font-family="{SANS}" font-size="46" font-weight="700" fill="{t['accent']}">{fmt(total)}</text>
  <text x="26" y="128" font-family="{SANS}" font-size="12.5" fill="{t['muted']}">total commits, all repositories</text>
  <rect x="24" y="146" width="248" height="8" rx="4" fill="{t['private']}"/>
  <rect x="24" y="146" width="{pub_w:.1f}" height="8" rx="4" fill="{t['public']}"/>
  <circle cx="29" cy="174" r="4" fill="{t['public']}"/>
  <text x="38" y="178" font-family="{SANS}" font-size="11.5" fill="{t['text']}">{fmt(s['commits_public'])} <tspan fill="{t['muted']}">public</tspan></text>
  <circle cx="149" cy="174" r="4" fill="{t['private']}"/>
  <text x="158" y="178" font-family="{SANS}" font-size="11.5" fill="{t['text']}">{fmt(s['commits_private'])} <tspan fill="{t['muted']}">private</tspan></text>
""" + "\n".join(tile_svg)
    label = (f"{fmt(total)} total commits ({fmt(s['commits_public'])} public, {fmt(s['commits_private'])} private); "
             f"{fmt(s['commits_this_year'])} in {year}; current streak {s['streak_current']} days; longest {s['streak_longest']} days.")
    return shell(206, t, "GitHub activity", f"public + private · updated {s['updated']}", body, label)


def years(s: dict, t: dict) -> str:
    data = s["per_year"][-8:]
    peak = max((y["public"] + y["private"] for y in data), default=1) or 1
    step = nice_step(peak / 3)
    top = step * 3 if step * 3 >= peak else step * 4
    ticks = int(round(top / step))
    chart_x, chart_w, base, chart_h = 56, W - 56 - 32, 196, 128
    slot = chart_w / max(len(data), 1)
    bar_w = min(54, slot * 0.56)
    parts = []
    for i in range(1, ticks + 1):  # gridlines on round numbers
        gy = base - chart_h * i / ticks
        parts.append(f'  <line x1="{chart_x}" x2="{W-32}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{t["grid"]}"/>')
        parts.append(f'  <text x="{chart_x-10}" y="{gy+4:.1f}" text-anchor="end" font-family="{MONO}" font-size="10" fill="{t["muted"]}">{fmt(int(step*i))}</text>')
    parts.append(f'  <line x1="{chart_x}" x2="{W-32}" y1="{base}" y2="{base}" stroke="{t["border"]}"/>')
    for i, y in enumerate(data):
        cx = chart_x + slot * i + slot / 2
        x = cx - bar_w / 2
        ph = chart_h * y["public"] / top
        vh = chart_h * y["private"] / top
        if ph + vh:  # one rounded bar, split into public (bottom) and private (top) without a seam
            parts.append(f'  <clipPath id="b{i}"><rect x="{x:.1f}" y="{base-ph-vh:.1f}" width="{bar_w:.1f}" height="{ph+vh:.1f}" rx="4"/></clipPath>')
            parts.append(f'  <g clip-path="url(#b{i})">'
                         f'<rect x="{x:.1f}" y="{base-ph-vh:.1f}" width="{bar_w:.1f}" height="{vh+0.5:.1f}" fill="{t["private"]}"/>'
                         f'<rect x="{x:.1f}" y="{base-ph:.1f}" width="{bar_w:.1f}" height="{ph:.1f}" fill="{t["public"]}"/></g>')
        total = y["public"] + y["private"]
        parts.append(f'  <text x="{cx:.1f}" y="{base-ph-vh-7:.1f}" text-anchor="middle" font-family="{SANS}" font-size="11" font-weight="600" fill="{t["text"]}">{fmt(total)}</text>')
        parts.append(f'  <text x="{cx:.1f}" y="{base+17}" text-anchor="middle" font-family="{MONO}" font-size="11" fill="{t["muted"]}">{y["year"]}</text>')
    legend = (f'  <circle cx="{W-196}" cy="30" r="4" fill="{t["public"]}"/><text x="{W-187}" y="34" font-family="{SANS}" font-size="11" fill="{t["muted"]}">public</text>\n'
              f'  <circle cx="{W-136}" cy="30" r="4" fill="{t["private"]}"/><text x="{W-127}" y="34" font-family="{SANS}" font-size="11" fill="{t["muted"]}">private</text>')
    svg = shell(230, t, "Commits per year", "", "\n".join(parts), "Commits per year, public and private: " +
                ", ".join(f"{y['year']}: {y['public']+y['private']}" for y in data))
    return svg.replace("</svg>", legend + "\n</svg>")


def languages(s: dict, t: dict) -> str:
    langs = s["languages"]
    x, parts = 24.0, []
    bar_w = W - 48
    parts.append(f'  <clipPath id="bar"><rect x="24" y="54" width="{bar_w}" height="12" rx="6"/></clipPath>')
    parts.append('  <g clip-path="url(#bar)">')
    for l in langs:
        w = bar_w * l["pct"] / 100
        parts.append(f'    <rect x="{x:.1f}" y="54" width="{w+0.6:.1f}" height="12" fill="{l["color"]}"/>')
        x += w
    parts.append("  </g>")
    for i, l in enumerate(langs):
        col, row = i % 4, i // 4
        lx, ly = 30 + col * 205, 96 + row * 26
        parts.append(f'  <circle cx="{lx}" cy="{ly-4}" r="5" fill="{l["color"]}"/>')
        parts.append(f'  <text x="{lx+12}" y="{ly}" font-family="{SANS}" font-size="12" fill="{t["text"]}">{escape(l["name"])} '
                     f'<tspan fill="{t["muted"]}">{l["pct"]:.1f}%</tspan></text>')
    rows = (len(langs) + 3) // 4
    h = 96 + rows * 26
    sub = f"{s['repos_owned']} repos · {s['repos_private']} private"
    return shell(h, t, "Top languages", sub, "\n".join(parts),
                 "Top languages: " + ", ".join(f"{l['name']} {l['pct']:.0f}%" for l in langs))


def render(stats: dict, out: str) -> None:
    os.makedirs(out, exist_ok=True)
    for name, t in THEMES.items():
        for kind, fn in (("overview", overview), ("years", years), ("languages", languages)):
            with open(os.path.join(out, f"stats-{kind}-{name}.svg"), "w", encoding="utf-8") as fh:
                fh.write(fn(stats, t))
    with open(os.path.join(out, "stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="dist")
    ap.add_argument("--sample", help="render from a stats JSON instead of calling the API")
    args = ap.parse_args()
    if args.sample:
        with open(args.sample, encoding="utf-8") as fh:
            stats = json.load(fh)
    else:
        token = os.environ.get("GH_TOKEN")
        if not token:
            raise SystemExit("GH_TOKEN is not set (needs a PAT with repo + read:user)")
        stats = compute(fetch(token), dt.datetime.now(TZ).date())
    render(stats, args.out)
    print(json.dumps({k: v for k, v in stats.items() if k not in ("per_year", "languages")}, indent=2))


if __name__ == "__main__":
    main()
