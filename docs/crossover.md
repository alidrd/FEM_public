# Crossover and the duals

Checked against: `main` @ e27cbba (2026-10-19).

What the solve on `main` returns without Gurobi's crossover, how to switch crossover on, what it
costs, and how much each dual changes, measured on four full-year runs. The short version is the
"Crossover" section of [duals.md](duals.md).

## The solver setting

`model/core.py` solves with `solver_parameters = "threads=8 method=2 crossover=0"` (since commit
31ff4d9, 20 August 2026): Gurobi's barrier method (`method=2`) without crossover. Barrier ends at an
interior point of the set of optimal solutions; crossover would move it to a vertex (a basic
solution). To switch crossover on, delete `crossover=0` from that string. The setting is not a
scenario setting and is not written to `settings.csv`; a run's `solver_log.log` shows
`Set parameter Crossover to value 0` when crossover was off.

Without crossover:

- the objective and the capacities are the same as with it;
- primal values that are zero at a vertex come back as small positive numbers: in the test run, 1,784
  of the 1,798 hourly heat-generation values (`genTh.csv`) that are exactly zero with crossover lie
  between 0 and 2.4e-7 MW without it. Compare primal values with zero using a tolerance;
- duals and reduced costs are a point inside the set of optimal duals, not a vertex of it. Where that
  set is a single point they agree with a vertex solution; where it is large (degenerate LP, see
  "Non-unique duals" in [duals.md](duals.md)) they do not.

## What crossover costs

Full-year 2050 runs, 8 threads. Barrier takes about as long on both machines, so the crossover
column is the extra time.

| Run | Barrier, with crossover | Crossover | Barrier, without crossover |
|---|---|---|---|
| Weather year 1995 | 7.2 min | 34 min | 7.0 min |
| Weather year 2008 | 14.2 min | 3.8 h | 14.5 min |
| Weather year 2009 | 14.5 min | 51 min | 14.3 min |
| Three weather years | 47 min | 10.7 h | 50 min |

With crossover, the one-year runs ran on a laptop (Intel Core Ultra 7 155H, Gurobi 12.0.1) and the
three-year run on Euler (AMD EPYC 7763, Gurobi 10.0.3); without crossover, all four ran on Euler
(AMD EPYC 7742, Gurobi 10.0.3). On Euler a three-year run takes about 2 h 50 min without crossover
([running.md](running.md), "HPC runs"), so about 13.5 hours with it, within the job script's 24 hours.

## How much the duals change

### Setup

Run year 2050, the full hydrological year, weather years 1995, 2008 and 2009 each in its own
optimisation and the three together (weight 1/3 each), `DUALS_EXPORT_ALL = True`. Each run was
solved twice with the same scenario settings: with crossover (`threads=8 method=2`, on another branch
with the same model) and without (`main`, Euler job 17294088). The two `settings.csv` files differ
only in the branch and model-version rows.

- Objectives agree to a relative 7e-9 (three years: 2.8e-7).
- Every capacity in `gen_max.csv` and `genTh_max.csv` agrees within 10 kW, except `V2G_CH` (up to
  33 MW apart).

Each dual file was compared row by row on its index. Hourly duals are divided by the sub-scenario
weight, so they are within-year values ([duals.md](duals.md), "Weights"). "Non-zero" means
|dual| > 1e-6.

### Results

- **Electricity prices** (`energy_balance`) and **line-limit duals** (`lineATClimit`): unchanged
  for any practical use. The largest difference is 0.005 CHF/MWh, 99% of node-hours differ by less
  than 1e-5 CHF/MWh, and the number of non-zero entries changes by less than 0.1% (prices) and 1 to
  5% (line limits).
- **District-heat prices** (`energy_balancethermal`): within 1.3 CHF/MWh at the five `DH_*` nodes;
  up to 47 CHF/MWh at the industrial nodes, where some nodes differ by more than 1 CHF/MWh in more
  than a third of the hours (table below).
- **Storage values** (`storage_soc`): 4.6 to 6.0% of entries differ by more than 1 CHF/MWh.
- **Heat capacity limits** (`generationTh_limit`): 3.4 to 4.2 times as many non-zero entries without
  crossover. A count of binding hours is not comparable between the two solves.
- **Reduced costs** (`gen_max_reduced_cost.csv`, `genTh_max_reduced_cost.csv`): built candidates
  agree. Of the 343 unbuilt candidates (counted once per run), 214 have a reduced cost above 1% of
  their annuity with crossover and below 0.1% without it: 90 CCGT-CCS plants, 60 gas boilers, 21
  resistive heaters, 16 TTES, 8 heat pumps and 19 other gas, oil and hydrogen plants.
  `break_even_overnight` then says they are almost competitive. Example, weather year 1995,
  `DH_Alpen_CHPNew`: reduced cost
  86,387 with and 7.9 without crossover (CHF per MW and year; annuity 111,266), so
  `break_even_overnight` is 350,639 with and 1,568,067 without (overnight cost 1,568,179 CHF/MW).
  The other 129 agree: 67 Swiss rooftop-PV and onshore-wind candidates with a positive reduced cost,
  62 with zero in both.

Per dual and run (CHF/MWh; `generationTh_limit` in CHF per MW and hour):

| Dual | Run | Largest difference | 99th percentile | Share > 1 | Non-zero with | Non-zero without |
|---|---|---|---|---|---|---|
| `energy_balance` | wy1995 | 0.0027 | 4.8e-06 | 0.0% | 221,780 | 221,868 |
| `energy_balance` | wy2008 | 0.0024 | 1.5e-06 | 0.0% | 222,690 | 222,700 |
| `energy_balance` | wy2009 | 0.00055 | 3.5e-06 | 0.0% | 221,727 | 221,878 |
| `energy_balance` | 3 years | 0.0052 | 5.9e-06 | 0.0% | 667,697 | 667,914 |
| `lineATClimit` | wy1995 | 0.0027 | 5.2e-06 | 0.0% | 151,971 | 158,446 |
| `lineATClimit` | wy2008 | 0.0024 | 1.9e-06 | 0.0% | 152,744 | 154,520 |
| `lineATClimit` | wy2009 | 0.00055 | 4.3e-06 | 0.0% | 156,430 | 161,014 |
| `lineATClimit` | 3 years | 0.0052 | 6.4e-06 | 0.0% | 418,866 | 425,619 |
| `energy_balancethermal` | wy1995 | 47 | 3.64 | 5.6% | 130,754 | 131,285 |
| `energy_balancethermal` | wy2008 | 32 | 2.58 | 4.5% | 129,865 | 130,923 |
| `energy_balancethermal` | wy2009 | 30 | 13 | 9.3% | 130,537 | 131,367 |
| `energy_balancethermal` | 3 years | 47 | 2.52 | 3.8% | 375,590 | 393,597 |
| `storage_soc` | wy1995 | 58 | 6.67 | 6.0% | 604,724 | 604,969 |
| `storage_soc` | wy2008 | 39 | 6.16 | 6.0% | 607,462 | 607,540 |
| `storage_soc` | wy2009 | 583 | 8.04 | 5.3% | 605,380 | 605,997 |
| `storage_soc` | 3 years | 54 | 5.96 | 4.6% | 1,820,680 | 1,821,192 |
| `generationTh_limit` | wy1995 | 86,811 | 19 | 33.0% | 144,915 | 554,910 |
| `generationTh_limit` | wy2008 | 69,851 | 13 | 32.7% | 151,391 | 537,410 |
| `generationTh_limit` | wy2009 | 77,894 | 27 | 35.3% | 137,203 | 571,716 |
| `generationTh_limit` | 3 years | 111,879 | 22 | 30.4% | 457,703 | 1,564,396 |

Heat prices per node: share of hours differing by more than 1 CHF/MWh / largest difference
(CHF/MWh):

| NodeDH | wy1995 | wy2008 | wy2009 | 3 years |
|---|---|---|---|---|
| `DH_Alpen` | 0.0% / 1.0 | 0.0% / 0.5 | 0.0% / 0.2 | 0.0% / 0.6 |
| `DH_Jura` | 0.0% / 1.0 | 0.0% / 0.4 | 0.0% / 0.2 | 0.0% / 0.5 |
| `DH_Mittelland` | 0.0% / 1.0 | 0.0% / 0.4 | 0.0% / 0.2 | 0.0% / 0.4 |
| `DH_Voralpen` | 0.0% / 1.0 | 0.0% / 0.5 | 0.0% / 0.2 | 0.0% / 0.6 |
| `DH_medium` | 0.1% / 1.3 | 0.0% / 0.4 | 0.0% / 0.5 | 0.0% / 0.4 |
| `ILHT_Alpen` | 37.0% / 47.2 | 37.6% / 26.7 | 34.9% / 24.4 | 36.4% / 47.2 |
| `ILHT_Alpensuedseite` | 1.0% / 20.2 | 0.2% / 12.0 | 1.1% / 19.0 | 6.7% / 35.0 |
| `ILHT_Jura` | 1.0% / 24.5 | 0.2% / 32.3 | 0.8% / 19.6 | 0.7% / 30.6 |
| `ILHT_Mittelland` | 2.9% / 22.5 | 2.3% / 19.3 | 45.9% / 29.6 | 1.5% / 25.9 |
| `ILHT_Voralpen` | 3.1% / 6.1 | 4.0% / 6.3 | 40.3% / 29.2 | 0.7% / 7.2 |
| `ILLT_Alpen` | 5.3% / 9.6 | 4.7% / 9.8 | 2.3% / 11.7 | 2.1% / 14.0 |
| `ILLT_Alpensuedseite` | 5.5% / 10.0 | 6.6% / 20.3 | 2.9% / 12.8 | 1.1% / 11.3 |
| `ILLT_Jura` | 16.3% / 18.9 | 4.3% / 4.7 | 2.9% / 19.6 | 1.6% / 22.2 |
| `ILLT_Mittelland` | 6.7% / 8.9 | 3.0% / 7.3 | 5.7% / 16.9 | 4.2% / 20.7 |
| `ILLT_Voralpen` | 5.7% / 10.5 | 4.8% / 4.2 | 2.4% / 11.7 | 1.2% / 10.2 |

## When to switch it on

- **Crossover off is enough** for capacities, dispatch, costs, emissions, electricity prices and
  line-limit duals.
- **Switch it on** for runs whose heat prices at the industrial nodes, storage values, capacity
  rents, binding-hour counts, reduced costs or break-even costs are analysed.
- A vertex is still only one of several optimal dual solutions, so compare duals only between runs
  solved the same way.

## Quick check on the test run

In the 25-hour test run ([running.md](running.md), "Test run") crossover adds 0.08 seconds to
0.41 seconds of barrier. With and without crossover, the electricity prices agree to 3e-8 CHF/MWh,
while the industrial heat prices (`ILHT_*`, `ILLT_*`) differ by up to 168 CHF/MWh in 10 to 19 of the
25 hours, with the same 25-hour mean. The test run therefore shows the same pattern within minutes.
