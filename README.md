# ha-mining-power-cost

A small, dependency-light tool that turns **Home Assistant** energy data into a
documented electricity-cost ledger for Bitcoin miners — day by day, per miner,
with a printable yearly report you can hand to an accountant or a tax authority.

It answers one question with evidence: *how many kWh did the miners actually
use, and what did that cost?*

```
date        meter_id      kwh  hours_covered  price_per_kwh    cost  source                              status    comment
2026-10-27  s9          7.453             15           1.10    8.20  sensor.s9_power                     partial   15 of 24 hours with data
2026-10-28  s9         11.620             24           1.10   12.78  sensor.miner_plug_power             complete
```

- **Auditable by construction.** Every row is derived from Home Assistant
  *long-term statistics*, which are kept forever and never rewritten. Re-running
  the tool reproduces identical numbers, and the whole ledger can be rebuilt
  from scratch if the CSV is lost.
- **Honest about gaps.** A day with no measurement is recorded as `0 kWh` with
  status `no_data` — never estimated. The report states how many days are
  complete, partial and missing.
- **Source changes are tracked, not hidden.** If you start measuring with a
  plug-in meter months after you started mining, each day is logged with the
  sensor that was active that day, and days measured by two sources at once are
  written to `comparison.csv`.
- **No database, no cloud.** One Python file, one CSV, one PDF.

## How it works

```
Home Assistant  ──►  recorder/statistics_during_period (WebSocket, hourly)
                            │
                            ▼
              power sensor (W)  →  kWh = Σ hourly mean × hours / 1000
              energy sensor (kWh) → kWh = hourly change (counter resets ignored)
                            │
                            ▼
                   ledger.csv  ──►  reports/*.pdf + *.csv
```

Each hourly bucket is assigned to the **local calendar day** it starts in, so
the ledger follows your wall clock (a DST day is 23 or 25 hours, not 24).

## Requirements

- Python 3.9+ with [`aiohttp`](https://docs.aiohttp.org/) (one dependency; on
  Arch: `sudo pacman -S python-aiohttp`).
- Home Assistant reachable over HTTP(S) and a long-lived access token
  (*Profile → Security → Long-lived access tokens*).
- A sensor per miner with `state_class` set:
  - `power` — instantaneous watts (`device_class: power`, unit `W`), or
  - `energy` — cumulative kWh (`device_class: energy`, unit `kWh`).
  Miner telemetry over SNMP/API works, but a plug-in energy meter at the wall
  socket is the stronger evidence (see *Which sensor to use*).
- Optional: `chromium` for good-looking PDFs (`libreoffice` is the fallback and
  renders tables poorly).

## Installation

```sh
git clone https://github.com/<you>/ha-mining-power-cost.git
cd ha-mining-power-cost

cp config.example.json config.json
cp .env.example .env          # or export HASS_URL / HASS_TOKEN yourself
chmod +x mining_power_cost.py
```

`.env`:

```sh
HASS_URL=https://your-home-assistant.example.com
HASS_TOKEN=<your long-lived access token>
```

Credentials are read from the environment first, then from `.env` next to the
script. `.env`, `config.json` and all data files are git-ignored.

### First run

```sh
./mining_power_cost.py log      # backfill from config.start_date to yesterday
./mining_power_cost.py report   # reports/mining_power_cost_2026.pdf
```

`log` always fetches the whole window and rewrites the ledger idempotently, so it
doubles as a repair command.

### Daily schedule (systemd user timer)

`systemd/` contains a template pair. Adjust `WorkingDirectory` and the script
path if you did not clone into `~/ha-mining-power-cost`:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/mining-power-cost.service systemd/mining-power-cost.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mining-power-cost.timer
systemctl --user list-timers mining-power-cost.timer     # verify
journalctl --user -u mining-power-cost.service -n 20     # last run
```

The timer runs at **00:20** with `Persistent=true`: if the machine was off, the
job runs at the next boot and backfills everything that is missing. cron works
just as well:

```cron
20 0 * * * cd $HOME/ha-mining-power-cost && ./mining_power_cost.py log && ./mining_power_cost.py report
```

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `language` | `en` or `no` — language of the generated report |
| `currency` | label only, kept for clarity |
| `start_date` | first day to log (`YYYY-MM-DD`) |
| `house_entity` | optional whole-house cumulative kWh sensor, used as a cross-check |
| `prices` | list of `{from, price_per_kwh, basis}` — the last entry that started on/before a day is used, so a price change only affects the days after it |
| `miners[]` | one entry per miner |
| `miners[].id` | short stable id, used as `meter_id` in the ledger |
| `miners[].name` | display name in the report |
| `miners[].sources[]` | measurement sources over time, oldest first |
| `sources[].from` | first day this source is authoritative |
| `sources[].type` | `power` (W) or `energy` (kWh) |
| `sources[].entity` | Home Assistant entity id |
| `sources[].note` | free text shown in the report's *Measuring point* line |

### Adding a miner

Add a block to `miners`, then:

```sh
./mining_power_cost.py log --from 2026-11-01   # its first day in service
./mining_power_cost.py report
```

The new miner gets its own column; the "Mining sum" column becomes the total.

### Changing the measuring point

Never edit old rows. Append a source with a later `from` date:

```json
"sources": [
  { "from": "2025-09-01", "type": "power",  "entity": "sensor.s9_power",
    "note": "miner telemetry" },
  { "from": "2026-10-28", "type": "power",  "entity": "sensor.miner_plug_power",
    "note": "plug-in meter at the wall socket" }
]
```

From that date the new source is used, the old history is untouched, and the
`source` column in the ledger records which sensor produced each day.

## Commands

```sh
./mining_power_cost.py log                    # all missing complete days
./mining_power_cost.py log --from 2026-01-01 --to 2026-06-30
./mining_power_cost.py report                 # current year
./mining_power_cost.py report --year 2025
./mining_power_cost.py show --limit 31        # last 31 rows in the terminal
```

Outputs in `reports/`:

- `mining_power_cost_<year>.pdf` — monthly table, method description, field
  definitions, data coverage. This is the document to keep.
- `mining_power_cost_<year>.html` — same content, regenerated each run.
- `mining_power_cost_<year>_months.csv` — monthly totals for a spreadsheet.

## Which sensor to use

Prefer an **energy meter in the wall socket** (a metering plug, a DIN-rail
meter, a Shelly with energy measurement). It measures exactly what the utility
bills you for, and it is the strongest documentation.

Miner telemetry (the `Power` value reported by Braiins OS, Whatsminer, etc.) is
usually *computed*, not measured, and can sit several percent above the wall
measurement. That direction is fine for a cost estimate but weak as evidence —
if you use it, say so in the report (the `note` field) and switch to a socket
meter as soon as you can. The tool never mixes the two silently: a source change
is dated, and the overlap is written to `comparison.csv`.

## Tax documentation

What a tax authority typically wants to see, and what this tool gives you:

1. **Consumption per day, per miner** — `ledger.csv`, 3 decimals, plus
   `hours_covered` so partial days are visible.
2. **A price per kWh with a basis** — `price_per_kwh` on every row, from the
   `prices` list. Keep the contract or invoice that shows the price is real.
3. **A method anyone can follow** — the report's *Basis and method* box, plus
   the field definitions table.
4. **Traceability** — every row can be regenerated from Home Assistant's
   long-term statistics for the same period; nothing is hand-edited. The report
   states the data coverage and never estimates a missing day.
5. **A whole-house cross-check** (optional) — if `house_entity` is set, the
   report shows the miners' share of total household consumption, which must
   stay below 100 %. Months where the house meter data is incomplete are shown
   as `–` rather than a misleading number.

This tool produces the *cost* documentation. Whether the cost is deductible, and
how mining income is taxed, depends on your jurisdiction and on whether the
activity counts as a hobby or a business — check that separately (in Norway, for
example, household support schemes such as a fixed-price contract generally do
not apply to business consumption).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing HASS_URL / HASS_TOKEN` | no `.env` and no exported variables |
| `Home Assistant login failed` | token expired or wrong URL scheme (`http` vs `https`) |
| A month shows `0.0` | the sensor had no data — check `status` and the HA history for the entity |
| Ledger rows are missing for old dates | the entity id changed in HA (integration re-added); add the old id as an earlier source |
| Report PDF looks broken with wrapped columns | no Chromium found, so LibreOffice rendered it — install `chromium` |
| `no_data` for every day | wrong entity id, or the sensor has no `state_class` and therefore no long-term statistics |
| A day is `partial` | Home Assistant or the sensor was down for some hours; the kWh for the hours that *were* measured is still correct |

## Uninstallation

```sh
systemctl --user disable --now mining-power-cost.timer
rm ~/.config/systemd/user/mining-power-cost.{service,timer}
systemctl --user daemon-reload
rm -rf ~/ha-mining-power-cost
```

Nothing was installed outside that directory (no packages, no services, no
database). Keep a copy of `ledger.csv` and the PDFs first if you want to retain
the documentation — they are plain files and readable without this tool.

## License

MIT — see [LICENSE](LICENSE).
