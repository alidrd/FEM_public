# Known data issues by period

**Read this before choosing a historical period or an input source for a CH_only back-cast.** Each entry
names the period and source it affects and the condition under which it applies. If a planned run meets that
condition, stop and decide explicitly how to handle the issue before building model inputs.

| ID | Affects | Source | Status |
|---|---|---|---|
| KDI-1 | Any run whose period includes **December 2023 - February 2024** (e.g. hydrological year 2023/24, calendar year 2024) **and** uses PECD v4.2 as Swiss reservoir inflow | Copernicus PECD v4.2, CH00, HRI | Open; logged 2026-10-09 |
| KDI-2 | Any run whose period includes **26 June 2024** and reads the Austrian day-ahead price | ENTSO-E A44 and Energy-Charts (SMARD), AT | Open; logged 2026-10-23 |
| KDI-3 | Any run with `historical_generation_source = "entsoe"` | ENTSO-E A75, CH | Known limitation; logged 2026-10-23 |
| KDI-4 | Any use of ENTSO-E generation per unit (`ch_generation_units_entsoe_hourly.csv`, e.g. `historical_hydro_fixed_units`) | ENTSO-E A73, CH | Open; logged 2026-10-23 |

## KDI-1: PECD v4.2 Swiss reservoir inflow is near zero in January 2024

**Applies if both hold:**
- the period includes December 2023 to February 2024;
- Swiss reservoir inflow comes from PECD v4.2 (series HRI, zone CH00).

It does **not** apply to hydrological year 2025/26, and not if inflow comes from the BFE storage balance.

**Symptom.** CH00 HRI is exactly 0 in the weeks starting 1 and 8 January 2024, and 2.0 and 6.7 GWh in the
next two weeks. The first four weeks of 2024 add up to 8.7 GWh, against 192-496 GWh in 2023, 2025 and 2026.
The zeros are in the raw file
`H_ERA5_ECMW_T639_HRI_0000m_Pecd_SZON_S202401010000_E202412230000_NRG_TIM_07d_COM_noc_org_NA_NA---_NA---_StRnF_PECD4.2_fv1.csv`
(published 17 June 2025).

**Why it is very likely an artefact:**
- Austria, France and northern Italy show no dip in the same weeks.
- BFE run-of-river output in December 2023 and January 2024 was the highest of those months in 2017-2025.
- The BFE storage balance gives 583 GWh (534-632) of natural inflow for January 2024; PECD has 78 GWh.
- In PECD 2021.3 (1982-2017) the lowest total for the same weeks is 177 GWh.

The data provider has not confirmed it.

**Size.** About 500 GWh too little inflow in January 2024, roughly 2% of a year's reservoir inflow, concentrated
in one winter month when water value is high.

**What to decide before such a run:**
- whether to use the BFE storage-balance residual instead of PECD for that winter;
- or to keep PECD and report the run as affected by KDI-1;
- or to check first whether a newer PECD file version corrects it.

Any replacement must be explicit and documented; never fill or scale values silently.

**Evidence:** the figures above. The analysis script and plot (section G of a local investigation,
2026-10-09) are kept outside the repository.

**Related.** The 2026 PECD values come from interim ("ITE") files. Copernicus may revise them, so record the
file versions used in any run.

## KDI-2: two Austrian day-ahead prices on 26 June 2024

**Applies if** the period includes 26 June 2024 and the run reads the Austrian price (border AT00).

**Symptom.** On that day the two sources disagree in all 24 hours:
- ENTSO-E A44, classification sequence 1 (the series `prices_day_ahead_hourly.csv` uses): an ordinary curve,
  mean 95.4 EUR/MWh.
- Energy-Charts (SMARD), kept in `prices_day_ahead_energy_charts_hourly.csv`: mean 236.3 EUR/MWh, from -64.7 to
  1,965.9 EUR/MWh.

On every other day of Oct 2023 - Sep 2026, and for every other zone, the two are equal in every hour
(`validation_report.csv`). Neither publisher states which auction its Austrian price for that day comes from.

**What to decide before such a run:** which of the two prices stands for the Austrian border on that day.
Neither is filled or replaced silently.

## KDI-3: ENTSO-E covers only part of Swiss generation

**Applies to** runs with `historical_generation_source = "entsoe"` (the table `ch_generation_entsoe_hourly.csv`).

**Symptom.** Against the BFE monthly balance over Oct 2023 - Aug 2026 (`validation_report.csv`), ENTSO-E A75
(published by Swissgrid) holds solar 67 %, run of river 52 %, wind 95 % and nuclear 100 % of BFE's production,
and nothing for thermal and waste plants. Energy-Charts matches BFE within 1 % for every type.

**Consequence.** Used as published, the series remove several TWh a year of Swiss supply from a back-cast whose
demand equals BFE's national consumption. `"entsoe_bfe_months"` keeps ENTSO-E's hourly profiles and BFE's
monthly levels instead (`generation.py`).

## KDI-4: ENTSO-E generation per unit has gaps, zeros and partial units

**Applies to** any use of `ch_generation_units_entsoe_hourly.csv` (ENTSO-E A73), checked over Oct 2025 - Jul 2026.

**Symptoms:**
- **Zeros for plants in service.** Usine de Nendaz and Usine de Fionnay are 0 in every hour, though Grande Dixence
  finished renovating both in 2023 and had them back in full operation by the end of winter 2023/24. Their
  output is most likely unreported, not absent.
- **Days missing.** All Axpo units (Limmern, Tierfehd, Mapragg) and Beznau and Leibstadt have no data on 13, 14,
  16 and 17 October 2025.
- **The 25th hour.** On 26 October 2025 the hour 22:00-23:00 UTC is missing for every unit: a request for that
  hour alone returns "No matching data found", while some units publish it inside a longer window.
- **Partial units.** "KWO Produktion" reaches at most 918 MW and 447 GWh over the ten months, a part of KWO's
  plants. "Châtelard" is published from October to December 2025 only.
- **Above WASTA capacity.** Mapragg reaches 299.6 MW against 279.9 MW in WASTA; "Mittelbünden Turbinen" 199 MW
  against the 165.5 MW of the three WASTA plants mapped to it.

**What to decide before such a run:** which units to trust. `input/historical/entsoe_unit_groups.csv` records the
choice per plant group (`fix` and `note`); a missing hour is never filled.
