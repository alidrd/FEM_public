# Per-plant rooftop PV from Nexus-E

Checked against: `main` @ a90496c (2026-10-16).

The setting `high_resolution_PV` (default `False` in `scenarios/settings_default.py`) replaces the
seven aggregated Swiss rooftop-PV investment candidates with one candidate per rooftop-PV unit of
the Nexus-E model; `high_resolution_PV_dataset` names which Nexus-E dataset to read, and
`pv_roof_cost_mode` whether those candidates pay the `pvrf` technology cost or a cost by size
class. This guide covers what the flag changes, the dataset folders and their format, how to
export a dataset from a Nexus-E database, the cost classes, how to run and check it, and the
datasets at hand.

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
| `cost_class` | Optional. The class the unit is priced at when `pv_roof_cost_mode` is `by_class` (section "Investment cost by size class"). A plain string that must match a `cost_class` of `input/cost_pv_roof_cost_classes.csv`; the loader never derives it from the name. In the other modes the column is ignored. |

The export adds `BusName` (the Nexus-E grid node), `idProfile`, and the database's own
annualised costs `nexuse_InvCost_EURperMWyr` and `nexuse_FOM_EURperMWyr` (EUR per MW and
year) for traceability; the loader ignores them.

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
poetry run python data_prep/export_nexuse_pv.py --db <database> --dataset <folder name> --weather-year <year> --run-year 2050 --host <server> --user <user> --cost-class name-suffix
```

`--cost-class` is required and says how `units.csv` gets its `cost_class` column:
`name-suffix` takes the last `_`-separated token of the unit name, which must be one of
`1-10kW`, `10-30kW`, `30-100kW`, `100-1000kW` (the export stops on any other name, so a
dataset without size classes cannot be classed by accident); `all` labels every unit `all`.
`SOURCE.md` records the option and the count per class. `--units-only` rewrites `units.csv`
and appends to `SOURCE.md` of a folder exported earlier, from the database, without touching
the profile file; it refuses if the unit names differ from the profiles already there.

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

## Investment cost by size class

Nexus-E prices rooftop PV by the size of the installation; FEM prices every `pvrf` plant at one
technology cost (`input/cost_assumptions.xlsx`, 697 and 615 CHF per kW for 2035 and 2050, the
cost of a large roof). The setting `pv_roof_cost_mode` (default `tech`) gives the per-plant
candidates Nexus-E's structure:

| `pv_roof_cost_mode` | Each per-plant candidate pays |
|---|---|
| `tech` | the `pvrf` technology cost, as before |
| `by_class` | the cost of its `cost_class` from `input/cost_pv_roof_cost_classes.csv` |
| `average` | the `all` row of that table, the potential-weighted mean of the classes |

`by_class` and `average` need `high_resolution_PV` on; with it off the aggregated candidates have
no class and the run stops. In `by_class` every PV-roof unit of the dataset must carry a class
that the table has for the run year, else the run stops naming the units or classes. The seven
aggregated candidates stay out of the investment set in every mode, as with the flag on in
general (`tests/test_high_res_pv_loader.py` guards it); the pre-existing Swiss PV capacity is
untouched. With the flag off, `pvrf` keeps its workbook cost.

**The table** `input/cost_pv_roof_cost_classes.csv` (`cost_class, year, investment_cost_chfMW,
source`) holds overnight costs in CHF2017 per MW, FEM's currency basis:

| `cost_class` | 2035 | 2050 | Derivation |
|---|---|---|---|
| `1-10kW` | 1,974 | 1,452 | |
| `10-30kW` | 1,459 | 987 | Nexus-E reference costs (SWEET CROSS, CHF2020 per kW, in `nexus_e/core_modules/update_inv_costs/cost_data/investment_costs.csv`) times 0.981 (CHF2020 to CHF2017, `cost_assumptions.xlsx` sheet `02_Parameters`); 2035 is the mean of the 2030 and 2040 values |
| `30-100kW` | 1,020 | 763 | |
| `100-1000kW` | 711 | 598 | |
| `all` | 1,423 | 1,025 | mean of the four classes weighted by the installable potential per class of `pv09_ewopt` (26,268 / 36,125 / 20,053 / 10,857 MW); Nexus-E's own capacity-weighted average for 2050 is 998 CHF2020 per kW |

in CHF per kW here. `model/pv_roof_costs.py` turns an overnight cost into the yearly slope the
objective uses with FEM's one annuity rule (`utils/annuity.py`: 5% discount rate over the `pvrf`
amortization period of 25 years), the same function `get_cost_component` applies to every
technology, so a class and `pvrf` are priced on the same terms. Like every other FEM technology
the classes carry no fixed operation and maintenance cost (the workbook's `fixed_op_cost_chfMW`
is read by nothing). The `100-1000kW` class is within 3% of the `pvrf` cost, so a large roof
costs what it did; the smaller classes cost up to 2.4 times more.

In the model the slope lands in `investment_genmax_slp[p, scen]`, per plant: the class annuity
where a plant has a class, the technology slope otherwise. Two outputs show it:
`investment_summary.csv` lists each plant's `Investment Cost (CHF/MW)` (the annuity, CHF per MW
and year), and `Map_plant_cost_class.csv` lists the class of every priced plant, so results can
be grouped by class (aggregation merges it like every other `Map_*` file). The log line
`[pv_roof_costs] mode …: N candidates priced by class [...]` confirms the mode took effect.

## Datasets at hand (November 2026)

All three databases live on the Nexus-E input server and hold the same five run years; only
2050 has rooftop-PV candidates.

| | `pv09_ewopt` | `pv_cy09` | `pv_old` |
|---|---|---|---|
| Database | `pv_all_eu_evflex_pv09_ewopt` | `pv_all_eu_evflex_pv_cy09_wy2009` | `pv_all_eu_evflex_pv_old` |
| Folder | `pv09_ewopt/2009` | `pv_cy09/2009` | `pv_old/1995` |
| Candidate units | 8,556 | 125 | 125 |
| Unit scheme | grid node × tilt (0/15/30/60) × azimuth (0/90/135/180/225/270) × size class (1-10, 10-30, 30-100, 100-1000 kW) | one per grid node | one per grid node |
| Grid nodes | 135 | 125 (121 in common with `pv09_ewopt`) | 125 |
| Total potential | 93,303 MW | 46,994 MW | 46,994 MW |
| Energy at potential | 83.1 TWh | 49.5 TWh | 44.6 TWh |
| `cost_class` | from the name suffix: 2,536 / 2,443 / 2,202 / 1,375 units | `all` | none (exported before the column existed) |
| Investment cost in the database, EUR per MW and year | four levels by size class, 46,280 to 105,904 | one level, 64,921 | one level, 64,921 |
| Weather year | 2009 (east-west optimised roofs) | 2009 | 1995 |

No database records its meteorological year, so each was checked against FEM's own EMHIRES Swiss
PV profiles (`input/RES_EMHIRES/pvrf_<year>.csv`, daily energy correlation): `pv09_ewopt` 0.94
and `pv_cy09` 0.95 with 2009 and below 0.6 with 1995, 2007 and 2008; `pv_old` 0.95 with 1995 and
0.58 with 2009, and its profiles are identical to the Nexus-E default per-node databases
(`1023_target0_ab`, `base_3_36`), which are 1995 data. `pv_cy09` was built by the user from the
per-node set with 2009 profiles and is the per-node counterpart of `pv09_ewopt`; `pv_old` is
kept for reference and not used. On the nodes both have, the `pv09_ewopt` potential is on median
1.9 times the per-node one with a range of 0.08 to 9.4, so the two rest on different potential
assumptions. Units at the same node and orientation have nearly the same profile shape and
differ in `Pmax`.

The files Nexus-E sends by hand (`NexusInput_genstab_newPV.csv` with the potential in column
`P_gen_max in 2015 (MW)` plus the profile table) carry the same content as the database export;
an aggregated 513-unit variant of the 2009 data (node × size class) exists as such a file pair
but has not been exported here.

## Running with the flag

- **Unit tests.** `tests/test_high_res_pv_loader.py`, `tests/test_export_nexuse_pv.py`,
  `tests/test_pv_roof_costs.py` and `tests/test_annuity.py` build small synthetic data and need
  neither a database, the data folders nor a solver. Run `poetry run pytest`.
- **Test runs.** Three 25-hour scenarios, one file each, all on weather year 2009 with the flag
  on: `scenarios/scen_to_run_test_high_res_pv.csv` (`pv09_ewopt`, mode `tech`; meta-scenario
  `2050_st_pv09`), `scenarios/scen_to_run_test_high_res_pv_cls.csv` (`pv09_ewopt`, `by_class`;
  `2050_st_pv09cls`) and `scenarios/scen_to_run_test_high_res_pv_cy09.csv` (`pv_cy09`,
  `average`; `2050_st_pvcy09`). Point `target_csv` in `scenarios/scenarios.py` at one of them, run
  `poetry run python run_scenarios.py`, then restore `target_csv` (`docs/running.md`). Results go
  to `output/<meta-scenario>/`. Check log lines 1 and 4 and, in the class modes, the
  `[pv_roof_costs]` line; that the `CH00` row of `Map_node_plant.csv` lists `CH00_*_pvrf` plants
  and no `CH0x_pvrf`; and that `investment_summary.csv` shows four cost levels for the `by_class`
  run and one for the others.
- **Size and run time.** Each candidate adds one `gen` variable and two constraints per modelled
  hour and sub-scenario. On the 25-hour test scenario (2026-10-16, same code, one laptop) the
  4,535-unit data took the model from 28,818 variables and 32,960 constraints to 146,546 and
  268,416, so a full hydrological year with the flag on has about 40 million more variables and
  80 million more constraints than without it (about 10 and 11.5 million). The 8,556-unit
  `pv09_ewopt` dataset gives 251,092 variables and 477,508 constraints for the same 25 hours,
  about 78 million more variables and 156 million more constraints over a full year; the
  125-unit per-node datasets give 31,886 and 39,096, a negligible addition. The flag took that
  25-hour run from 16 seconds to 132 seconds
  and its peak memory to 6.8 GB. About 40 seconds of that is `load_high_res_PV` itself (12 seconds
  parsing the 770 MB CSV, 28 seconds building the dictionary of 4,535 x 8,760 availability values,
  which stays in memory for the whole run); the rest is the ordinary data import and model build
  handling 4,535 more plants. With the 8,556-unit dataset the same 25-hour test takes about
  5 minutes on a laptop (2026-11-03, modes `tech` and `by_class` alike; the 125-unit per-node
  test takes under a minute), so the cost mode costs nothing extra. It used to take 12 to
  15 minutes: the curtailment limit scanned the plant list for every consumer and hour, which
  grows with the square of the plant count, and a full-year Euler run was still building
  constraints after 8 hours (model/plant_lookups.py builds those lookups once now). What is left
  scales with the year: Pyomo's LP file writer took 91 s for the 25 hours, about 9 hours for a
  full year with 167 million constraints, and the objective about 3 hours, so give a full-year
  per-unit run a multi-day limit on a large-memory Euler node (`docs/running.md`,
  `docs/multi_weather_year.md`). Note that the 25-hour window builds no PV at all, so the three
  test runs share one objective value; the cost modes show in `investment_summary.csv` and
  `Map_plant_cost_class.csv`, not in the solution.
- **Run set.** `scenarios/scen_to_run_high_res_pv.csv` holds six full-year 2050 meta-scenarios
  on weather year 2009, each without and with the Swiss RES target of 45 TWh
  (`minimum_RES_target_CH` in MWh of annual PV and wind infeed in Switzerland): `2050_old_pv` and
  `2050_res45_old_pv` on the per-node set `pv_cy09` at the average class cost, `2050_cls_pv` and
  `2050_res45_cls_pv` on the 8,556-unit set priced by class, `2050_avg_pv` and `2050_res45_avg_pv`
  on the 8,556-unit set at the average class cost. The other settings are those of the full-year
  weather-year-2009 run `2050_wy2009` of `scen_to_run_dh_paper_weather_years.csv` (no explicit hydro
  volumes, no demand change, no cap on resistive heaters), with `pump_capacity_GW = 5.54` from the
  2050 base column of `scenarios/scen_to_run_STORSUPPORT.csv`, and `solver_parameters`, which loosens the
  barrier convergence to `BarConvTol=1e-6` and matches `threads` to the CPUs of the job (8 for the
  per-node runs, 16 for the per-unit ones); the duals of these runs are not used. The per-unit runs
  need a large-memory Euler node and a multi-day limit (see "Size and run time").
- **Only Switzerland varies.** Four more rows make the six runs differ in the Swiss PV candidates
  alone. The neighbours' candidates of `input/plants_invest_candidates_res_CH.csv` (PV, wind and gas
  in DE00, IT00, FR00, AT00, 20 GW caps for PV and wind) stay in the model (`investment_markets =
  False`) but are fixed at what they built in `2050_wy2009` (20 GW of PV and 20 GW of wind in DE, 20 GW of
  wind in IT, 0.45 GW of PV and 18.4 GW of wind in AT, 4.6 GW of gas with carbon capture in FR), so
  they are equal in every run. `variable_presets_file =
  input/variable_presets/neighbours_wy2009_no_new_ch_nuclear.csv` holds these values and
  also fixes the Swiss nuclear candidate `CH00_nuclear` at 0.01 MW, the floor `model/core.py` sets on it,
  so no new nuclear is built: without inherited Swiss PV and wind it otherwise filled the gap (6.7 to
  9.1 GW without the target). `input/variable_presets/neighbour_capacities_wy2009.csv` holds the neighbours
  alone. `input/variable_presets/neighbours_wy2009_fr_oil_no_new_ch_nuclear.csv` also
  fixes the existing French oil plant `FR00_oil` (0 MW in 2050) at 6,500 MW as a backup; with
  `RES_EU_coefficient = 1.0` France otherwise sheds up to 5.5 GW in a January cold spell. The oil plant is not
  a candidate, so its annuity (34,236 CHF/MW/yr) is not in the objective. The current run set sets
  `electrolyzer_demand_reduction_coefficient = 0.6667` and uses
  `input/variable_presets/neighbours_wy2009_fr_oil_no_new_ch_nuclear_no_fuel_allowance.csv`, which also fixes
  the annual fuel allowance `fuel_storage_capacity_annual` at 0 for the fuels without a limit in
  `input/fuel_limits.csv`: two per-unit runs stopped unconverged with tens of TWh in it, unused. Its run
  `2050_res45_le30_pv` keeps only candidates up to 30 kW investable through a presets file written by
  `data_prep/presets_pv_size_limit.py` (gen_max of the larger classes fixed at 0). An earlier set fixed the
  neighbours at the build of the first run set
  (`input/variable_presets/neighbour_capacities_design1.csv`). The second run set dropped them
  instead (`investment_markets = CH00`, which keeps only the listed markets' candidates).
  `battery_investment_nodes_in_addition_to_CH` is left empty, so no neighbour battery is built.
  `PVRF_CH_coefficient
  = 0` and `Windon_CH_coefficient = 0` remove the inherited Swiss rooftop PV and onshore wind
  (37.5 GW and 2.2 GW in 2050 under `ch_policy` ZBA, 46.85 TWh of infeed in weather year 2009), so
  the roof potential of the Nexus-E candidates is not counted twice and the 45 TWh target has to be
  built: in the first run set, with the inherited capacity in place, the target never bound. The
  seven Swiss wind candidates (600 MW each) stay. The three 25-hour test files carry the same
  rows; `model/investment_candidates.py` and `tests/test_investment_candidates.py` hold the market
  filter, `tests/test_variable_presets.py` checks that the presets file fixes every candidate outside
  Switzerland within its cap.
