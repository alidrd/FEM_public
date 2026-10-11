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
- every plant is a CH00 plant of the back-cast fleet, and no district-heating plant is built;
- every investment variable is fixed;
- no fuel or energy-budget constraint and no electrolyzer window is active;
- no demand-side response plant is built, `slack_soc` is off, and neither the Swiss RES target nor the
  winter import limit is active;
- no part built on the static calendar is active (DSR days, EV weeks, the building heat state,
  district-heating weeks);
- no variable preset applies to the back-cast meta-scenario.

It then audits the objective and prints each group of cost terms with its unit: hydro O&M, lost load and
trade in EUR, investment in CHF. An investment term with a non-zero coefficient or constant stops the run;
terms whose variables are all fixed, such as the costs of fixed plants, appear as a constant offset.

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
- **Representative hydro fleet** (`input/hydro_PECD`): `large_psp`, `medium_reservior` and
  `small_reservior`, the closed loop `CH00_psp_close`, and `CH00_dam` with zero capacity. Storage
  efficiencies, O&M and emission factors come from the cost workbook, used only where its 2035 and 2050
  values agree; availability comes from `input/availability_factor_conventional.csv` and must not vary
  by month (0.966).
- **No investment and no fuel model.** Every investment slope is 0, the operating-cost calibration is 1,
  and the set of limited fuels is empty.
- **Lost load** as in the normal import.
- **Border trade**: observed prices and transfer limits on the four border lines (next section).
- **Swiss demand**: option A as the demand of `CH00_fixedconsumer` (section Demand).
- **Renewable infeed**: observed solar, wind and run-of-river generation (section Renewable infeed).

Nothing outside the back-cast fleet is built: EV, V2G, electrolyzer, building heat pumps, district
heating, fuel limits, KVA infeed, energy limits and investment candidates stay empty.

## Borders

The back-cast trades with Austria, Germany, France and Italy at observed prices within observed limits,
read from the processed folder for every hour of the period:
- **Prices** from `prices_day_ahead_hourly.csv`: day-ahead, EUR/MWh, hourly mean of the quarter-hours,
  each neighbour's price on its border line. Negative prices are kept.
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

Observed Swiss generation from `ch_public_power_hourly.csv` (Energy-Charts public power) is the
infeed of `CH00_fixedconsumer` at every hour of the period:

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

## Run records

After the import, `core_main` writes two files into `output/<meta-scenario>/`:
- `historical_time_map.csv`: model step, UTC hour and local hour of every hour of the period;
- `historical_inputs_manifest.json`: the period and model steps, every file read with its checksum and
  columns, the assumptions, and the git commit with a flag for uncommitted changes.

## Status

The import builds the structure, the hydro fleet, lost load, border trade, Swiss demand and renewable
infeed. Until the following inputs are built, a back-cast stops with an error that names them: fixed
plants, reservoir inflow and reservoir stocks.
