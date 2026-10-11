# Future Markets (FEM)

FEM optimises the European electricity and district-heating system, with Switzerland in detail.
This glossary fixes the words for back-casts, the runs that replay a past period of the Swiss system.

## Language

### Back-casts

**Back-cast**:
A CH_only optimisation of a past period, driven by inputs observed or reconstructed for that period, whose results are compared with observed outcomes.
_Avoid_: historical run, hindcast

**Historical period**:
The local calendar dates a back-cast covers, both days included.
_Avoid_: delivery period, run window, target period

**Model step**:
A position `t_k` in the model's hourly sequence. In a back-cast, a model step need not be the calendar hour its label suggests.
_Avoid_: hour (for the label)

**Back-cast fleet**:
The plants a back-cast builds: the representative hydro fleet and the fixed plants. Nothing else is modelled, since observed series already contain the energy of other loads and plants.
_Avoid_: future-only modules

**Representative hydro fleet**:
The Swiss hydro storage plants of the model, aggregated into a few representative plants with 2030 capacities.

**Fixed plant**:
A plant whose hourly output equals an observed series, such as nuclear or "others".
_Avoid_: must-run (a lower bound, not an equality)

### Reservoirs

**Reservoir stock**:
Energy stored in the reservoirs, in electrical MWh.
_Avoid_: level

**Start stock**:
The reservoir stock at the first instant of the historical period, before any flow of its first hour.

**End target**:
The national reservoir stock required at the last instant of the historical period.

**Fill fraction**:
A reservoir stock divided by the maximum stock reported in the same records.
_Avoid_: fill level
