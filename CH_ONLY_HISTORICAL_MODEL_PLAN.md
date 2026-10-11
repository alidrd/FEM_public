# Historical CH-only runs: code changes needed (plan)

Prepared 2026-10-09 for `feature/historical-backcast-data`.
- Revised 2026-10-09 after review.
- Revised 2026-10-11 with corrections from a review by Codex (kept outside this repository), each checked
  against the code.
- Revised 2026-10-11 with the user's decisions 10-15, including a **separate historical import** (section 1).
- Revised 2026-10-11 with decisions 16-24 and the ticket index (section 12): GitHub issues #1-#25 on
  `alidrd/FEM_public`. Terms follow `CONTEXT.md`.

**Plan only; nothing below is implemented in FEM.**

**Input.**
- Most tables come from the processed output of this repository's pipeline, `data_prep/historical`
  (`input/historical/processed/<period>/`, `time_utc` index, UTC interval start). These include
  `ntc_dayahead_combined_hourly.csv` (5.2) and Swiss demand option A, `ch_demand_option_a_hourly.csv` (5.3).
- Reservoir inflow needs Copernicus PECD v4.2 weekly CH00 inflow (HRI, HOL in MWh).
  - **This repository does not import PECD yet.** A PECD import into `data_prep/historical` is a
    prerequisite for the first run (decision 17, #9). It reads the CDS archive downloaded by hand and writes
    the weekly table to the processed folder, which stays the only interface (decision 16).
- A portable bundle with checksums, needed for HPC runs, comes later.

**Pipeline status (2026-10-11).** The two preprocessing bugs Codex found are fixed and tested:
- partial months in demand option A now stay NaN;
- ENTSO-E API data now feed the combined NTC.

The processed tables were regenerated: every data file is byte-identical to before, and only
`provenance.json` changed. Regenerate provenance once more from committed code before staging the model
bundle.

**First run:** 1 Oct 2025 - 30 Aug 2026, reservoir end level equal to the observed level, the repository's
2030 representative hydro fleet.

## 1. Design rules

- **One master switch.**
  - `historical_period` (default `False`) names the delivery period, e.g. `"2025-10-01/2026-08-30"`: local
    dates, both days included. It is converted once to a UTC interval with an exclusive end
    (`[2025-09-30 22:00, 2026-08-30 22:00)` UTC).
  - With `False`, nothing runs differently. A regression test checks existing scenarios (section 10).
- **Data side: a separate historical import (decision 14).**
  - **The switch:** one `if` in `data_import_TYNDP_fcn`, after the lost-load block, so lost load is read as
    today:

        if settings_scen["historical_period"]:
            import_historical_inputs(scenario_name, settings_scen)
            return

  - **The new file:** everything else lives in `data_prep/historical_import.py`. In historical mode none of the
    normal readers run, so no future file is read and no future value can reach the model.
  - **What it builds:** the same global lists and dicts the normal import fills, so the model code stays
    unchanged (inventory in 5.0).
  - **Reused helpers:** only those that read no year- or weather-dependent data, namely the hydro fleet table
    and cost values that are the same in both workbook years (section 2).
- **Model hook after the variable presets.** `apply_historical_constraints(model, ...)` runs in
  `model/core.py` after `apply_variable_presets` (`core.py:174-176`), not after `fixing_capacities_central`.
  A preset could otherwise undo a historical fix. The function raises if a preset touches a variable it fixes
  or excludes.
- **All new logic lives in two new files**, one function per input:
  - `data_prep/historical_import.py` (data side);
  - `model/components_historical.py` (model side).
- **Validate first, then write.** All historical inputs for a sub-scenario are read and checked before any
  global dict is changed, so a failed import leaves no partial state. Keys carry the sub-scenario, so one
  scenario's values do not reach another.
- **Errors over fallbacks.** Any missing hour, missing column, duplicate or non-finite value inside the period
  raises, naming the series and hours. Zero NTC, zero inflow and negative prices are valid values.
- **Each run records what it used.** It writes `historical_time_map.csv` (t ↔ UTC ↔ local) and
  `historical_inputs_manifest.json` (files, checksums, columns, assumptions) into `output/<run>/`. Nothing
  later infers dates from `t` labels.

## 2. `run_year` and `weather_year` in a historical run

**The target period is `historical_period`, not `run_year`.**
- `run_year` means a future scenario year; its files (costs 2035/2050, CH capacities 2030-2050, demand,
  EV, electrolyzer, district heating) have no 2025 or 2026 entries.
- With the separate import (section 1) these readers do not run in historical mode, so `run_year` and
  `weather_year` have no role there. They keep whatever value the scenario file gives.

**Only two reused inputs could depend on a year:**
- **The hydro fleet table** `PECD_EERA2021_reservoir_pumping_2030_table_representative_plants.csv`. It is a
  fixed file, not chosen by `run_year`; decision 4 uses it as is.
- **Cost workbook values** for the technologies that remain: hydro O&M and the efficiencies of `psp_open` and
  `psp_close`. The import uses a workbook value only if it is identical for 2035 and 2050, which holds for
  these (O&M 0.1 EUR/MWh, efficiency 0.87); otherwise it raises. The fixed plants' operating cost is set to 0
  and documented: their output is fixed, so the cost would only add a constant.

**The table below shows where each dependency goes** (all replaced or absent):

| Depends on `run_year` / `weather_year` | Treatment in a historical run |
|---|---|
| Swiss demand | Replaced (option A, 5.3) |
| PV, wind, run-of-river | Replaced by observed generation (5.4) |
| Reservoir inflow | Replaced by PECD v4.2 historical (5.6) |
| Household heat pumps, EV, district heating, electrolyzer | Switched off (5.8, 6.3) |
| CH plant capacities | Nuclear and "others" from data (5.5); the rest of the thermal fleet off (6.3); hydro fleet kept (not `run_year`-dependent) |
| Fuel and CO2 prices, plant O&M | Reach dispatch only through freely dispatched plants. Nuclear and "others" are fixed and gas plants off; the only priced Swiss dispatch is hydro (0.1 EUR/MWh O&M). |
| Transfer limits, neighbour prices | Replaced (5.1, 5.2) |

If a thermal plant is dispatched freely in a later variant, its fuel, CO2 and O&M costs need historical
values (6.4).

**Invariance test (cheap check of the above).**
- The historical model is built (not solved) with `run_year` 2035/2050 and `weather_year` 1995/2008/2009.
- Compared are the canonical model data, by constraint and variable name: coefficients, bounds, right-hand
  sides and objective coefficients. LP text and ordering are not compared.
- Any difference fails the test and is listed.

## 3. Settings (`scenarios/settings_default.py`)

| Setting | Default | Meaning |
|---|---|---|
| `historical_period` | `False` | Master switch and target period, `"YYYY-MM-DD/YYYY-MM-DD"` (local, inclusive). Sets the time window (section 4). Requires `CH_only=True` and `allow_investment=False`. |
| `historical_data_dir` | `False` | Processed folder of `data_prep/historical`, e.g. `"input/historical/processed/2023-10-01_2026-09-30"`. May span several years; only `historical_period` is used. |
| `historical_demand` | `"demand_MW"` | Column of `ch_demand_option_a_hourly.csv`; the extremes `demand_solar_shaped_MW` and `demand_flat_MW` are for sensitivity runs |
| `historical_hydro_end` | `"equal"` | National reservoir level at the period end: `"equal"` (decided), `"at_least"` or `"free"` |

**No hybrid runs in the first version.**
- The earlier `historical_replace` list, which mixed future and historical inputs, is dropped.
- With a separate import, mixing would need the future readers again.
- Sensitivities use the settings above (e.g. `historical_demand`, `historical_hydro_end`) or other data files.
- If hybrids are needed later, they are a separate feature.

**Settings that must be `False`/`0` with `historical_period`.** They have no effect in historical mode, and the
run raises so that nobody believes they do:
- `hydro_inflow_TWh`, `ror_annual_TWh`, `pump_capacity_GW` and `adding_hydro_storage_cap_TWh`;
- `reduce_inflex_demand_by_[MWh]`;
- `slack_soc`;
- the winter import limit and the Swiss RES target;
- `NodeDH_list`, which must be `[]`.

`t_start` / `t_end` are derived from `historical_period`. If the scenario CSV sets them too, they must agree.
`NTC_CH_ratio` stays an explicit multiplier on the historical limits. Hydro spill stays optimized.

## 4. Time mapping (`historical_time_map`)

**Rule (first run).** The k-th UTC hour of the period becomes model step `t_((offset + k - 1) mod 8760) + 1`.
`offset` is the hour-of-year of the first local hour in a 365-day calendar, so 1 Oct 00:00 local (CEST) is
`t_6553`.

**These labels are positions, not calendar hours.** The earlier claim that "the existing week, day and month
maps stay valid" was wrong (Codex):

| Example | Value |
|---|---|
| 1 Jan 2026 00:00 local (CET) | `t_2`, because the static map's `t_1` sits one hour earlier in UTC |
| Model day blocks (`t // 24`) | Start 22:00 UTC: 00:00-00:00 local in summer, 23:00-23:00 local in winter |
| Static weekday labels | Call 1 Oct a Sunday; 1 Oct 2025 was a Wednesday |
| Static months | Shifted by one hour in winter |

**Decided (13): rotated labels for the first run.** This is acceptable only because nothing active depends on
the calendar. Each
calendar-dependent part is replaced or switched off, and `apply_historical_constraints` raises if one is
still active:
- daily: DSR (the CH plant tables have no DSR rows), run-of-river daily split (replaced);
- weekly: EV energy, building heat state, district heating (off), PECD weekly inflow reader (replaced);
- monthly: conventional availability (overwritten hourly for the fixed plants);
- windows: winter import limit, electrolyzer window (off).

| Period | `t_start` | `t_end` | Hours | Status |
|---|---|---|---|---|
| 2025-10-01 / 2026-08-30 (**decided**) | 6553 | 5808 | 8,016 | All inputs present |
| 2025-10-01 / 2026-08-31 | 6553 | 5832 | 8,040 | PECD ends 31 Aug 2026 00:00 UTC: 22 hours lack inflow |
| 2025-10-01 / 2026-09-30 | 6553 | 6552 | 8,760 | Swissgrid end-user data and BFE balance missing for September |

**Not supported by this mapping:** periods longer than 8,760 hours or containing 29 February (they raise),
and periods whose sub-scenarios have different timelines.

**Next step after the first run: chronological time sets** (Codex's recommendation).
- A historical time-set helper builds `t_1..t_N` in chronological order, with real local days and weeks from
  the UTC timestamps.
- It is needed before any leap period, e.g. Oct 2023 - Sep 2024 with 8,784 hours.
- It changes how the existing code builds `T`, `Days` and `Weeks`, but only when `historical_period` is set.
- 29 February is never dropped silently.

**KDI-1** (`data_prep/historical/KNOWN_DATA_ISSUES.md`): PECD v4.2 inflow is near zero in January 2024. It
does not affect 2025/26.

## 5. Data-side functions (`data_prep/historical_import.py`)

`import_historical_inputs` validates all inputs first, then fills the existing global lists and dicts, keyed
`(..., 't_k', scenario)`, for `t` in `T_list` only.

**5.0 What the import builds.** This is the inventory from `data_import_TYNDP_fcn`, the dicts the model code
reads.

| Group | Global names | Historical content |
|---|---|---|
| Nodes, consumers | `Consumer_list`, `Map_consumer_node`, `Map_node_consumer` | `CH00`, `CH00_fixedconsumer` only |
| Plants | `Plant_list`, `Map_plant_tech`, `Map_plant_node`, `Map_consumer_plant`, `Map_node_plant` | Hydro fleet from the table (`large_psp`, `medium_reservior`, `small_reservior`, `CH00_psp_close`; `CH00_dam` with 0 capacity as today), `CH03_nuclear`, `CH01_other`. Nothing else. |
| Capacities | `Plant_capacity_gen`, `Plant_capacity_pmp`, `Plant_capacity_strg` | Fleet table for hydro (via the existing `read_hydro_capacities`, with the scaling settings off); 5.5 for the fixed plants |
| Efficiencies, start | `Map_eff_in_plant`, `Map_eff_out_plant`, `Map_plant_startcondition` | Electrical basis for the three open plants (6.1); workbook values (year-independent) for closed loop |
| Availability | `Avail_plant` | Fixed plants: observed / capacity. Hydro: the value the normal import gives (to read and declare during implementation, Codex: 0.966). |
| Infeed | `Infeed_consumers`, `Infeedplant_list`, `Map_infeedplant_*` | 5.4 |
| Inflow, outflow | `Inflow_data`, `Plant_inflow_list`, `Outflow_data`, `Plant_outflow_list` | 5.6; no outflow |
| Lines, prices | `LineATC_list`, `Map_line_node`, `ATC_exportlimit`, `ATC_importlimit`, `Line_trade_price`, `Map_node_exportinglineATC`, `Map_node_importinglineATC` | 5.1, 5.2; four lines `HVAC_{AT,DE,FR,IT}00_CH00`, the existing names and direction |
| Demand | `Demand_data`, `EV_inflexible_demand_data`, `HP_inflexible_demand_data` | 5.3; EV and HP parts empty |
| Costs | `cost_data_opr_slp`, `cost_data_opr_int`, `op_cost_n_tech_calibration`, `emission_factor_per_MWh`, investment-cost dicts | Year-independent workbook values for the remaining technologies (section 2); investment dicts empty, since investment is off |
| Lost load | `lost_load_cost` | As today (`input/LostLoadCost`, not year-dependent) |
| Future-only modules | EV, V2G, electrolyzer, building heat pumps (`BA_*`), district heating (`PlantDH_*`, `DemandDH_data`), `Fuel_limits_data`, `KVAinfeed`, `Data_plant_energy_limited`, investment candidate lists | **Not built: empty.** Model code that assumes them must be guarded (section 7). |

The exact list is checked against `data_import_TYNDP_fcn` during implementation. A missing name shows up as a
`KeyError` when the model is built and is fixed in the import, not by a default.

**5.1 `neighbour_prices`** → `Line_trade_price` for `HVAC_{DE,FR,IT,AT}00_CH00`.
- Source: `prices_day_ahead_hourly.csv` (DE00, FR00, IT00, AT00; EUR/MWh; hourly means of the
  quarter-hours).
- Negative prices are kept.
- `read_line_data` does not run in historical mode. Its silent zero fallback remains a risk for
  non-historical CH_only runs (separate fix, section 7).

**5.2 `transfer_limits`** → `ATC_exportlimit` and `ATC_importlimit`.
- Source: `ntc_dayahead_combined_hourly.csv`, complete for Oct 2025 - Sep 2026.
  - Values are Swissgrid D-2, with ENTSO-E for hours without D-2 and for 24 Sep 2026.
  - ENTSO-E means the API download once the token arrives, else the web copy (`NTC_ENTSOE_SOURCE_ORDER`);
    each value keeps its source.
- The day-ahead NTC is used, not the intraday NTC.
  - Schedules above the day-ahead NTC fit the intraday NTC in most hours. This is consistent with intraday
    trading but does not prove it.
  - Scheduled flows serve as a diagnostic only, not as an exact day-ahead clearing.
- Bounds follow each line's own start and end nodes: for `HVAC_X00_CH00` the upper bound is `X_to_CH` and the
  lower bound is `-CH_to_X` (`components_common.py:2166-2189`).
- Hourly values are the minimum of the quarter-hours.
- Raises on a duplicate representation of a border.

**5.3 `swiss_demand`** → `Demand_data[('CH00_fixedconsumer','fixed',t,s)]`; `EV_inflexible_demand` and
`HP_inflexible_demand` become 0.

**Decided: option A** (`data_prep/historical/demand.py`), column `historical_demand`:

    demand(t) = Swissgrid end-user consumption(t) + grid losses(t) + on-site consumption(t)

- **Excludes storage pumping** (a model decision).
- **Includes consumption covered by on-site generation**, because the solar infeed is total PV production.
- **Monthly totals equal BFE Landesverbrauch.** They are computed over complete local calendar months before
  the window is cut.
- **Hourly placement of on-site consumption is an assumption.** `demand_MW` places 50% like PV output and 50%
  flat (user decision 2026-10-09). The extremes differ by about 1.6 GW at noon in July 2026, so state this
  with every result.
- **Rejected:** Swissgrid total consumption and Energy-Charts load (both contain pumping), and Swissgrid
  end-user consumption scaled to BFE (misses self-consumed PV at midday).

**5.4 `res_infeed`** → `Infeed_consumers[('CH00_fixedconsumer', tech, t, s)]`.
- Techs: `pvrf` = solar, `windon` = wind, `ror` = run-of-river, from `ch_public_power_hourly.csv` (observed,
  equal to BFE monthly).
- Other CH infeed keys become 0, and no future fixed infeed remains.
- Infeed is reported by technology in `infeed.csv`.
- Curtailment stays possible.

**5.5 `fixed_generation`**: nuclear and "others" are plants whose output equals the observed series.

| Item | Plant (existing) | Observed series | Capacity |
|---|---|---|---|
| `nuclear` | `CH03_nuclear` | Energy-Charts `nuclear` | 2,973 MW net (`NUCLEAR_UNITS_NET_MW`); maximum observed in the 8,016-hour window 2,885.4 MW |
| `others` | `CH01_other` | Energy-Charts `others` | Energy-Charts installed power of the "others" categories (see below) |

- **Category mapping for "others":** every Energy-Charts installed-power category except hydro, nuclear, wind
  and solar:
  - conventional thermal and landfill gas;
  - waste incinerators with and without heat extraction;
  - industry, district-heating plants and small CHP.
- **Capacity year:** the latest published year not after each hour. Energy-Charts publishes up to 2025, so
  2026 hours use the 2025 value. This is declared in the manifest.
- **Why the capacity choice is harmless:** output is fixed, so capacity only normalizes `Avail_plant`. The
  function raises if an observed value exceeds it.
- `Avail_plant[(p,t,s)]` = observed / capacity.
- `gen.csv` shows both sources by name.
- **Caveat on "others":** Energy-Charts gives one constant per month (13 distinct values in Oct 2025 -
  Sep 2026), i.e. the BFE monthly thermal total spread flat. It is not an hourly measurement, and plots must
  not suggest one.
- **For other years:** capacities and plant mapping are read from that year's data, not reused as constants.

**5.6 `hydro_inflow`** → `Inflow_data[(p,t,s)]`, assigned automatically to the current fleet.
- **Source:** PECD HRI and HOL weekly MWh, kept as separately labelled series in logs and the manifest.
  - Their sum is the natural inflow of the three open plants.
  - It is spread flat over each week's hours, keeping weekly MWh, then mapped to `t`.
  - Closed loop (`CH00_psp_close`) gets 0.
- **Split:** the `inflow_share` column of
  `input/hydro_PECD/PECD_EERA2021_reservoir_pumping_2030_table_representative_plants.csv`, the column the
  existing reader uses.
  - The table repeats the share on each plant's turbine, pump and storage rows, so the function takes one
    value per plant.
  - It raises if a plant's rows disagree, or if the plant shares do not sum to 1. Today: 0.716, 0.245, 0.039.
- **Week timing** follows the table's `start_utc` / `end_utc`. That is Codex's week-start/UTC convention,
  provisional and not verified against provider metadata; the manifest records it.
- **No missing week is interpolated.**
- **Energy basis:** PECD inflow is electrical-grid energy (PECD methodology as cited in Codex's plan, to be
  confirmed in the product documentation). This is why 6.1 changes the discharge efficiency.

**5.7 `hydro_levels`** (data for 6.1): national stocks at the two period boundaries.
- **Start stock:** at the period start instant, 1 Oct 2025 00:00 local (2025-09-30 22:00 UTC), before the
  first hour's flows.
- **End target:** at the period end instant, 31 Aug 2026 00:00 local (2026-08-30 22:00 UTC), after the last
  hour (30 Aug 23:00 local). Not at the start of the last hour.
- **Readings:** BFE weekly `TotalCH` stocks, dated Monday for Sunday 24:00 local, converted to UTC instants.
  - An exact reading is used directly.
  - Otherwise the two surrounding readings are interpolated linearly. They may be at most 8 days apart, and
    there is no extrapolation.
- **Scaling:** observed fill fraction (stock / BFE maximum, the maximum read from the same records) × total
  model storage of the three open plants. The plots show this scaling.
- **Start split to plants:** equal fill fraction, i.e. pro rata to storage capacity. This is an explicit
  assumption, since BFE observes only the national stock.

**5.8 Future-only modules.** They are not built (table 5.0), so they cannot double count observed load. This
covers:
- EV and V2G;
- the CH electrolyzer;
- household heat pumps and their thermal state;
- district heating, fuel limits, KVA infeed and energy limits.

## 6. Model-side functions (`model/components_historical.py`)

**6.1 `hydro_storage_boundary`.** For `large_psp`, `medium_reservior` and `small_reservior`.

**Energy basis (P1, Codex).**
- Today's balance subtracts `gen / storage_charge_eff_out`, and the discharge efficiency of `psp_open` is 0.87
  (`components_common.py:2014`; cost workbook).
- PECD inflow and BFE stocks are electrical energy, so 100 MWh of inflow would allow only 87 MWh of
  generation, about 2 TWh lost over the period.
- **Fix:** in historical mode these three plants get discharge efficiency 1 and charging efficiency 0.87 ×
  0.87 = 0.7569. Both are derived from the workbook values, not typed. The round trip is unchanged, and
  stocks, inflow and generation are all electrical MWh.
- **Fleet label:** this is the repository's 2030 representative fleet (storage about 8.8 TWh), not observed
  2025/26 capacities. The fleet table's `sto_GWh` is taken as electrical energy; check its basis once.

**Boundaries.** The constraint component is `model.storage_soc[p, t, s]`; its rule is
`storeBalance_Constraint`.
- **Deactivate** `storage_soc[p, T.first(), s]` (the cyclic link via `T.prevw`) and
  `storage_start_condition[p, s]` (the 0.95 start).
- **Add a first-hour balance:** the same terms as the existing rule (charging, discharging, inflow, outflow,
  spill), with the observed start stock of each plant (5.7) in place of `soc[prevw]`.
- **Add the national end target:** `Σ_p soc[p, T.last(), s] == S_end` for `"equal"`, or `>=` for
  `"at_least"`.
- **Unchanged:** all later hourly balances, optimized spill, and closed-loop storage (cyclic, with its
  declared start condition; it has no observed boundary data).

**6.2 `fixed_generation`.**
- `gen[p, t, s] == observed[t]` for each plant in table 5.5.
- The historical import builds no energy or fuel limit for these plants, so the 2.78 TWh/yr `other` cap
  (decision 7) does not exist in historical mode. The function asserts that no such constraint is active for
  them.

**6.3 `check_historical_model`.** With the separate import, the 2050-only assets are not built at all:
- the rest of the 2050 thermal fleet;
- district heating;
- `ev_flex`, `v2g`, `electrolyzer` and `heat_pump_households`;
- batteries (excluded by declared fleet policy);
- fuel storage.

Nothing needs to be fixed to 0. The function only asserts the built model:
- the plant set is exactly the historical fleet (table 5.0);
- no investment variable is free;
- no fuel or energy-budget constraint is active;
- DSR is empty and `slack_soc` is off;
- no Swiss RES target or winter import limit is active;
- no variable preset touches a historical variable;
- no calendar-dependent part is active (section 4).

**6.4 Hourly operating cost (optional; NOT implemented now).** Every plant gets a variable cost per hour,
driven by fuel and CO2 price series.

**Today.**
- The objective (`model.OBJ`, `core.py:158`) uses `operation_slp[p, s] × gen[p, t, s]`
  (`components_common.py:162`, `:198`).
- `operation_slp` = O&M + fuel/efficiency + CO2 from `cost_assumptions.xlsx` for `run_year`.
- District heating has `operation_slpTh` (`:261`).

**Requirement: all plants**, electric and thermal:

    cost[p, t, s] = O&M[p] + fuel_price[fuel(p), t] / efficiency[p] + co2_price[t] × emission_factor_per_MWh[p]

- `emission_factor_per_MWh` is already per MWh of output, efficiency included (`components_common.py:804`).
  It is not divided again.
- A fuel without a series keeps its constant workbook price, and the log names it.
- Plants without fuel keep O&M only.
- CHP fuel costs follow the existing `PDH_costExclusivelyOnThermalSide` rule, so they are not counted twice.
- Raises on an unmapped fuel or a gap in a series.

**Formulation.**
- `operation_slp[p, s]` stays for existing runs.
- When `hourly_operating_cost` is on, an additional parameter `operation_cost_hourly[p, t, s]` is built.
- One accessor function returns the cost for (p, t, s) in either mode. Both the objective and
  `price_setting_tech.py:240,303` use it.
- The aggregation scripts (`aggregate.py:453`, `aggregate_new.py:524`, `aggregate_results.py:69`) and
  `docs/duals.md:82` read the hourly file only in that mode.
- Existing runs keep today's parameters and file shapes.
- The earlier "variant A" (a time index in every run) is dropped: it would grow `operation_slp.csv` and memory
  8,760-fold in all runs.

**Inputs:**
- price series per fuel (EUR/MWh thermal) and CO2 (EUR/t), with units, calendar-day or gas-day mapping and
  source in their metadata;
- historical O&M, efficiencies and emission factors. The workbook values for 2035/2050 are not historical
  costs.

**Data reality:** gas (THE, TTF) and EUA spot prices are daily products, so an hourly series repeats each
day's price. Free sources are limited by redistribution terms.

**Effect:** none in the first run, where no thermal plant is dispatched freely. The reported objective total
does depend on the fixed plants' costs.

## 7. Edits to existing code

All logic stays in the two new files. Existing code gets only switches:
1. **Negative prices:** `model.line_trade_price` domain `NonNegativeReals` → `Reals`
   (`components_common.py:1201`). It changes nothing for non-negative inputs.
2. **Data switch:** one `if` at the top of `data_import_TYNDP_fcn` (section 1).
3. **Model switch:** one `if` in `core.py` after `apply_variable_presets` that calls
   `apply_historical_constraints`.
4. **Time window:** one `if` in `model/read_settings.py` that derives `t_start` / `t_end` from
   `historical_period`.
5. **Model code that assumes a future module exists** gets an `if` guard. Known case: the two EV constraints
   are built unconditionally and name `"CH00_EV_flex"` (`components_common.py:1832-1838`, `:2478`, `:2489`).
   They are built only if that plant exists. Other cases found during implementation are listed here. The
   nuclear lower bound in `core.py:188` is already guarded.
6. **New defaults** in `settings_default.py` (section 3).

**Separate fix, recommended:** the existing CSV price loader in `read_line_data` swallows every error and runs
with zero trade cost, and `.get(...,0)` adds the same risk. A raise there protects all CH_only runs.

## 8. Currency and active costs

- Neighbour prices and lost-load cost are EUR/MWh.
- Hydro O&M comes from `om_cost_eurMWH`: 0.1 EUR/MWh for open and closed pumped storage, no fuel or emission
  cost.
- Plant costs elsewhere in the workbook are CHF2017.
- With nuclear and "others" fixed, the gas fleet off and investment off, the active marginal costs are hydro
  O&M and lost load, both EUR. The CH energy-balance dual is therefore in EUR/MWh, comparable with the
  observed CH price.
- The check covers every active marginal cost and penalty in the objective, not just its size:
  - the adapter logs each term with its unit;
  - it raises if a CHF investment or fixed-cost term is still active;
  - it lists the fixed plants' costs as a constant offset.

## 9. Scenario file `scenarios/scen_to_run_CH_only_historical.csv`

The first column is `hist_2526_octaug`. Its rows:
- `CH_only=True`, `allow_investment=False`, `NodeDH_list=[]`, `slack_soc=False`;
- `historical_period="2025-10-01/2026-08-30"`;
- `historical_data_dir`, `historical_demand="demand_MW"`,
  `historical_hydro_end="equal"`;
- one sub-scenario with weight 1.

`target_csv` is set for the run and restored afterwards (AGENTS.md).

## 10. Tests and checks

**Unit tests (no solver).** Each is written to fail on wrong code:
- **Time and data:**
  - time map: offset, wrap, `t_start` / `t_end` derivation, DST hours, leap or overlong period raises;
  - inputs: missing, duplicate and non-finite values raise; zero NTC and inflow and negative prices are kept;
- **Borders:** limit direction per line, with asymmetric values (symmetric fixtures would hide a reversal);
- **Demand:** column choice and NaN check. The construction is covered in `tests/test_historical_demand.py`,
  including partial months and the 745-hour October.
- **Inflow:** one `inflow_share` per plant (repeated rows), shares sum to 1, and weekly MWh is kept across DST.
- **Reservoir levels:**
  - the start stock lies before the first hour and the end target after the last hour;
  - an exact reading is used directly;
  - a gap of more than 8 days raises, with no extrapolation;
  - equal period targets still give two separately interpolated stock values.
- **Fixed generation:** capacity check, and the category mapping for "others".

**Model-side unit tests** on a tiny Pyomo model (no solve):
- the first-hour balance is replaced and the national end target is set;
- **hydro energy budget:** 100 MWh electrical inflow, no pumping or spill, equal start and end stock →
  100 MWh of generation possible (87 MWh with the old efficiency);
- fixed output, with no energy or fuel limit on the fixed plants;
- `check_historical_model` raises on an unexpected plant, a free investment variable, a conflicting preset or
  an active calendar-dependent part;
- the EV guard: a model without `CH00_EV_flex` builds, and one with it still has both EV constraints.

**Import completeness:** building the model (no solve) from the historical import alone succeeds, and the plant,
line and consumer sets equal table 5.0.

**Invariance test** (section 2): canonical model data identical across `run_year` and `weather_year`.

**Regression:** `scen_to_run_test.csv` with `historical_period=False`, before and after the change.
- Capacities, relevant dispatch, objective and prices must agree within declared numerical tolerances.
- Any degenerate LP alternative is explained.
- Solver timing or statistics files are not compared byte for byte.

**Pilot runs:** a winter week, a summer week, then the full period. Check:
- the energy balance, limits respected, and price signs;
- the modelled national reservoir path vs BFE weekly levels;
- modelled border flows vs scheduled commercial exchange (diagnostic);
- physical flows only from Swissgrid metering, never Energy-Charts `cbpf`;
- curtailment, pumping and spill plotted.

**Price check:** compare the base CH dual with the objective change divided by a small added demand, away from
a kink. The exported duals already undo row scaling, and one sub-scenario with weight 1 needs no further
division.

**Price comparison** (`tools/compare_historical_prices.py`). It maps CH00 `energy_balance_dual.csv` to UTC
through the saved `historical_time_map.csv` and reports:
- MAE, RMSE, bias and correlation;
- monthly means and the price duration curve;
- hours at the border limits against the observed reference (FR→CH imports at the limit in 69% of hours,
  CH→IT exports in 34%);
- a benchmark (mean neighbour price).

August is partial (to 30 Aug), so August model totals are not compared with BFE monthly totals.

## 11. Decisions

| # | Topic | Status |
|---|---|---|
| 1 | Demand definition | Decided: option A, `demand_MW` (50/50 on-site split) |
| 2 | Run window | Decided: 1 Oct 2025 - 30 Aug 2026 (`t_start` 6553, `t_end` 5808, 8,016 h) |
| 3 | Reservoir end level | Decided: equal to observed |
| 4 | Hydro fleet | Decided: the repository's 2030 representative fleet; inflow split by its `inflow_share` |
| 5 | "Others" | Decided: fixed generation from the observed series, shown as its own source (5.5) |
| 6 | Hydro spill | Decided: optimized by the model (no new setting) |
| 7 | Annual cap on "other" | Decided: lifted for the fixed plant (6.2) |
| 8 | Hourly gas and CO2 prices | Optional, not implemented now (6.4); all plants, accessor-based |
| 9 | Target year | Decided: `historical_period`; `run_year` / `weather_year` have no role in historical mode (section 2) |
| 10 | Hydro energy basis | Decided: electrical basis; the three open plants get discharge efficiency 1, charging 0.7569, round trip unchanged (6.1) |
| 11 | End-target scope and start split | Decided: national end target; start split by equal fill fraction (5.7, 6.1) |
| 12 | Stock scaling | Decided: observed fill fraction × model storage, shown in plots (5.7) |
| 13 | Time labels | Decided: rotated labels with calendar-dependence assertions for the first run; calendar time steps before any leap period (section 4) |
| 14 | Import | Decided: separate historical import in its own file, switched by one `if`; no hybrid runs in the first version (sections 1, 3, 5.0) |
| 15 | "Others" capacity year | Decided: latest published Energy-Charts year (2025, about 1,200 MW, also for 2026 hours), declared (5.5) |
| 16 | Pipeline-model interface | Decided: the back-cast import reads only processed files, never pipeline code or constants (#8, #9) |
| 17 | PECD source | Decided: in-repo importer of the hand-downloaded CDS archive; the setting `historical_inflow_file` is dropped (#9) |
| 18 | EV guard | Decided: the two EV constraints are built only when `CH00_EV_flex` is a plant of the model (#3) |
| 19 | Meta-scenario isolation | Decided: each meta-scenario runs in its own process; the back-cast import also raises on global data left by another meta-scenario (#4, #11) |
| 20 | Sub-scenarios | Decided: exactly one sub-scenario, weight 1, per back-cast meta-scenario (#10) |
| 21 | Variable presets | Decided: a preset that applies to a back-cast meta-scenario raises (#12) |
| 22 | Tests needing local data | Decided: pytest marker `needs_data`, deselected by default; a selected test with missing data fails (#6) |
| 23 | Guides on feature branches | Decided: feature commits change guide bodies only; "Checked against" lines are refreshed after the merge into `main` (#7) |
| 24 | Commits | Decided: one commit per ticket, packaged for cherry-picking (section 12) |

## 12. Tickets and commits

The work is split into the GitHub issues #1-#25 on `alidrd/FEM_public`. Each issue holds the scope, the
acceptance criteria and its blockers (native GitHub dependencies).

**Packaging: one ticket, one commit.**
- The commit subject is the issue title. The commit holds the code, unit tests that fail on the old code, and
  the guide text it changes (guide body only, decision 23).
- The message references the issue and ends with `Requires:` and the subjects of all commits it needs, in
  order. A feature moves to another branch by cherry-picking those commits, then the feature.
- Plan, glossary and ADR edits go into docs-only commits, which no cherry-pick needs.
- #1-#7 and #10 apply to `main` on their own. #8 and #9 need this branch's two pipeline commits.

| Issue | Topic | Ticket | Blocked by |
|---|---|---|---|
| #1 | Generic | Allow negative trade prices | - |
| #2 | Generic | Raise on missing CH_only trade prices | - |
| #3 | Generic | Build EV constraints only when the EV plant exists | - |
| #4 | Generic | Run each meta-scenario in its own process | - |
| #5 | Generic | Compare two runs within declared tolerances | - |
| #6 | Generic | Opt-in tests that need local data | - |
| #7 | Docs | Refresh "Checked against" after the merge | - |
| #8 | Pipeline | Installed power by category and year | - |
| #9 | Pipeline | Weekly PECD inflow for CH00 | - |
| #10 | Back-cast | Settings and time map | - |
| #11 | Back-cast | Historical import skeleton | #10 |
| #12 | Back-cast | Model hook and shape checks | #10 |
| #13 | Back-cast | Neighbour prices and transfer limits | #1, #11 |
| #14 | Back-cast | Swiss demand | #11 |
| #15 | Back-cast | Renewable infeed | #11 |
| #16 | Back-cast | Fixed plants | #8, #11, #12 |
| #17 | Back-cast | Reservoir inflow on an electrical basis | #9, #11 |
| #18 | Back-cast | Start stock and end target | #12, #17 |
| #19 | Back-cast | Scenario file and data tests | #3, #6, #13-#16, #18 |
| #20 | Back-cast | Price comparison tool | #10 |
| #21 | Back-cast | First run and validation (run steps, no commit) | #5, #19, #20 |
| #22 | Later | Chronological model steps | #21 |
| #23 | Later | Hourly operating cost | - |
| #24 | Later | Automated PECD download | #9 |
| #25 | Later | Portable input bundle for HPC | #19 |

#1-#20 are labelled `ready-for-agent`, #21 `ready-for-human`, and #22-#25 `needs-triage`.
