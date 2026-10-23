# Back-casts

Checked against: not yet verified on `main`; the docs commit that follows the merge sets this line.

A **back-cast** is a `CH_only` optimisation of a past period, driven by inputs observed or reconstructed
for that period; its results are compared with observed outcomes. Terms: `CONTEXT.md`. Design and
decisions: `CH_ONLY_HISTORICAL_MODEL_PLAN.md`. Code: `data_prep/historical_import.py`. The inputs come
from the processed folder of `data_prep/historical` (see its README).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `historical_period` | `False` | Turns a sub-scenario into a back-cast: a quoted `"YYYY-MM-DD/YYYY-MM-DD"` of local (Europe/Zurich) dates, both included |
| `historical_data_dir` | `False` | The processed folder of `data_prep/historical`, e.g. `"input/historical/processed/2023-10-01_2026-09-30"` |
| `historical_demand` | `"demand_MW"` | Demand column of `ch_demand_option_a_hourly.csv` (section Demand) |
| `historical_generation_source` | `"entsoe_bfe_months"` | Table of the observed Swiss solar, wind, run-of-river, nuclear and "others" output: `"entsoe_bfe_months"`, `"entsoe"` or `"energy_charts"` (section Renewable infeed) |
| `historical_hydro_end` | `"equal"` | National reservoir stock at the period end: `"equal"`, `"at_least"` or `"free"` (section Reservoir stocks) |

Quote the period in the scenario CSV. The reader evaluates cells with `eval`, so an unquoted
`2025-11-15/2026-12-20` becomes a number and an empty cell becomes `"nan"`; both stop the run.

A back-cast sub-scenario also needs these settings; the run stops and names every conflict:
- `CH_only = True`, `allow_investment = False`, and CH00 as the only Swiss node of `Node_list_setting`;
- `NodeDH_list = []`;
- `False` or `0` for `hydro_inflow_TWh`, `ror_annual_TWh`, `pump_capacity_GW`,
  `adding_hydro_storage_cap_TWh`, `reduce_inflex_demand_by_[MWh]`, `slack_soc` and
  `minimum_RES_target_CH`, and mode `False` in `winter_limit`;
- exactly one sub-scenario in its meta-scenario.

`run_year` and `weather_year` play no role in a back-cast.

## Model checks

After capacity fixing and the variable presets, `core_main` calls `apply_historical_constraints`
(`model/components_historical.py`). It runs after the presets, so no preset can undo a back-cast
constraint; constraints a back-cast adds go through `add_backcast_constraint` and are exported as duals
by default. It stops the run, naming every reason, unless:
- every plant is a CH00 plant of the back-cast fleet (the hydro fleet and the fixed plants), and no
  district-heating plant is built;
- every investment variable is fixed;
- no fuel or energy-budget constraint and no electrolyzer window is active;
- no demand-side response plant is built, `slack_soc` is off, and neither the Swiss RES target nor the
  winter import limit is active;
- no part built on the static calendar is active (DSR days, EV weeks, the building heat state,
  district-heating weeks);
- no variable preset applies to the back-cast meta-scenario.

It then adds the back-cast constraints: `fixed_generation` (section Fixed plants), and
`reservoir_first_hour` and `reservoir_end_target` (section Reservoir stocks). Last, it audits the
objective and prints each group of cost terms with its unit: hydro O&M, lost load and trade in EUR,
investment in CHF. An investment term with a non-zero coefficient or constant stops the run; terms whose
variables are all fixed appear as a constant offset.

## Time steps

The period becomes a UTC interval with an exclusive end: 1 Oct 2025 to 30 Aug 2026 is
[2025-09-30 22:00, 2026-08-30 22:00) UTC, 8,016 hours. The k-th UTC hour becomes model step
`t_((offset + k - 1) mod 8760) + 1`, where `offset` is the hour of year of the first local hour in a
365-day calendar, so 1 October is `t_6553` in every year. `t_start` and `t_end` follow from the period
(6553 and 5808 for the first run); if the scenario CSV sets them, they must agree.

The labels are positions, not calendar hours: the static day, week and month maps no longer match
local days. Every date comes from the time map (`historical_time_map`: model step, UTC hour, local
hour), never from a label. A period that contains 29 February or has more than 8,760 hours stops the
run.

## Historical import

In a back-cast, `data_import_TYNDP_fcn` hands over to `import_historical_inputs` before any normal
reader runs, so no future-year file is read. The import fills the same global lists and dicts the model
reads:
- **Validate first, then write.** Every input of the sub-scenario is read and checked before any global
  changes, so a failing input changes nothing.
- **Clean state.** The globals must be empty when it starts; data left by another meta-scenario in the
  same process stops the run. `run_scenarios.py` runs each meta-scenario in its own process.
- **Structure.** Node CH00 with its fixed consumer `CH00_fixedconsumer`, and the border lines
  `HVAC_{AT,DE,FR,IT}00_CH00` with the neighbour as start node.
- **Representative hydro fleet** (`input/hydro_PECD`): the open plants `large_psp`, `medium_reservior`
  and `small_reservior`, the closed loop `CH00_psp_close`, and `CH00_dam` with zero capacity. This is the
  repository's 2030 fleet (about 8.8 TWh of storage), not observed 2025/26 capacities. Storage
  efficiencies, O&M and emission factors come from the cost workbook, used only where its 2035 and 2050
  values agree; the open plants are on an electrical basis (section Reservoir inflow). Availability comes
  from `input/availability_factor_conventional.csv` and must not vary by month (0.966).
- **No investment and no fuel model.** Every investment slope is 0, the operating-cost calibration is 1,
  and the set of limited fuels is empty.
- **Lost load** as in the normal import.
- **Border trade**: observed prices and transfer limits on the four border lines (next section).
- **Swiss demand**: option A as the demand of `CH00_fixedconsumer` (section Demand).
- **Renewable infeed**: observed solar, wind and run-of-river generation (section Renewable infeed).
- **Fixed plants**: nuclear and "others", whose output equals the observed series (section Fixed plants).
- **Reservoir inflow**: PECD natural inflow of the open plants (section Reservoir inflow).
- **Reservoir stocks**: the observed start stock and end target of the open plants (section Reservoir
  stocks).

Nothing outside the back-cast fleet is built: EV, V2G, electrolyzer, building heat pumps, district
heating, fuel limits, KVA infeed, energy limits and investment candidates stay empty.

## Borders

The back-cast trades with Austria, Germany, France and Italy at observed prices within observed limits,
read from the processed folder for every hour of the period:
- **Prices** from `prices_day_ahead_hourly.csv`: day-ahead, EUR/MWh, hourly mean of the quarter-hours,
  each neighbour's price on its border line. Negative prices are kept. The pipeline fills this table from
  ENTSO-E (`PRICE_SOURCE` in `data_prep/historical/config.py`); it equals Energy-Charts (SMARD) in every hour
  except 26 June 2024 in Austria (`data_prep/historical/KNOWN_DATA_ISSUES.md`, KDI-2).
- **Transfer limits** from `ntc_dayahead_combined_hourly.csv`: day-ahead NTC, Swissgrid D-2 where
  available and ENTSO-E otherwise, hourly minimum of the quarter-hours; intraday NTC is not used. On line
  `HVAC_X00_CH00`, positive flow runs into CH00: the upper bound (`ATC_exportlimit`) is `X_to_CH` and the
  lower bound is minus `CH_to_X` (`ATC_importlimit`). `NTC_CH_ratio` multiplies both directions once. A
  zero limit is valid.
- The manifest records both files and counts the direction-hours per NTC source.

A missing file, column or hour, a duplicate column or hour, a missing or non-finite value, a negative
limit, or an `NTC_CH_ratio` that is not a finite number of at least 0 stops the run.

## Demand

The fixed consumer's demand at every hour of the period is Swiss demand option A from
`ch_demand_option_a_hourly.csv`: Swissgrid end-user consumption plus grid losses plus on-site
consumption, without storage pumping. Its monthly totals equal the BFE national consumption
(Landesverbrauch); the manifest counts the hours in months BFE has published only provisionally.

The hourly placement of on-site consumption is an assumption, so state the column with every result.
`historical_demand` chooses it:
- `demand_MW` (default): half like PV output, half flat (`onsite_solar_share_assumed`);
- `demand_solar_shaped_MW`: all like PV output;
- `demand_flat_MW`: all flat.

The two extremes are for sensitivity runs; at noon in July 2026 they are about 1.6 GW apart. EV and
heat-pump inflexible demand stay empty, since observed demand already contains them. Another column, a
missing or non-finite value in the chosen column, a negative value, or an `onsite_solar_share_assumed` that
varies within the period stops the run.

## Renewable infeed

Observed Swiss generation is the infeed of `CH00_fixedconsumer` at every hour of the period. The
setting `historical_generation_source` picks the table:

| Setting | Table | Content |
|---|---|---|
| `"entsoe_bfe_months"` (default) | `ch_generation_entsoe_bfe_hourly.csv` | ENTSO-E hourly profiles, each local month scaled to BFE's production of the type |
| `"entsoe"` | `ch_generation_entsoe_hourly.csv` | ENTSO-E A75 as published; it covers only part of Swiss generation (solar 67 %, run of river 52 % of BFE over Oct 2023 - Aug 2026) |
| `"energy_charts"` | `ch_public_power_hourly.csv` | Energy-Charts public power; monthly totals within 1 % of BFE |

The ENTSO-E tables take `others` from BFE, since ENTSO-E publishes none for Switzerland
(`data_prep/historical/generation.py`). The default holds values only in the local months BFE has
published and the table covers completely, so a period reaching into another month stops the run
with missing values. It is the default because it lowered the censored price MAE of all 16 back-cast
experiments against Energy-Charts, by 0.14 to 0.67 EUR/MWh (`experiments/backcast/LOG.md`). In all
three the columns are:

| Model technology | Column |
|---|---|
| `pvrf` | `solar` |
| `windon` | `wind_onshore` |
| `ror` | `hydro_run_of_river` |

Every other infeed technology (`windof`, `pv`) is 0 at every hour, so no future infeed remains.
`infeed.csv` reports the infeed by technology, and curtailment stays possible. There are no infeed
plants, so solar, wind and run-of-river capacities are not modelled. The manifest records the energy of
each technology over the period. A missing column, a missing or non-finite value, or negative generation
stops the run.

## Fixed plants

A **fixed plant** produces exactly its observed output in every hour:

| Plant | Technology | Observed output (table of `historical_generation_source`) | Capacity C(t) |
|---|---|---|---|
| `CH03_nuclear` | `nuclear` | `nuclear` | `net_capacity_MW` of `nuclear_availability_hourly.csv` (2,973 MW) |
| `CH01_other` | `other` | `others` | `others` of `ch_installed_power_yearly.csv`, the sum of the Energy-Charts "others" categories (`INSTALLED_POWER_OTHERS` in `data_prep/historical/config.py`) |

- **Capacity year of "others":** each hour takes the latest published year not after its local
  year. Energy-Charts publishes up to 2025, so 2026 hours use the 2025 value (1,200 MW). The
  manifest records the year used for each local year.
- **One capacity per plant:** the model holds one `gen_max` per plant for the whole period, so it
  is P_base, the maximum of C(t) over the period, and the availability is observed / P_base. C(t)
  only validates: an observed value above the C(t) of its hour, a negative observed value, or a
  P_base that is not positive stops the run.
- **Model side:** the hook adds `fixed_generation`, `gen = gen_max × avail_plant` for both plants,
  so their output equals the observed series; its dual is exported by default. No energy or fuel
  limit is built for them, so the annual cap on `other` of normal runs does not apply; the hook
  stops the run if one is.
- **Operating cost 0:** their output is fixed, so a cost would only add a constant to the objective.
- **"Others" is not an hourly measurement:** in every table it is the BFE monthly thermal total spread flat
  over each month. Plots must not suggest an hourly profile.

`gen.csv` shows both plants by name.

## Reservoir inflow

The open plants receive the natural inflow of CH00 from `pecd_inflow_weekly_ch00.csv` (Copernicus PECD
4.2, written by `process` when a PECD archive is registered; `data_prep/historical/README.md`):
- **Series:** HRI (reservoir) plus HOL (open-loop pumped storage), kept apart in the manifest with their
  energy and the weeks of each release (`COM` annual, `ITE` interim).
- **Weeks:** each week is 168 UTC hours from Monday 00:00 UTC (inferred from the files; PECD does not
  state the time zone). Its energy is spread flat over those hours, clipped to the period and mapped to
  model steps. A partial first or last week contributes only its overlap, 122/168 and 166/168 in the first
  run; nothing is renormalised and no missing week is interpolated.
- **Split:** `inflow_share` of the representative fleet table, one value per plant (the table repeats it
  on each of the plant's rows): 0.716, 0.245 and 0.039. The closed loop and the dam get 0.

A week that is not 168 hours, overlapping weeks, an hour of the period no week covers, a missing,
non-finite or negative energy of a week in the period, a plant whose rows disagree on its share, or shares
that do not sum to 1 stops the run.

**Electrical basis.** PECD inflow is electrical energy at grid level (C3S user guide), and so are the BFE
stocks. The balance divides generation by the discharge efficiency, so with the workbook's 0.87 every
100 MWh of inflow would allow only 87 MWh of generation. The open plants therefore get discharge
efficiency 1 and charging efficiency 0.87 × 0.87 = 0.7569, both derived from the workbook values; the
round trip is unchanged. The closed loop and the dam keep the workbook values. The fleet table's storage
is taken as electrical energy, as the ENTSO-E methodology strongly implies without stating it; the
manifest declares this.

## Reservoir stocks

A back-cast replaces the cyclic storage condition of the open plants with observed boundary values.

**Readings.** `reservoir_weekly_bfe.csv` holds the BFE weekly national stock `TotalCH_GWh`, dated Monday
for Sunday 24:00 local, so each reading is the instant Monday 00:00 local in UTC. The fill fraction of a
reading is `TotalCH_GWh / TotalCH_max_GWh` of the same reading.
- The **start stock** is taken at the period start instant, before the first hour's flows; the **end
  target** at the period end instant, after the last hour.
- An exact reading is used directly; otherwise the two surrounding readings, at most 8 days apart, are
  interpolated linearly. Nothing is extrapolated.
- The fill fraction times the model storage of the open plants (8.8 TWh) gives the stocks. The start
  stock is split to plants by equal fill fraction, an assumption, since BFE observes only the national
  stock.
- In the first run the start lies 48 hours after the reading of 29 September (fill fraction 0.8528, 7.51
  TWh); the end is the reading of 31 August (0.7182, 6.32 TWh).

Duplicate dates, a `TotalCH_max_GWh` that is not positive, readings more than 8 days apart around an
instant, or an instant without a reading on one side stops the run.

**Model side.** For the open plants, the hook deactivates the cyclic link of the first model step
(`storage_soc` at the first step) and the 0.95 start condition. It adds:
- `reservoir_first_hour`: the production storage balance of the first hour (the same terms, from
  `storage_balance_terms` in `model/components_common.py`) with the plant's start stock as the previous
  stock;
- `reservoir_end_target`: the sum of the open plants' stocks after the last hour equals the end target
  (`historical_hydro_end = "equal"`), or is at least it (`"at_least"`); `"free"` adds no target and needs
  no reading after the end.

The later hourly balances, optimised spill and the closed loop (cyclic, with its start condition) are
unchanged. Both new constraints are unscaled, so their exported duals are in EUR/MWh:
`reservoir_end_target_dual.csv` is the cost of one more MWh required in storage at the period end, the
value of stored energy; `reservoir_first_hour_dual.csv` is minus the value of one more MWh of start
stock. The dual export leaves out the deactivated rows (`docs/duals.md`).

## Run records

After the import, `core_main` writes two files into `output/<meta-scenario>/`:
- `historical_time_map.csv`: model step, UTC hour and local hour of every hour of the period;
- `historical_inputs_manifest.json`: the period and model steps, every file read with its checksum and
  columns, the assumptions, and the git commit with a flag for uncommitted changes.

## Price comparison

`tools/compare_historical_prices.py` compares a back-cast's modelled Swiss price with the observed
day-ahead price, in EUR/MWh:

```bash
poetry run python tools/compare_historical_prices.py output/hist_2526_octaug
```

- **Pairing:** the CH00 rows of `energy_balance_dual.csv`, divided by the sub-scenario weight (1 in a
  back-cast), are dated through the run's `historical_time_map.csv`. Observed prices, transfer limits and
  scheduled exchange come from the processed folder the run's manifest names. Every hour of the time map
  needs exactly one dual, one flow per border line and one observed value; a gap, a repeat, a model step
  outside the time map or a non-finite value stops the tool. The report states the expected and the
  paired hours.
- **Tables** in `<run>/price_comparison/`:
  - `metrics.csv`: MAE, RMSE, bias (model minus observed) and correlation, for the model and for a
    benchmark, the mean neighbour price. The correlation of a constant series is undefined (empty).
  - `monthly.csv`: mean prices per local month. A month the period cuts (August 2026 ends on the 30th)
    is listed but not compared.
  - `duration_curve.csv`: both prices sorted from highest to lowest.
  - `border_limits.csv`: the hours each border flow is within 1 MW of its limit, modelled (flows against
    NTC times `NTC_CH_ratio`) and observed (scheduled exchange against NTC), over the run's period. The
    plan's 69% (FR to CH) and 34% (CH to IT) describe October 2025 to September 2026.

## Scenario and data tests

`scenarios/scen_to_run_CH_only_historical.csv` defines the first back-cast: meta-scenario
`hist_2526_octaug` with one sub-scenario, `obs`, of weight 1. It sets `CH_only = True`,
`allow_investment = False`, `NodeDH_list = []`, `slack_soc = False`, the period 1 October 2025 to
30 August 2026, the processed folder `input/historical/processed/2023-10-01_2026-09-30`,
`historical_demand = "demand_MW"` and `historical_hydro_end = "equal"`. To run it, point `target_csv` in
`scenarios/scenarios.py` at this file and restore the line afterwards.

Two tests in `tests/test_backcast_scenario.py` need that processed folder (marker `needs_data`, run with
`poetry run pytest -m needs_data`). They first name every table missing from it.
- **Import completeness:** the scenario builds from the historical import alone, and its plants, lines and
  consumers are the back-cast fleet, the four border lines and `CH00_fixedconsumer`.
- **Invariance:** builds with `run_year` 2035 and `weather_year` 2008 and 2009 have the same canonical
  model data as the scenario as it is (2050, 1995): the coefficients, bounds and right-hand sides by
  constraint and variable name, and the objective coefficients. LP text and order are not compared. Any
  difference fails the test and is listed.

Each build runs `tests/backcast_build.py` in a fresh process through `core_main(..., solve=False)`, with a
spy that fails the build if a solver is created; the four builds take a few minutes.

## Status

The import builds every input. `NOT_YET_BUILT` in `data_prep/historical_import.py` is empty; an
input added to it stops a back-cast with an error that names it, until the step that builds the input
removes it. A one-week back-cast builds, solves and writes all its outputs.

The processed folder needs `ch_installed_power_yearly.csv` and `pecd_inflow_weekly_ch00.csv`, which
`process` writes since the commits "Pipeline: installed power by category and year" and "Pipeline: weekly
PECD inflow for CH00" (the latter with a registered PECD archive); rerun `process` on a folder made before
them.
