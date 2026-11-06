"""Write a presets file that keeps the rooftop-PV candidates of some cost classes out of investment.

Copies a base presets file and adds `gen_max,,<plant>,,0.0` for every PV-roof candidate of the dataset whose cost
class (units.csv via model/high_res_PV.load_cost_classes, never the unit name) is not allowed. For installations up to
30 kWp: python data_prep/presets_pv_size_limit.py --base <presets.csv> --dataset pv09_ewopt --weather-year 2009
--allow 1-10kW 10-30kW --out <new presets.csv>
"""
import argparse
from pathlib import Path

from model.high_res_PV import load_cost_classes


def excluded_plants(cost_classes: dict[str, str], allowed: set[str]) -> list[str]:
    """Plants whose cost class is not allowed, sorted; raise if an allowed class is not in the dataset."""
    unknown = allowed - set(cost_classes.values())
    if unknown:
        raise ValueError(f"cost classes {sorted(unknown)} are not in the dataset, which has "
                         f"{sorted(set(cost_classes.values()))}")
    return sorted(plant for plant, cost_class in cost_classes.items() if cost_class not in allowed)


def write_presets(base: Path, plants: list[str], out: Path, comment: str) -> None:
    """Write a comment line, the base file's lines, then one gen_max row of 0 per plant."""
    lines = [f"# {comment}"] + base.read_text(encoding="utf-8").splitlines()
    lines += [f"gen_max,,{plant},,0.0" for plant in plants]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", type=Path, required=True, help="presets file whose rows are kept")
    parser.add_argument("--dataset", required=True, help="folder under input/RES_nexuse/PV/")
    parser.add_argument("--weather-year", type=int, required=True)
    parser.add_argument("--allow", nargs="+", required=True, help="cost classes that stay investable")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cost_classes = load_cost_classes(args.dataset, args.weather_year)
    if cost_classes is None:
        raise ValueError(f"dataset {args.dataset}/{args.weather_year} has no cost_class column in units.csv")
    plants = excluded_plants(cost_classes, set(args.allow))
    comment = (f"Written by data_prep/presets_pv_size_limit.py from {args.base.name}: gen_max fixed at 0 for the "
               f"{len(plants)} PV-roof candidates of {args.dataset}/{args.weather_year} outside {' '.join(args.allow)}.")
    write_presets(args.base, plants, args.out, comment)
    print(f"wrote {args.out}: {len(plants)} candidates fixed at 0")


if __name__ == "__main__":
    main()
