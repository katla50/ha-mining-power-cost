#!/usr/bin/env python3
"""Mining power cost — daily ledger and yearly report from Home Assistant.

Source of truth: Home Assistant long-term statistics
(``recorder/statistics_during_period``). Long-term statistics are kept forever
(recorder purge does not touch them), so the ledger can be rebuilt at any time
and yields identical numbers. That also makes it self-healing: if the machine
was off for two weeks, the next run fills in every missing day.

Commands:
    mining_power_cost.py log       # add every complete day that is missing
    mining_power_cost.py log --from 2026-01-01 --to 2026-06-30
    mining_power_cost.py report    # write HTML + PDF + monthly summary CSV
    mining_power_cost.py show      # print the most recent rows

Files in the same directory:
    config.json      miners, sensors and electricity price(s)   (not in git)
    ledger.csv       main ledger: one row per date per miner
    house.csv        daily whole-house consumption (cross-check, optional)
    comparison.csv   days measured by two sources at once (source-change audit)
    reports/         generated reports
    .env             HASS_URL + HASS_TOKEN                      (not in git)
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
LEDGER = BASE / "ledger.csv"
HOUSE_CSV = BASE / "house.csv"
COMPARISON_CSV = BASE / "comparison.csv"
REPORT_DIR = BASE / "reports"
ENV_FILE = BASE / ".env"

FIELDS = ["date", "meter_id", "kwh", "hours_covered", "price_per_kwh",
          "cost", "source", "status", "comment"]

STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_NO_DATA = "no_data"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def read_env() -> tuple[str, str]:
    """HASS_URL and HASS_TOKEN from the environment or a local .env file."""
    url = os.environ.get("HASS_URL")
    token = os.environ.get("HASS_TOKEN")
    if not (url and token) and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if key.strip() == "HASS_URL" and not url:
                url = value
            elif key.strip() == "HASS_TOKEN" and not token:
                token = value
    if not (url and token):
        sys.exit("Missing HASS_URL / HASS_TOKEN (environment or a .env file "
                 "next to this script).")
    return url.rstrip("/"), token


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        sys.exit(f"{CONFIG_FILE.name} not found — copy config.example.json "
                 "to config.json and edit it.")
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def price_for(day: dt.date, config: dict) -> tuple[float, str]:
    """Effective price for a date: the last price entry that starts on/before it."""
    prices = sorted(config["prices"], key=lambda p: p["from"])
    chosen = prices[0]
    for p in prices:
        if dt.date.fromisoformat(p["from"]) <= day:
            chosen = p
    return float(chosen["price_per_kwh"]), chosen.get("basis", "")


def sources_for(miner: dict) -> list[dict]:
    """Measurement sources for one miner, oldest first.

    A miner may have several sources over time (e.g. the miner's own telemetry
    first, a plug-in energy meter later). Every day is measured with the source
    that was active that day, so history is never rewritten when the measuring
    point changes.
    """
    if miner.get("sources"):
        return sorted(miner["sources"], key=lambda s: s["from"])
    return [{"from": "1970-01-01", "type": miner["type"],
             "entity": miner["entity"], "note": miner.get("note", "")}]


def source_for_day(miner: dict, day: dt.date) -> dict:
    chosen = sources_for(miner)[0]
    for source in sources_for(miner):
        if dt.date.fromisoformat(source["from"]) <= day:
            chosen = source
    return chosen


# --------------------------------------------------------------------------- #
# Home Assistant statistics
# --------------------------------------------------------------------------- #
async def fetch_hourly_statistics(entities: list[str], start: dt.datetime,
                                  end: dt.datetime) -> dict[str, list[dict]]:
    """Hourly long-term statistics per entity.

    Note: ``period="day"`` is unreliable on some Home Assistant builds (it can
    return a handful of rows for a whole year), so always ask for ``hour`` and
    aggregate locally.
    """
    import aiohttp

    url, token = read_env()
    ws_url = url.replace("http", "ws", 1) + "/api/websocket"
    out: dict[str, list[dict]] = {e: [] for e in entities}

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, max_msg_size=128 * 1024 * 1024) as ws:
            await ws.receive_json()  # auth_required
            await ws.send_json({"type": "auth", "access_token": token})
            hello = await ws.receive_json()
            if hello.get("type") != "auth_ok":
                sys.exit(f"Home Assistant login failed: {hello}")

            # 30-day chunks keep the responses small and the memory flat
            chunk_start = start
            msg_id = 1
            while chunk_start < end:
                chunk_end = min(chunk_start + dt.timedelta(days=30), end)
                await ws.send_json({
                    "id": msg_id,
                    "type": "recorder/statistics_during_period",
                    "start_time": chunk_start.isoformat(),
                    "end_time": chunk_end.isoformat(),
                    "statistic_ids": entities,
                    "period": "hour",
                    "types": ["mean", "change", "state", "sum"],
                })
                while True:
                    message = await ws.receive_json()
                    if message.get("id") != msg_id:
                        continue
                    if not message.get("success"):
                        sys.exit(f"Home Assistant statistics failed: {message.get('error')}")
                    for entity, rows in (message.get("result") or {}).items():
                        out.setdefault(entity, []).extend(rows)
                    break
                msg_id += 1
                chunk_start = chunk_end
    return out


def kwh_per_day(rows: list[dict] | None, source_type: str) -> dict[dt.date, tuple[float, float]]:
    """{local date: (kWh, hours covered)}.

    Power sensors (state_class measurement, unit W) are integrated with the
    hourly mean; energy sensors (cumulative kWh) use the hourly change.
    """
    per_day: dict[dt.date, list[float]] = {}
    if not rows:
        return {}
    for row in rows:
        start = dt.datetime.fromtimestamp(row["start"] / 1000, dt.timezone.utc)
        end = dt.datetime.fromtimestamp(row["end"] / 1000, dt.timezone.utc)
        hours = (end - start).total_seconds() / 3600
        local_date = start.astimezone().date()
        if source_type == "power":
            mean = row.get("mean")
            if mean is None:
                continue
            kwh = float(mean) * hours / 1000.0
        else:
            change = row.get("change")
            if change is None:
                continue
            # cumulative counters can be reset (meter swap, power cycle)
            kwh = max(float(change), 0.0)
        bucket = per_day.setdefault(local_date, [0.0, 0.0])
        bucket[0] += kwh
        bucket[1] += hours
    return {d: (v[0], v[1]) for d, v in per_day.items()}


# --------------------------------------------------------------------------- #
# CSV storage
# --------------------------------------------------------------------------- #
def read_ledger() -> dict[tuple[str, str], dict]:
    rows: dict[tuple[str, str], dict] = {}
    if not LEDGER.exists():
        return rows
    with LEDGER.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows[(row["date"], row["meter_id"])] = row
    return rows


def write_ledger(rows: dict[tuple[str, str], dict]) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for key in sorted(rows):
            writer.writerow({field: rows[key].get(field, "") for field in FIELDS})


def write_house(per_day: dict[dt.date, tuple[float, float]]) -> None:
    rows: dict[str, str] = {}
    if HOUSE_CSV.exists():
        with HOUSE_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows[row["date"]] = row["kwh_house"]
    for day, (kwh, hours) in per_day.items():
        if hours >= 22:
            rows[day.isoformat()] = f"{kwh:.3f}"
    with HOUSE_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["date", "kwh_house"])
        for day in sorted(rows):
            writer.writerow([day, rows[day]])


# --------------------------------------------------------------------------- #
# command: log
# --------------------------------------------------------------------------- #
def cmd_log(date_from: str | None, date_to: str | None) -> None:
    config = load_config()
    miners = config["miners"]
    ledger = read_ledger()

    today = dt.datetime.now().astimezone().date()
    start_date = (dt.date.fromisoformat(date_from) if date_from
                  else dt.date.fromisoformat(config.get("start_date", "2025-01-01")))
    end_date = (dt.date.fromisoformat(date_to) if date_to
                else today - dt.timedelta(days=1))
    if end_date < start_date:
        print("No complete days to log yet.")
        return

    # always fetch the whole window: the ledger and the cross-check must both be
    # reproducible, and the query only takes a few seconds
    start_dt = dt.datetime.combine(start_date, dt.time.min).astimezone()
    end_dt = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min).astimezone()

    all_sources = {s["entity"] for m in miners for s in sources_for(m)}
    entities = sorted(all_sources | {config["house_entity"]})
    print(f"Fetching statistics {start_date} .. {end_date} from Home Assistant ...")
    stats = asyncio.run(fetch_hourly_statistics(entities, start_dt, end_dt))

    written = 0
    for miner in miners:
        per_source = {s["entity"]: kwh_per_day(stats.get(s["entity"]), s["type"])
                      for s in sources_for(miner)}
        day = start_date
        while day <= end_date:
            source = source_for_day(miner, day)
            kwh, hours = per_source[source["entity"]].get(day, (0.0, 0.0))
            if hours >= 22:
                status, comment = STATUS_COMPLETE, ""
            elif hours > 0:
                status = STATUS_PARTIAL
                comment = f"{hours:.0f} of 24 hours with data"
            else:
                status = STATUS_NO_DATA
                comment = "no measurement — miner off or sensor unavailable"
            price, _basis = price_for(day, config)
            ledger[(day.isoformat(), miner["id"])] = {
                "date": day.isoformat(),
                "meter_id": miner["id"],
                "kwh": f"{kwh:.3f}",
                "hours_covered": f"{hours:.0f}",
                "price_per_kwh": f"{price:.2f}",
                "cost": f"{kwh * price:.2f}",
                "source": source["entity"],
                "status": status,
                "comment": comment,
            }
            written += 1
            day += dt.timedelta(days=1)

    # days where more than one source measured at the same time: keep both, so
    # the deviation between them (e.g. miner telemetry vs wall meter) stays
    # documented over time instead of being a one-off claim
    comparison = []
    for miner in miners:
        sources = sources_for(miner)
        if len(sources) < 2:
            continue
        per_source = {s["entity"]: kwh_per_day(stats.get(s["entity"]), s["type"])
                      for s in sources}
        # only compare from the day the newest source arrived — before that the
        # old sensor was measuring something else entirely
        switchover = dt.date.fromisoformat(sources[-1]["from"])
        day = max(start_date, switchover)
        while day <= end_date:
            values = {entity: per_day[day][0] for entity, per_day in per_source.items()
                      if per_day.get(day, (0, 0))[1] >= 22}
            if len(values) >= 2:
                for entity, kwh in values.items():
                    comparison.append({"date": day.isoformat(), "meter_id": miner["id"],
                                       "source": entity, "kwh": f"{kwh:.3f}"})
            day += dt.timedelta(days=1)
    if comparison:
        with COMPARISON_CSV.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["date", "meter_id", "source", "kwh"],
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(comparison)

    write_ledger(ledger)
    try:
        write_house(kwh_per_day(stats.get(config["house_entity"]), "energy"))
    except Exception as exc:  # the cross-check must never break the ledger
        print(f"Warning: house consumption not written: {exc}")

    print(f"Wrote/updated {written} daily rows in {LEDGER}")


# --------------------------------------------------------------------------- #
# report labels
# --------------------------------------------------------------------------- #
LABELS = {
    "en": {
        "html_lang": "en",
        "title": "Mining power cost {year}",
        "generated": "Generated {when} · source: Home Assistant long-term statistics · "
                     "ledger: {ledger} ({rows} daily rows)",
        "month": "Month",
        "mining_sum": "Mining sum<br><span class='small'>kWh / cost</span>",
        "share": "Share of<br>household use",
        "house_total": "Household<br><span class='small'>kWh</span>",
        "total_row": "Total {year}",
        "method_heading": "Basis and method",
        "price_label": "Electricity price",
        "price_text": "{prices} per kWh including grid fees and VAT ({basis}).",
        "consumption_label": "Consumption",
        "consumption_text": "measured per miner at hourly resolution from Home Assistant. "
                            "Power sensors are integrated to kWh hour by hour; energy meters "
                            "use the difference in cumulative kWh.",
        "cost_label": "Cost",
        "cost_text": "kWh × price per kWh, per day. No mark-up, rounded to 2 decimals.",
        "total_cost_label": "Total cost {year}",
        "total_cost_text": "{cost} for {kwh} kWh.",
        "check_label": "Cross-check",
        "check_text": "the share column compares against the whole-house meter ({entity}). "
                      "It is a sanity check only — the share must stay below 100 % — and is "
                      "shown for months where the whole-house data is complete.",
        "source_label": "Measuring point",
        "source_text": "{points}.",
        "trace_label": "Traceability",
        "trace_text": "every row in {ledger} can be recreated from Home Assistant long-term "
                      "statistics for the same period; the data is not edited afterwards.",
        "coverage_label": "Data coverage {year}",
        "coverage_text": "{coverage}. Days without a measurement are recorded as 0 kWh and "
                         "flagged 'no_data' (miner off or sensor not responding) — they are "
                         "not estimated.",
        "definitions": "Field definitions",
        "field": "Field",
        "meaning": "Meaning",
        "def_date": "local calendar day, 00:00–24:00",
        "def_meter": "which miner the measurement belongs to",
        "def_kwh": "consumption that day, 3 decimals",
        "def_hours": "hours with data that day (24 = full coverage)",
        "def_price": "applied price per kWh, including grid fees and VAT",
        "def_cost": "kwh × price_per_kwh",
        "def_source": "the Home Assistant entity that was measured",
        "def_status": "complete (24 h of data) / partial / no_data",
        "from": "from",
        "until": "until",
        "today": "today",
        "days_complete": "{n} days with full coverage",
        "days_partial": ", {n} partial",
        "days_missing": ", {n} without measurement",
        "misc_basis": "fixed-price agreement",
        "months_csv": "month",
        "kwh_col": "kwh",
        "cost_col": "cost",
        "house_col": "house_kwh",
        "sum_row": "total {year}",
        "not_made": "not created",
    },
    "no": {
        "html_lang": "nb",
        "title": "Strømkostnad mining {year}",
        "generated": "Generert {when} · kilde: Home Assistant-langtidsstatistikk · "
                     "logg: {ledger} ({rows} dagslinjer)",
        "month": "Måned",
        "mining_sum": "Sum mining<br><span class='small'>kWh / kr</span>",
        "share": "Andel av<br>husets forbruk",
        "house_total": "Huset totalt<br><span class='small'>kWh</span>",
        "total_row": "Sum {year}",
        "method_heading": "Grunnlag og metode",
        "price_label": "Strømpris",
        "price_text": "{prices} kr/kWh inkl. nettleie og mva ({basis}).",
        "consumption_label": "Forbruk",
        "consumption_text": "målt per miner, timesoppløsning fra Home Assistant. "
                            "Effektbaserte sensorer er omregnet til kWh time for time; "
                            "energibaserte målere bruker differanse i kumulativ kWh.",
        "cost_label": "Kostnad",
        "cost_text": "kWh × strømpris per dag. Ingen påslag, avrundet til 2 desimaler.",
        "total_cost_label": "Total kostnad {year}",
        "total_cost_text": "{cost} kr for {kwh} kWh.",
        "check_label": "Kontroll",
        "check_text": "kolonnen «Andel av husets forbruk» sammenligner med husets "
                      "totalmåler ({entity}). Det er bare en rimelighetssjekk – andelen skal "
                      "ligge under 100 % – og vises for måneder med komplette data.",
        "source_label": "Målepunkt",
        "source_text": "{points}.",
        "trace_label": "Sporbarhet",
        "trace_text": "hver linje i {ledger} kan gjenskapes fra Home Assistants "
                      "langtidsstatistikk for samme tidsrom; dataene er ikke redigert i ettertid.",
        "coverage_label": "Datadekning {year}",
        "coverage_text": "{coverage}. Dager uten måling føres med 0 kWh og merkes «no_data» "
                         "(miner av eller sensor som ikke svarer) – de er ikke estimert.",
        "definitions": "Definisjoner",
        "field": "Felt",
        "meaning": "Betydning",
        "def_date": "lokal kalenderdag, 00:00–24:00",
        "def_meter": "hvilken miner målingen gjelder",
        "def_kwh": "forbruk den dagen, 3 desimaler",
        "def_hours": "antall timer med data den dagen (24 = full dekning)",
        "def_price": "anvendt strømpris per kWh, inkl. nettleie og mva",
        "def_cost": "kwh × price_per_kwh",
        "def_source": "Home Assistant-entiteten som er målt",
        "def_status": "complete (24 t data) / partial / no_data",
        "from": "fra",
        "until": "til",
        "today": "i dag",
        "days_complete": "{n} dager med full dekning",
        "days_partial": ", {n} delvise",
        "days_missing": ", {n} uten måling",
        "misc_basis": "fastprisavtale",
        "months_csv": "maaned",
        "kwh_col": "kwh",
        "cost_col": "kr",
        "house_col": "hus_kwh",
        "sum_row": "sum {year}",
        "not_made": "ikke laget",
    },
}


def format_number(value: float, decimals: int, language: str) -> str:
    """1 234,5 (nb) / 1,234.5 (en). Non-breaking space as thousands separator,
    otherwise the report table breaks numbers over two lines."""
    text = f"{value:,.{decimals}f}"
    if language == "no":
        return text.replace(",", "\u00a0").replace(".", ",")
    return text


# --------------------------------------------------------------------------- #
# command: report
# --------------------------------------------------------------------------- #
def cmd_report(year: int | None) -> None:
    config = load_config()
    language = config.get("language", "en")
    labels = LABELS.get(language, LABELS["en"])
    ledger = read_ledger()
    if not ledger:
        sys.exit("The ledger is empty — run 'log' first.")

    year = year or dt.datetime.now().astimezone().year
    rows = [r for r in ledger.values() if r["date"].startswith(str(year))]
    if not rows:
        sys.exit(f"No rows for {year}.")

    names = {m["id"]: m["name"] for m in config["miners"]}
    months: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        month = row["date"][:7]
        bucket = months.setdefault(month, {})
        entry = bucket.setdefault(row["meter_id"], [0.0, 0.0])
        entry[0] += float(row["kwh"])
        entry[1] += float(row["cost"])

    house: dict[str, float] = {}
    house_days: dict[str, int] = {}
    if HOUSE_CSV.exists():
        with HOUSE_CSV.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["date"].startswith(str(year)) and row["kwh_house"]:
                    month = row["date"][:7]
                    house[month] = house.get(month, 0.0) + float(row["kwh_house"])
                    house_days[month] = house_days.get(month, 0) + 1

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now().astimezone()

    def fmt(value: float, decimals: int = 1) -> str:
        return format_number(value, decimals, language)

    totals: dict[str, list[float]] = {}
    for month in sorted(months):
        for meter_id, (kwh, cost) in months[month].items():
            entry = totals.setdefault(meter_id, [0.0, 0.0])
            entry[0] += kwh
            entry[1] += cost

    counts = {STATUS_COMPLETE: 0, STATUS_PARTIAL: 0, STATUS_NO_DATA: 0}
    for row in rows:
        status = row.get("status") or STATUS_COMPLETE
        counts[status] = counts.get(status, 0) + 1
    coverage = labels["days_complete"].format(n=counts[STATUS_COMPLETE])
    if counts[STATUS_PARTIAL]:
        coverage += labels["days_partial"].format(n=counts[STATUS_PARTIAL])
    if counts[STATUS_NO_DATA]:
        coverage += labels["days_missing"].format(n=counts[STATUS_NO_DATA])

    # which measuring point was used when — essential for traceability
    points = []
    for miner in config["miners"]:
        sources = sources_for(miner)
        for index, source in enumerate(sources):
            start_text = (labels["from"] if source["from"] == "1970-01-01"
                          else dt.date.fromisoformat(source["from"]).strftime("%Y-%m-%d"))
            if index + 1 < len(sources):
                end_text = (dt.date.fromisoformat(sources[index + 1]["from"])
                            - dt.timedelta(days=1)).strftime("%Y-%m-%d")
            else:
                end_text = labels["today"]
            note = f" — {source['note']}" if source.get("note") else ""
            points.append(f"{miner['name']}: <code>{source['entity']}</code> "
                          f"({start_text}–{end_text}){note}")
    measuring_point = "; ".join(points)

    header = "".join(f"<th>{names.get(meter_id, meter_id)}<br>"
                     f"<span class='small'>kWh / {labels['cost_col']}</span></th>"
                     for meter_id in names)
    used = [meter_id for meter_id in names
            if any(meter_id in bucket for bucket in months.values())]

    # one decision per month, used both for the row and for the total, so the
    # total can never include a month the table shows as "–"
    house_ok: dict[str, bool] = {}
    for month in months:
        days_in_month = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
        mine = sum(value[0] for value in months[month].values())
        house_kwh = house.get(month, 0.0)
        house_ok[month] = (house_days.get(month, 0) >= 0.8 * days_in_month
                           and mine <= house_kwh)

    table_rows = []
    for month in sorted(months):
        cells = []
        month_kwh = 0.0
        month_cost = 0.0
        for meter_id in used:
            kwh, cost = months[month].get(meter_id, (0.0, 0.0))
            month_kwh += kwh
            month_cost += cost
            cells.append(f"<td>{fmt(kwh)}<br><span class='small'>{fmt(cost, 0)}</span></td>")
        house_kwh = house.get(month, 0.0)
        share_cell = (f"<td>{fmt(100 * month_kwh / house_kwh, 0)} %</td>"
                      if house_ok[month] else "<td>–</td>")
        house_cell = (f"<td>{fmt(house_kwh)}</td>" if house_ok[month] else "<td>–</td>")
        table_rows.append(
            f"<tr><td>{month}</td>{''.join(cells)}"
            f"<td class='sum'>{fmt(month_kwh)}<br>"
            f"<span class='small'>{fmt(month_cost, 0)}</span></td>"
            f"{share_cell}{house_cell}</tr>")

    total_cells = "".join(
        f"<td class='sum'>{fmt(totals[meter_id][0])}<br>"
        f"<span class='small'>{fmt(totals[meter_id][1], 0)}</span></td>"
        for meter_id in used)
    house_total_ok = sum(house[m] for m in house if house_ok.get(m))
    total_kwh = sum(value[0] for value in totals.values())
    total_cost = sum(value[1] for value in totals.values())
    prices = sorted({row["price_per_kwh"] for row in rows})

    html = f"""<!doctype html><html lang="{labels['html_lang']}"><head><meta charset="utf-8">
<title>{labels['title'].format(year=year)}</title>
<style>
 @page {{ size: A4; margin: 14mm; }}
 body {{ font-family: "DejaVu Sans", Arial, sans-serif; font-size: 10.5pt; color:#111; margin: 18px; }}
 h1 {{ font-size: 18pt; margin: 0 0 4px 0; }}
 h2 {{ font-size: 13pt; margin: 22px 0 8px 0; }}
 .small {{ font-size: 8.5pt; color:#555; }}
 table {{ border-collapse: collapse; width: 100%; }}
 th, td {{ border-bottom: 1px solid #ccc; padding: 5px 6px; text-align: right;
   white-space: nowrap; }}
 th {{ background: #f0f0f0; text-align: center; font-weight: 600; }}
 td:first-child, th:first-child {{ text-align: left; }}
 tfoot td {{ font-weight: 700; border-top: 2px solid #333; background:#fafafa; }}
 .sum {{ background:#fafafa; }}
 .boks {{ border:1px solid #ccc; padding:10px 12px; margin-top:14px; break-inside: avoid; }}
 ul {{ margin:6px 0 0 18px; padding:0; }}
 li {{ margin-bottom:3px; }}
 code {{ font-size: 9pt; }}
 h2, .boks {{ break-after: avoid; }}
 .defs {{ break-inside: avoid; }}
</style></head><body>
<h1>{labels['title'].format(year=year)}</h1>
<div class="small">{labels['generated'].format(when=now.strftime('%Y-%m-%d %H:%M'),
                                               ledger=LEDGER.name, rows=len(rows))}</div>

<table>
<thead><tr><th>{labels['month']}</th>{header}<th>{labels['mining_sum']}</th>
<th>{labels['share']}</th><th>{labels['house_total']}</th></tr></thead>
<tbody>{''.join(table_rows)}</tbody>
<tfoot><tr><td>{labels['total_row'].format(year=year)}</td>{total_cells}
<td class="sum">{fmt(total_kwh)}<br><span class="small">{fmt(total_cost, 0)}</span></td>
<td></td><td>{fmt(house_total_ok)}</td></tr></tfoot>
</table>

<div class="boks">
<b>{labels['method_heading']}</b>
<ul>
<li><b>{labels['price_label']}:</b> {labels['price_text'].format(prices=', '.join(prices), basis=labels['misc_basis'])}</li>
<li><b>{labels['consumption_label']}:</b> {labels['consumption_text']}</li>
<li><b>{labels['cost_label']}:</b> {labels['cost_text']}</li>
<li><b>{labels['total_cost_label'].format(year=year)}:</b> {labels['total_cost_text'].format(cost=fmt(total_cost, 2), kwh=fmt(total_kwh))}</li>
<li><b>{labels['check_label']}:</b> {labels['check_text'].format(entity=config['house_entity'])}</li>
<li><b>{labels['source_label']}:</b> {labels['source_text'].format(points=measuring_point)}</li>
<li><b>{labels['trace_label']}:</b> {labels['trace_text'].format(ledger=LEDGER.name)}</li>
<li><b>{labels['coverage_label'].format(year=year)}:</b> {labels['coverage_text'].format(coverage=coverage)}</li>
</ul>
</div>

<div class="defs"><h2>{labels['definitions']}</h2>
<table><thead><tr><th style="text-align:left">{labels['field']}</th>
<th style="text-align:left">{labels['meaning']}</th></tr></thead><tbody>
<tr><td>date</td><td style="text-align:left">{labels['def_date']}</td></tr>
<tr><td>meter_id</td><td style="text-align:left">{labels['def_meter']}</td></tr>
<tr><td>kwh</td><td style="text-align:left">{labels['def_kwh']}</td></tr>
<tr><td>hours_covered</td><td style="text-align:left">{labels['def_hours']}</td></tr>
<tr><td>price_per_kwh</td><td style="text-align:left">{labels['def_price']}</td></tr>
<tr><td>cost</td><td style="text-align:left">{labels['def_cost']}</td></tr>
<tr><td>source</td><td style="text-align:left">{labels['def_source']}</td></tr>
<tr><td>status</td><td style="text-align:left">{labels['def_status']}</td></tr>
</tbody></table></div>
</body></html>"""

    html_file = REPORT_DIR / f"mining_power_cost_{year}.html"
    html_file.write_text(html, encoding="utf-8")

    csv_file = REPORT_DIR / f"mining_power_cost_{year}_months.csv"
    with csv_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow([labels["months_csv"]]
                        + [f"{meter_id}_{labels['kwh_col']}" for meter_id in used]
                        + [f"{meter_id}_{labels['cost_col']}" for meter_id in used]
                        + ["sum_kwh", "sum_cost", labels["house_col"]])
        for month in sorted(months):
            kwhs = [months[month].get(m, (0.0, 0.0))[0] for m in used]
            costs = [months[month].get(m, (0.0, 0.0))[1] for m in used]
            writer.writerow([month] + [f"{v:.1f}" for v in kwhs] + [f"{v:.0f}" for v in costs]
                            + [f"{sum(kwhs):.1f}", f"{sum(costs):.0f}",
                               f"{house.get(month, 0.0):.1f}"])
        writer.writerow([labels["sum_row"].format(year=year)]
                        + [f"{totals[m][0]:.1f}" for m in used]
                        + [f"{totals[m][1]:.0f}" for m in used]
                        + [f"{total_kwh:.1f}", f"{total_cost:.0f}",
                           f"{sum(house.values()):.1f}"])

    pdf_file = REPORT_DIR / f"mining_power_cost_{year}.pdf"
    created = False
    # A Chromium-based browser renders the CSS correctly; LibreOffice mangles
    # the table columns, so it is only the fallback.
    for browser in ("chromium", "chromium-browser", "google-chrome-stable", "brave"):
        if not shutil.which(browser):
            continue
        try:
            subprocess.run([browser, "--headless", "--disable-gpu", "--no-sandbox",
                            "--no-pdf-header-footer",
                            f"--print-to-pdf={pdf_file}", f"file://{html_file}"],
                           check=True, capture_output=True, timeout=120)
            created = pdf_file.exists()
            if created:
                break
        except Exception as exc:
            print(f"({browser} failed: {exc})")
    if not created:
        try:
            subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                            "--outdir", str(REPORT_DIR), str(html_file)],
                           check=True, capture_output=True, timeout=120)
        except Exception as exc:
            print(f"(no PDF produced: {exc})")

    print(f"Report: {html_file}")
    print(f"PDF:    {pdf_file if pdf_file.exists() else labels['not_made']}")
    print(f"CSV:    {csv_file}")
    print(f"\n{year}: {total_kwh:.1f} kWh -> {total_cost:.2f}")


# --------------------------------------------------------------------------- #
def cmd_show(limit: int) -> None:
    ledger = read_ledger()
    rows = [ledger[key] for key in sorted(ledger)][-limit:]
    print(f"{'date':12} {'miner':8} {'kWh':>8} {'per_kWh':>8} {'cost':>9}  {'status':10} comment")
    for row in rows:
        print(f"{row['date']:12} {row['meter_id']:8} {row['kwh']:>8} "
              f"{row['price_per_kwh']:>8} {row['cost']:>9}  "
              f"{row.get('status', ''):10} {row['comment']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Electricity cost ledger for Bitcoin miners, from Home Assistant.")
    sub = parser.add_subparsers(dest="command", required=True)

    log = sub.add_parser("log", help="fill in every complete day that is missing")
    log.add_argument("--from", dest="date_from", help="first date YYYY-MM-DD")
    log.add_argument("--to", dest="date_to", help="last date YYYY-MM-DD (default: yesterday)")

    report = sub.add_parser("report", help="write HTML + PDF + monthly CSV")
    report.add_argument("--year", type=int)

    show = sub.add_parser("show", help="print the most recent ledger rows")
    show.add_argument("--limit", type=int, default=14)

    args = parser.parse_args()
    if args.command == "log":
        cmd_log(args.date_from, args.date_to)
    elif args.command == "report":
        cmd_report(args.year)
    else:
        cmd_show(args.limit)


if __name__ == "__main__":
    main()
