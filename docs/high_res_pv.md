# Per-plant rooftop PV from Nexus-E

Checked against: `main` @ a90496c (2026-10-16).

The setting `high_resolution_PV` (default `False` in `scenarios/settings_default.py`) replaces the
seven aggregated Swiss rooftop-PV investment candidates with one candidate per rooftop-PV unit of
the Nexus-E model; `high_resolution_PV_dataset` names which Nexus-E dataset to read. This guide
covers what the flag changes, the dataset folders and their format, how to export a dataset from
a Nexus-E database, how to run and check it, and the datasets at hand.

## What the flag changes

With the flag off, the Swiss rooftop PV the model can build is the rows `CH01_pvrf` to
`CH07_pvrf` of `input/plants_invest_candidates_res_CH.csv`: one candidate per Grossregion, each
capped at 10 GW, with the EMHIRES profile of its region (`input/RES_EMHIRES/pvrf_<weather_year>.csv`).

With the flag on, `model/data_import_fcns.py` changes two things at its existing PV read sites:

- `read_plant_non_hydro_data` drops the candidate rows whose index matches `^CH0[1-7]_pvrf$` and
  appends one candidate per Nexus-E unit, named `CH00_<GenName>_pvrf` with `-` replaced by `_`,
  at node and market `CH00`, tech `pvrf`, `gen_max_limit` equal to the unit's `Pmax` (MW) and
  `energy_max_limit` infinite.
- `read_RES_avail_data` adds each unit's hourly availability factor (its profile divided by
  `Pmax`, clipped to [0, 1]) to the availability dictionary. The EMHIRES `pvrf` profiles are
  still loaded: the pre-existing PV capacity at `CH01`..`CH07` keeps its aggregated plants.

Both call `load_high_res_PV(dataset, weather_year, run_year)` in `model/high_res_PV.py`, the
only code that reads the Nexus-E files; it is cached, so the two call sites parse the files once
per run. `data_prep/data_import_TYNDP.py` reads the flag and the dataset name from the scenario
settings and passes them to both functions. Wind, alpine PV and all non-Swiss candidates are
unchanged.

The `_pvrf` suffix keeps aggregation and plots working: they select rooftop PV by the substring
`pvrf` in the plant name or by the tech `pvrf`, never by the full plant name.

The loader prints up to four lines per run, which are the quickest check that the right data
went in:

1. `[high_res_PV] loaded N PV-roof units, sum Pmax = X GW (dataset=…, weather_year=…, profile_year=…)` (always)
2. `[high_res_PV] requested run_year=A not present in profile CSV; using closest available year=B …` (only when `run_year` is not in the profile file)
3. `[high_res_PV] dropped K unit(s) without profile, M profile(s) without unit` (only when the join drops rows)
4. `[high_res_PV] removed N aggregated CHxx_pvrf candidates, added M per-plant candidates` (always)

## Dataset folders

The loader reads `input/RES_nexuse/PV/<dataset>/<weather_year>/`, where `<dataset>` is the
scenario's `high_resolution_PV_dataset` setting and `<weather_year>` its `weather_year`. The
setting has no default: the flag on without a dataset name is an error that lists the datasets
present, and so is a dataset or weather-year folder that does not exist. `input/RES_nexuse/` is
gitignored (a dataset of 8,556 units is about 450 MB); copy or link it into place. Each folder
holds two files with fixed names, written by the export tool below.

**`units.csv`**, one row per unit. Columns the loader uses:

| Column | Content |
|---|---|
| `GenName` | Unit name: unique, characters `A-Z a-z 0-9 _ -`, at most 49 characters. The join key to the profile file. |
| `Pmax` | Installable capacity (roof potential) of the unit in MW. It becomes the candidate's investment cap and the divisor that turns the profile into an availability factor, so it must be the capacity the profile was computed for. Rows with `Pmax <= 0` are dropped. |
| `Technology` | Only rows equal to `PV-roof` are used. |

The export adds `BusName` (the Nexus-E grid node) and `idProfile` for traceability; the loader
ignores them.

**`NexusInput_profiles_newPV_mv.csv`**, the Nexus-E profile table, one row per unit and profile
year. Columns the loader uses:

| Column | Content |
|---|---|
| `Name` | Equal to the unit's `GenName`. Rows without a unit are dropped and counted in log line 3. |
| `year` | Profile year as an integer. The rows whose `year` equals the scenario's `run_year` are used; if that year is absent, the closest year present is used (ties go to the smaller year) and log line 2 says so. |
| `1` … `8760` | Hourly generation in MWh per hour at the unit's `Pmax`; column `1` is 1 January 00:00 of the weather year. Exactly the columns `1` to `8760` are read and any further column is ignored, which matches how FEM uses its other profiles: the first 8760 hours of the year, so a leap year loses 31 December. Values above `Pmax` are clipped to an availability of 1 and values below 0 to 0. |

The other columns of the Nexus-E table (`Profile Number`, `Country`, `type`, `resolution`,
`unit`) are carried along and ignored. A `SOURCE.md` next to the two files records the database,
server, export date, weather year and the checks the export ran.

## Exporting a dataset from a Nexus-E database

Nexus-E keeps its inputs in MySQL scenario databases. `data_prep/export_nexuse_pv.py` reads one
such database and writes a dataset folder:

```bash
poetry install --with nexuse        # once: the optional MySQL client (pymysql)
set NEXUSE_DB_PASSWORD=<password>   # export NEXUSE_DB_PASSWORD=... on Linux
poetry run python data_prep/export_nexuse_pv.py --db <database> --dataset <folder name> --weather-year <year> --run-year 2050 --host <server> --user <user>
```

It takes the generator configuration of `--run-year` (table `genconfiginfo`), joins
`genconfiguration` (with `Pmax`, bus and profile id), `gendata` (technology) and `profiledata`
(the hourly series, a JSON list of 8,760 MWh values in column `timeSeries`) for every candidate
unit of technology `PV-roof`, and streams the profiles into the CSV one row at a time. Before a
row is kept it checks that the profile has 8,760 finite values, none negative and none more
than 1 kW above `Pmax` (Nexus-E stores profiles rounded to 0.1 kWh, and five units of the 2009
set, all below 0.04 MW, peak up to 0.5 kW above their `Pmax`; FEM clips those to availability 1,
and `SOURCE.md` counts them); afterwards it checks that unit names are unique and every `Pmax`
is positive. Any failure
stops the export with the unit's name, so a dataset folder is either complete and consistent or
absent.

The database does not record which meteorological year its profiles follow; `--weather-year`
names the folder and is written to `SOURCE.md`, so make sure of it before exporting. The password
is read from the environment only, never from the command line or a file in the repository.

`units.csv` is not CentIv's `NewUnits.xlsx`. That workbook, written at the end of a Nexus-E run,
lists the capacities Nexus-E chose to build, filtered to the units it built; an earlier FEM
version used it as the capacity cap and so clipped the profiles of 290 units to full availability.

## Datasets at hand (October 2026)

Both databases live on the Nexus-E input server and hold the same five run years; only 2050 has
rooftop-PV candidates.

| | `pv09_ewopt` | `pv_old` |
|---|---|---|
| Database | `pv_all_eu_evflex_pv09_ewopt` | `pv_all_eu_evflex_pv_old` |
| Candidate units | 8,556 | 125 |
| Unit scheme | grid node × tilt (0/15/30/60) × azimuth (0/90/135/180/225/270) × size class (1-10, 10-30, 30-100, 100-1000 kW) | one per grid node |
| Grid nodes | 135 | 125 (121 in common) |
| Total potential | 93,303 MW | 46,994 MW |
| Full-load hours at potential | 391 to 1,227, median 958 | 838 to 1,159, median 994 |
| Investment cost, EUR per MW and year | four levels by size class, 46,280 to 105,904 | one level, 64,921 |
| Weather year | 2009 (east-west optimised roofs) | 2009 |

Neither database records its meteorological year; 2009 for both is the Nexus-E team's
statement. A check against FEM's own EMHIRES Swiss PV profiles
(`input/RES_EMHIRES/pvrf_<year>.csv`) agrees for the new set (daily energy correlates at 0.94
with 2009, below 0.6 with 1995, 2007 and 2008) but not for the old one (0.95 with 1995, 0.58 with
2009). On the nodes both have, the new potential is on median 1.9 times the old one with a range
of 0.08 to 9.4, so the two rest on different potential assumptions. Units at the same node and
orientation have nearly the same profile shape and differ in `Pmax`.

The files Nexus-E sends by hand (`NexusInput_genstab_newPV.csv` with the potential in column
`P_gen_max in 2015 (MW)` plus the profile table) carry the same content as the database export;
an aggregated 513-unit variant of the 2009 data (node × size class) exists as such a file pair
but has not been exported here.

## Running with the flag

- **Unit tests.** `tests/test_high_res_pv_loader.py` and `tests/test_export_nexuse_pv.py` build
  small synthetic data and need neither a database, the data folders nor a solver. Run
  `poetry run pytest`.
- **Test run.** Point `target_csv` in `scenarios/scenarios.py` at
  `scenarios/scen_to_run_test_high_res_pv.csv` (25 hours, weather year 2009, the flag on with
  `pv09_ewopt`; meta-scenario `2050_st_pv09`) or `scenarios/scen_to_run_test_high_res_pv_old.csv`
  (the same with `pv_old`; `2050_st_pvold`), run `poetry run python run_scenarios.py`, then
  restore `target_csv` (`docs/running.md`). Results go to `output/2050_st_pv09/` and
  `output/2050_st_pvold/`. Check log lines 1 and 4, and that the `CH00` row of
  `Map_node_plant.csv` lists `CH00_*_pvrf` plants and no `CH0x_pvrf`. The two are separate files
  because one `run_scenarios.py` process cannot run meta-scenarios whose Swiss candidates differ
  (`docs/running.md`, test run).
- **Size and run time.** Each candidate adds one `gen` variable and two constraints per modelled
  hour and sub-scenario. On the 25-hour test scenario (2026-10-16, same code, one laptop) the
  4,535-unit data took the model from 28,818 variables and 32,960 constraints to 146,546 and
  268,416, so a full hydrological year with the flag on has about 40 million more variables and
  80 million more constraints than without it (about 10 and 11.5 million). The 8,556-unit
  `pv09_ewopt` dataset gives 251,092 variables and 477,508 constraints for the same 25 hours,
  about 78 million more variables and 156 million more constraints over a full year; the
  125-unit `pv_old` dataset gives 31,886 and 39,096, a negligible addition. The flag took that
  25-hour run from 16 seconds to 132 seconds
  and its peak memory to 6.8 GB. About 40 seconds of that is `load_high_res_PV` itself (12 seconds
  parsing the 770 MB CSV, 28 seconds building the dictionary of 4,535 x 8,760 availability values,
  which stays in memory for the whole run); the rest is the ordinary data import and model build
  handling 4,535 more plants. No full-year run with the flag on has been done yet; plan it for a
  large-memory Euler node (`docs/running.md`, `docs/multi_weather_year.md`).
- **Run set.** `scenarios/scen_to_run_high_res_pv.csv` is a 2 x 2 design for 2050 and weather
  year 2009: aggregated against per-plant PV (dataset `pv09_ewopt`), each without and with the
  Swiss RES target of 45 TWh (`minimum_RES_target_CH` in MWh of annual PV and wind infeed in
  Switzerland). The other settings are those of the 2050 base column of
  `scenarios/scen_to_run_STORSUPPORT.csv`. Its aggregated and per-plant columns have different
  Swiss candidates, so run it one meta-scenario per process (the Euler array script), not with
  `run_scenarios.py` in one go.
