#!/usr/bin/env python3
"""Rebuild README.md and companies.json for a company-level list (one row per employer) from the
JobsGlitch public API's /lists endpoints.

Run inside a list's folder (it reads ./list.json):

    JOBSGLITCH_API_KEY=... python build_company_list.py

Standard library only. If the API fails or too few companies pass the rules, it exits non-zero
and leaves yesterday's files alone.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from build_list import ListError, _link, api_get, format_pay, md

REGIONS = (
    ("United States", {"United States"}),
    ("Canada", {"Canada"}),
    ("United Kingdom", {"United Kingdom"}),
    ("Europe", {"Germany", "France", "Netherlands", "The Netherlands", "Spain", "Ireland", "Sweden", "Switzerland",
                "Poland", "Portugal", "Italy", "Denmark", "Norway", "Finland", "Belgium", "Austria", "Czechia",
                "Czech Republic", "Romania", "Estonia", "Lithuania", "Latvia", "Greece", "Hungary", "Luxembourg",
                "Bulgaria", "Croatia", "Slovakia", "Slovenia", "Serbia", "Ukraine", "Cyprus", "Malta", "Iceland"}),
    ("India", {"India"}),
    ("Latin America", {"Mexico", "Brazil", "Argentina", "Colombia", "Chile", "Peru", "Uruguay", "Costa Rica",
                       "Ecuador", "Guatemala", "Panama", "Dominican Republic", "Puerto Rico"}),
    ("Asia Pacific", {"Australia", "New Zealand", "Singapore", "Philippines", "Japan", "Malaysia", "Indonesia",
                      "Vietnam", "Thailand", "South Korea", "Hong Kong", "Taiwan", "Pakistan", "Bangladesh",
                      "Sri Lanka", "China"}),
)
NOT_STATED = "Location not stated"
OTHER = "Other countries"


def region_of(company: dict) -> str:
    countries = company.get("countries") or []
    if not countries:
        return NOT_STATED
    first = str(countries[0]).strip().title().replace("The Netherlands", "Netherlands")
    for name, members in REGIONS:
        if first in {m.title() for m in members}:
            return name
    return OTHER


def reject_reason(company: dict, cfg: dict) -> str | None:
    name = str(company.get("company") or "").strip()
    if not name:
        return "no company name"
    if any(re.search(p, name, re.I) for p in cfg.get("company_exclude", [])):
        return "job board, staffing agency or middleman"
    if company.get("open_roles", 0) < cfg.get("min_roles", 1):
        return "fewer open roles than the list's minimum"
    examples = [e for e in company.get("examples") or []
                if str(e.get("apply_url") or "").startswith(("http://", "https://")) and e.get("title")]
    if not examples:
        return "no employer link"
    if cfg.get("require_pay_examples") and not any(e.get("salary") for e in examples):
        return "no example with posted pay"
    company["examples"] = examples
    return None


def select(companies: list[dict], cfg: dict) -> tuple[dict[str, list[dict]], dict[str, int]]:
    dropped: dict[str, int] = {}
    groups: dict[str, list[dict]] = {}
    for company in companies:
        reason = reject_reason(company, cfg)
        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
            continue
        groups.setdefault(region_of(company) if cfg.get("group_by") == "region" else "All", []).append(company)
    cap = cfg.get("max_per_group", 150)
    for name, rows in groups.items():
        rows.sort(key=lambda c: (-c["open_roles"], c["company"].lower()))
        if len(rows) > cap:
            dropped["beyond the rows shown per region (all are in companies.json)"] = (
                dropped.get("beyond the rows shown per region (all are in companies.json)", 0) + len(rows) - cap)
    return groups, dropped


def _fit_link(example: dict, cfg: dict) -> str:
    return f"{example['job_url']}?utm_source=github&utm_medium=list&utm_campaign={cfg['repo']}"


def _role(example: dict, cfg: dict, with_pay: bool) -> str:
    text = f"[{md(example['title'])}]({_link(example['apply_url'])})"
    if with_pay and example.get("salary") and example["salary"].get("currency") and example["salary"].get("period") in (
            "yearly", "monthly", "weekly", "daily", "hourly"):
        text += f" {format_pay(example['salary'])}"
    return text


def _row(company: dict, cfg: dict) -> str:
    shown = company["examples"][:cfg.get("examples", 2)]
    roles = "<br>".join(_role(e, cfg, with_pay=cfg["kind"] == "pay-companies") for e in shown)
    fit = f"[Check my fit]({_link(_fit_link(shown[0], cfg))})"
    if cfg["kind"] == "pay-companies":
        total = company.get("all_open_roles") or 0
        share = f"{round(100 * company['open_roles'] / total)}%" if total >= company["open_roles"] and total else "—"
        return f"| {md(company['company'])} | {company['open_roles']} | {share} | {roles} | {fit} |"
    pay = f"{company.get('roles_with_pay', 0)} of {company['open_roles']}"
    return f"| {md(company['company'])} | {company['open_roles']} | {pay} | {roles} | {fit} |"


def _header(cfg: dict) -> list[str]:
    if cfg["kind"] == "pay-companies":
        return ["| Company | Roles showing pay | Share of its open roles | Recent roles and posted pay | |",
                "|---|---|---|---|---|"]
    return ["| Company | Open remote roles | Roles showing pay | Recent remote roles | |", "|---|---|---|---|---|"]


def _anchor(title: str) -> str:
    return re.sub(r"[^a-z0-9 -]", "", title.lower()).replace(" ", "-")


def render(groups: dict[str, list[dict]], cfg: dict, data: dict, now: datetime) -> str:
    order = [name for name, _ in REGIONS] + [OTHER, NOT_STATED, "All"]
    names = [n for n in order if n in groups]
    cap = cfg.get("max_per_group", 150)
    listed = sum(len(groups[n]) for n in names)
    lines = [f"# {cfg['title']}", "", cfg["intro"], "",
             f"**{listed:,} {'company' if listed == 1 else 'companies'} hiring now** (open roles verified on the employer's own site in the last 7 days). "
             f"Rebuilt {now.strftime('%-d %B %Y')}. Pay is per year unless marked.", ""]
    if len(names) > 1:
        lines += ["## Contents", "", *[f"- [{n}](#{_anchor(n)}-{len(groups[n])})" for n in names], ""]
    for name in names:
        rows = groups[name]
        if len(names) > 1:
            lines += [f"## {name} ({len(rows)})", ""]
        lines += [*_header(cfg), *[_row(c, cfg) for c in rows[:cap]], ""]
        if len(rows) > cap:
            lines += [f"Showing the {cap} companies with the most open roles; all {len(rows)} are in "
                      f"[`companies.json`](companies.json).", ""]
    lines += ["## How this list is built", "", *[f"- {p}" for p in cfg["method"]],
              "- Every company here is in [`companies.json`](companies.json), with up to three recent roles each.",
              "- Found a mistake, or a company that should not be here? Open an issue.", ""]
    return "\n".join(lines)


def build(cfg: dict, key: str, now: datetime | None = None, get=api_get):
    now = now or datetime.now(timezone.utc)
    data = get(f"/lists/{cfg['kind']}", {"limit": cfg.get("limit", 3000)}, key)
    if data.get("kind") != cfg["kind"] or not isinstance(data.get("companies"), list):
        raise ListError(f"unexpected answer from /lists/{cfg['kind']}")
    groups, dropped = select(data["companies"], cfg)
    listed = sum(len(rows) for rows in groups.values())
    if listed < cfg.get("min_companies", 50):
        raise ListError(f"only {listed} companies passed the rules (minimum {cfg.get('min_companies', 50)}); left out: {dropped}")
    payload = {"list": cfg["repo"], "generated_at": now.isoformat(timespec="seconds"),
               "companies": [{**c, "region": name} for name, rows in groups.items() for c in rows]}
    return render(groups, cfg, data, now), json.dumps(payload, indent=2, ensure_ascii=False) + "\n", listed, dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", default=".", help="folder holding list.json (default: current folder)")
    folder = Path(parser.parse_args().dir)
    cfg = json.loads((folder / "list.json").read_text())
    key = os.environ.get("JOBSGLITCH_API_KEY", "")
    if not key:
        print("JOBSGLITCH_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        readme, data, listed, dropped = build(cfg, key)
    except ListError as exc:
        print(f"List not rebuilt: {exc}", file=sys.stderr)
        return 1
    (folder / "README.md").write_text(readme)
    (folder / "companies.json").write_text(data)
    print(f"{cfg['repo']}: {listed} companies written. Left out: {dropped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
