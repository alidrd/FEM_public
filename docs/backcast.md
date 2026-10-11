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

Nothing outside the back-cast fleet is built: EV, V2G, electrolyzer, building heat pumps, district
heating, fuel limits, KVA infeed, energy limits and investment candidates stay empty.

## Run records

After the import, `core_main` writes two files into `output/<meta-scenario>/`:
- `historical_time_map.csv`: model step, UTC hour and local hour of every hour of the period;
- `historical_inputs_manifest.json`: the period and model steps, every file read with its checksum and
  columns, the assumptions, and the git commit with a flag for uncommitted changes.

## Status

The import builds the structure, the hydro fleet and lost load. Until the following inputs are built, a
back-cast stops with an error that names them: neighbour prices, transfer limits, Swiss demand,
renewable infeed, fixed plants, reservoir inflow and reservoir stocks.
