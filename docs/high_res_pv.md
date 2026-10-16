# Per-plant rooftop PV from Nexus-E

Checked against: `main` @ a90496c (2026-10-16).

The setting `high_resolution_PV` (default `False` in `scenarios/settings_default.py`) replaces the
seven aggregated Swiss rooftop-PV investment candidates with one candidate per rooftop-PV unit of
the Nexus-E model. This guide covers what the flag changes, the input files it needs and their
format, how to run and check it, and the problems known in the current data.

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

Both call `load_high_res_PV(weather_year, run_year)` in `model/high_res_PV.py`, the only code
that reads the Nexus-E files; it is cached, so the two call sites parse the files once per run.
`data_prep/data_import_TYNDP.py` reads the flag from the scenario settings and passes it to both
functions. Wind, alpine PV and all non-Swiss candidates are unchanged.

The `_pvrf` suffix keeps aggregation and plots working: they select rooftop PV by the substring
`pvrf` in the plant name or by the tech `pvrf`, never by the full plant name.

The loader prints up to four lines per run, which are the quickest check that the right data
went in:

1. `[high_res_PV] loaded N PV-roof units, sum Pmax = X GW (weather_year=…, profile_year=…)` (always)
2. `[high_res_PV] requested run_year=A not present in profile CSV; using closest available year=B …` (only when `run_year` is not in the profile file)
3. `[high_res_PV] dropped K unit(s) without profile, M profile(s) without unit` (only when the join drops rows)
4. `[high_res_PV] removed N aggregated CHxx_pvrf candidates, added M per-plant candidates` (always)

## Input files

The loader reads `input/RES_nexuse/PV/<weather_year>/`, where `<weather_year>` is the scenario's
`weather_year` setting. A missing folder is a hard error that lists the folders present. The
folder is gitignored (about 1.2 GB per weather year); copy or link it into place. It holds two
files with fixed names.

**`NewUnits.xlsx`**, first sheet, one row per unit. Columns used:

| Column | Content |
|---|---|
| `GenName` | Unit name: unique, characters `A-Z a-z 0-9 _ -`, at most 49 characters. The join key to the profile file. The current data uses `CH_RES_PV_CH_<grid node>_<kV>_<tilt>_<azimuth>_<size class>`. |
| `Pmax` | Installable capacity of the unit in MW. It becomes the candidate's investment cap and the divisor that turns the profile into an availability factor, so it must be the capacity the profile was computed for. Rows with `Pmax <= 0` are dropped. |
| `Technology` | Only rows equal to `PV-roof` are used; other technologies in the file are ignored. |

**`NexusInput_profiles_newPV_mv.csv`**, one row per unit and profile year. Columns used:

| Column | Content |
|---|---|
| `Name` | Equal to the unit's `GenName`. Rows without a unit are dropped and counted in log line 3. |
| `year` | Profile year as an integer. The rows whose `year` equals the scenario's `run_year` are used; if that year is absent, the closest year present is used (ties go to the smaller year) and log line 2 says so. |
| `1` … `8760` | Hourly generation in MWh per hour at the unit's `Pmax`; column `1` is 1 January 00:00 of the weather year. Exactly the columns `1` to `8760` are read and any further column is ignored, which matches how FEM uses its other profiles: the first 8760 hours of the year, so a leap year loses 31 December. Values above `Pmax` are clipped to an availability of 1 and values below 0 to 0. |

Other columns in either file (`Pmin`, `NewInvestment`, `Profile Number`, `Country`, `type`,
`resolution`, `unit`) are ignored, so they may stay or go.

### The current data (received March 2026)

One folder, `2009`, with profile year 2050 only: 4,535 PV-roof units totalling 44.0 GW, spread
over 131 grid nodes, tilts 0/15/30/60 degrees, azimuths 0/90/135/180/225/270 degrees and four
size classes (1-10, 10-30, 30-100, 100-1000 kW). Units at the same node and orientation have
nearly the same profile shape; they differ in `Pmax`. Known problems:

- **Placeholder capacities.** 331 units have `Pmax` exactly 1.0 MW, and for 264 of them the
  profile is far larger (up to 133 MWh per hour). After clipping, these units show about 3,570
  full-load hours against 920 to 1,050 for the others, so the optimiser sees them as cheap
  near-firm capacity. They total 361 MW. Get the real `Pmax` of these units, or drop them.
- **Orphan profiles.** 4,021 profile rows have no unit in `NewUnits.xlsx`. They are dropped but
  double the file to 770 MB.
- **One weather year, one profile year.** `run_year` 2035 falls back to the 2050 profiles (log
  line 2); `weather_year` 1995 or 2008 stops the run.

### Requesting new data

Ask the Nexus-E team for, per FEM weather year (1995, 2008, 2009):

1. The two files above, computed from that meteorological year, with the folder named after it.
   Have them state which meteorological year and time zone the hourly columns follow.
2. `Pmax` equal to the capacity each profile was computed with, for every unit.
3. Profile years 2035 and 2050 in the `year` column if the unit set or capacities differ
   between them; otherwise one year is enough.
4. Only the PV-roof rows in the profile file, zipped for transfer. Keep the file names.

## Running with the flag

- **Unit tests.** `tests/test_high_res_pv_loader.py` builds small synthetic input files and
  needs neither the data nor a solver. Run `poetry run pytest`.
- **Test run.** Point `target_csv` in `scenarios/scenarios.py` at
  `scenarios/scen_to_run_test_high_res_pv.csv` (the 25-hour test scenario with `weather_year`
  2009 and the flag on), run `poetry run python run_scenarios.py`, then restore `target_csv`
  (`docs/running.md`). Results go to `output/2050_st_hrpv/`. Check log lines 1 and 4, and that
  the `CH00` row of `output/2050_st_hrpv/Map_node_plant.csv` lists `CH00_*_pvrf` plants and no
  `CH0x_pvrf`.
- **Size and run time.** Each candidate adds one `gen` variable and two constraints per modelled
  hour and sub-scenario. On the 25-hour test scenario (2026-10-16, same code, one laptop) the flag
  took the model from 28,818 variables and 32,960 constraints to 146,546 and 268,416, so a full
  hydrological year with the flag on has about 40 million more variables and 80 million more
  constraints than without it (about 10 and 11.5 million). The flag took that 25-hour run from
  16 seconds to 132 seconds and its peak memory to 6.8 GB. About 40 seconds of that is
  `load_high_res_PV` itself (12 seconds parsing the 770 MB CSV, 28 seconds building the dictionary
  of 4,535 x 8,760 availability values, which stays in memory for the whole run); the rest is the
  ordinary data import and model build handling 4,535 more plants. No full-year run with the flag
  on has been done yet; plan it for a large-memory Euler node (`docs/running.md`,
  `docs/multi_weather_year.md`).
- **Run set.** `scenarios/scen_to_run_high_res_pv.csv` is a 2 x 2 design for 2050 and weather
  year 2009: aggregated against per-plant PV, each without and with the Swiss RES target of
  45 TWh (`minimum_RES_target_CH` in MWh of annual PV and wind infeed in Switzerland). The other settings are those of the 2050 base column of
  `scenarios/scen_to_run_STORSUPPORT.csv`.
