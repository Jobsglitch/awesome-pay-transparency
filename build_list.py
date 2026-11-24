#!/usr/bin/env python3
"""Rebuild README.md and jobs.json for one job list from the JobsGlitch public API.

Run inside a list's folder (it reads ./list.json):

    JOBSGLITCH_API_KEY=... python build_list.py

Standard library only, so the GitHub Action needs no install step. If the API fails, or fewer
than `min_rows` jobs pass the rules, it exits non-zero and leaves the existing files alone:
a bad day must never wipe the list.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_BASE = os.environ.get("JOBSGLITCH_API_BASE", "https://jobsglitch.com/api/public/v1").rstrip("/")
PAGE_SIZE = 100          # the API's maximum
PAUSE_SECONDS = 0.7      # stays under the API's 100 requests/minute
SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹", "CAD": "CA$", "AUD": "A$"}
PERIOD_SUFFIX = {"yearly": "", "monthly": "/mo", "weekly": "/wk", "daily": "/day", "hourly": "/hr"}


class ListError(RuntimeError):
    """The list could not be rebuilt; the existing files are left untouched."""


# ── Fetch ────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict, key: str, tries: int = 3) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "jobsglitch-list-builder"})
    for attempt in range(1, tries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # 4xx other than 429 will not fix itself: a bad key or a bad parameter.
            if exc.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise ListError(f"API answered {exc.code} for {path} {params}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == tries:
                raise ListError(f"API unreachable for {path}: {exc}") from exc
        time.sleep(5 * attempt)
    raise ListError(f"API gave no answer for {path}")


def fetch_jobs(cfg: dict, key: str, get=api_get, pause: float = PAUSE_SECONDS) -> list[dict]:
    """Every job the list's searches return, newest first within each search, one copy per id."""
    found: dict[str, dict] = {}
    for query in cfg["queries"]:
        params = {**cfg.get("params", {}), **query,
                  "days": cfg.get("days", 14), "sort_by": "date", "limit": PAGE_SIZE}
        for page in range(cfg.get("pages_per_query", 3)):
            data = get("/jobs/search", {**params, "page": page}, key)
            for job in data.get("jobs", []):
                found.setdefault(job["id"], job)
            if not data.get("pagination", {}).get("has_next"):
                break
            time.sleep(pause)
        time.sleep(pause)
    return list(found.values())


# ── Rules ────────────────────────────────────────────────────────────────────

def _matches_title(title: str, cfg: dict) -> bool:
    """`title_match` is a list of alternatives; within one alternative every pattern must match."""
    if any(re.search(p, title, re.I) for p in cfg.get("title_exclude", [])):
        return False
    alternatives = cfg.get("title_match")
    if not alternatives:
        return True
    return any(all(re.search(p, title, re.I) for p in patterns) for patterns in alternatives)


def reject_reason(job: dict, cfg: dict) -> str | None:
    """Why a job is left out, or None when it belongs on the list."""
    if job.get("is_open") is not True:
        return "not checked open"
    if not str(job.get("apply_url") or "").startswith(("http://", "https://")):
        return "no employer link"
    if not job.get("posted_at") or not job.get("company") or not job.get("title"):
        return "missing basics"
    pay = job.get("salary") or {}
    if pay.get("is_estimated") is not False:
        return "pay not posted by the employer"
    if not (pay.get("min") or pay.get("max")) or not pay.get("currency"):
        return "no pay figure"
    if pay.get("period") not in PERIOD_SUFFIX:
        return "unknown pay period"
    bounds = cfg.get("yearly_pay_bounds", {}).get(pay["currency"])
    if bounds and pay["period"] == "yearly" and not bounds[0] <= (pay.get("max") or pay.get("min")) <= bounds[1]:
        # Upstream parsing slips exist (a monthly figure stored as yearly, thousands read as millions).
        return "pay figure outside a believable yearly range"
    if cfg.get("currencies") and pay["currency"] not in cfg["currencies"]:
        return "currency outside the list"
    if cfg.get("countries") and not set(job.get("countries") or []) & set(cfg["countries"]):
        return "country outside the list"
    if any(re.search(p, job["company"], re.I) for p in cfg.get("company_exclude", [])):
        return "not a direct employer"
    if not _matches_title(job["title"], cfg):
        return "title outside the list"
    return None


def _pay_key(job: dict) -> tuple:
    pay = job["salary"]
    return (pay.get("min"), pay.get("max"), pay.get("currency"), pay.get("period"))


def select(jobs: list[dict], cfg: dict) -> tuple[list[dict], dict[str, int]]:
    """Apply the rules, fold the same role posted once per office into one row, cap each company,
    and return the rows newest first with a tally of why the rest were left out."""
    dropped: dict[str, int] = {}
    kept = []
    for job in jobs:
        reason = reject_reason(job, cfg)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(job)
    kept.sort(key=lambda j: j["posted_at"], reverse=True)

    rows: dict[tuple, dict] = {}
    for job in kept:
        key = (job["company"].lower(), re.sub(r"\s+", " ", job["title"]).lower(), _pay_key(job))
        if key in rows:
            first = rows[key]
            first["locations"] = _merge(first.get("locations"), job.get("locations") or [job.get("location")])
            first["countries"] = _merge(first.get("countries"), job.get("countries"))
            dropped["same role, another office"] = dropped.get("same role, another office", 0) + 1
        else:
            rows[key] = {**job, "locations": _merge(job.get("locations") or [job.get("location")], [])}

    per_company: dict[str, int] = {}
    result = []
    for job in rows.values():
        company = job["company"].lower()
        per_company[company] = per_company.get(company, 0) + 1
        if per_company[company] > cfg.get("max_per_company", 5):
            dropped["company already has its share of rows"] = dropped.get("company already has its share of rows", 0) + 1
            continue
        result.append(job)
    limit = cfg.get("max_rows", 250)
    if len(result) > limit:
        dropped["older than the newest rows shown"] = len(result) - limit
    return result[:limit], dropped


def _merge(first: list | None, second: list | None) -> list:
    out: list = []
    for item in (first or []) + (second or []):
        if item and item not in out:
            out.append(item)
    return out


# ── Render ───────────────────────────────────────────────────────────────────

def _amount(value: float, period: str) -> str:
    if period == "hourly":
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1000:
        thousands = value / 1000
        return f"{thousands:.1f}".rstrip("0").rstrip(".") + "k" if thousands < 10 else f"{round(thousands)}k"
    return f"{round(value)}"


def format_pay(pay: dict) -> str:
    symbol = SYMBOLS.get(pay["currency"], pay["currency"] + " ")
    period = pay["period"]
    low, high = pay.get("min"), pay.get("max")
    if low and high and low != high:
        text = f"{symbol}{_amount(low, period)}–{symbol}{_amount(high, period)}"
    else:
        text = f"{symbol}{_amount(low or high, period)}"
    return text + PERIOD_SUFFIX[period]


def md(text: str) -> str:
    """Make employer-written text safe inside a Markdown table cell."""
    text = re.sub(r"\s+", " ", str(text)).strip().replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\|*_`\[\]])", r"\\\1", text)


def _link(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def format_location(job: dict) -> str:
    places = job.get("locations") or []
    text = " · ".join(places[:2]) + (f" +{len(places) - 2} more" if len(places) > 2 else "")
    if job.get("remote_friendly") and "remote" not in text.lower():
        text = f"{text} · Remote" if text else "Remote"
    return text or "—"


def _row(job: dict, cfg: dict) -> str:
    posted = datetime.fromisoformat(job["posted_at"].replace("Z", "+00:00"))
    fit = f"{job['job_url']}?utm_source=github&utm_medium=list&utm_campaign={cfg['repo']}"
    return (f"| {md(job['company'])} | {md(job['title'])} | {md(format_location(job))} | {format_pay(job['salary'])} "
            f"| {posted.strftime('%b %-d')} | [Apply]({_link(job['apply_url'])}) · [Check my fit]({_link(fit)}) |")


def _table(jobs: list[dict], cfg: dict) -> list[str]:
    return ["| Company | Role | Location | Pay | Posted | Links |", "|---|---|---|---|---|---|",
            *[_row(job, cfg) for job in jobs]]


def _group_country(job: dict, cfg: dict) -> str:
    wanted = cfg.get("countries")
    for country in job.get("countries") or []:
        if not wanted or country in wanted:
            return country
    return "Other"


def render(jobs: list[dict], cfg: dict, now: datetime) -> str:
    companies = len({job["company"].lower() for job in jobs})
    lines = [f"# {cfg['title']}", "", cfg["intro"], "",
             f"**{len(jobs)} roles at {companies} companies.** Rebuilt {now.strftime('%-d %B %Y, %H:%M')} UTC "
             f"from jobs posted in the last {cfg.get('days', 14)} days. Pay is per year unless marked.", ""]
    if cfg.get("group_by") == "country":
        groups: dict[str, list[dict]] = {}
        for job in jobs:
            groups.setdefault(_group_country(job, cfg), []).append(job)
        for country, group in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
            lines += [f"## {country} ({len(group)})", "", *_table(group, cfg), ""]
    else:
        lines += [*_table(jobs, cfg), ""]
    lines += ["## How this list is built", "",
              *[f"- {point}" for point in cfg["method"]],
              "- The same rows are in [`jobs.json`](jobs.json) for anyone who wants the data.",
              "- Found a mistake? Open an issue.", ""]
    return "\n".join(lines)


def build(cfg: dict, key: str, now: datetime | None = None, get=api_get, pause: float = PAUSE_SECONDS):
    """Return (readme text, jobs.json text, rows, drop tally). Raises ListError instead of producing a thin list."""
    now = now or datetime.now(timezone.utc)
    rows, dropped = select(fetch_jobs(cfg, key, get=get, pause=pause), cfg)
    if len(rows) < cfg.get("min_rows", 10):
        raise ListError(f"only {len(rows)} jobs passed the rules (minimum {cfg.get('min_rows', 10)}); left out: {dropped}")
    fields = ("id", "company", "title", "locations", "countries", "remote_friendly", "salary",
              "posted_at", "last_checked_at", "apply_url", "job_url", "source")
    payload = {"list": cfg["repo"], "generated_at": now.isoformat(timespec="seconds"),
               "jobs": [{f: job.get(f) for f in fields} for job in rows]}
    return render(rows, cfg, now), json.dumps(payload, indent=2, ensure_ascii=False) + "\n", rows, dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=".", help="folder holding list.json (default: current folder)")
    args = parser.parse_args()
    folder = Path(args.dir)
    cfg = json.loads((folder / "list.json").read_text())
    key = os.environ.get("JOBSGLITCH_API_KEY", "")
    if not key:
        print("JOBSGLITCH_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        readme, data, rows, dropped = build(cfg, key)
    except ListError as exc:
        print(f"List not rebuilt: {exc}", file=sys.stderr)
        return 1
    (folder / "README.md").write_text(readme)
    (folder / "jobs.json").write_text(data)
    print(f"{cfg['repo']}: {len(rows)} rows written. Left out: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
