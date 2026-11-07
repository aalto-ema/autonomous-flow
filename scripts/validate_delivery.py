"""Summarize independently measured deliveries; never operates hardware.

CSV columns: source, workflow, rpm, requested_ml, commanded_ml,
             mass_before_g, mass_after_g, density_g_ml
"""

import argparse
import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


def summarize(path):
    groups = defaultdict(list)
    with Path(path).open(newline="") as handle:
        for index, row in enumerate(csv.DictReader(handle), 2):
            try:
                requested, commanded, before, after, density = (
                    float(row[k])
                    for k in (
                        "requested_ml",
                        "commanded_ml",
                        "mass_before_g",
                        "mass_after_g",
                        "density_g_ml",
                    )
                )
                if (
                    not all(
                        math.isfinite(x)
                        for x in (requested, commanded, before, after, density)
                    )
                    or min(requested, commanded, density) <= 0
                    or after < before
                ):
                    raise ValueError("Invalid mass, density or volume")
                measured = (after - before) / density
                groups[
                    (row["source"], row["workflow"], row["rpm"], requested, commanded)
                ].append(measured)
            except (KeyError, ValueError) as error:
                raise ValueError(f"CSV row {index}: {error}") from error
    if not groups:
        raise ValueError("No measurements found")
    for key, values in sorted(groups.items()):
        mean = statistics.mean(values)
        deviation = statistics.stdev(values) if len(values) > 1 else None
        yield dict(
            source=key[0],
            workflow=key[1],
            rpm=key[2],
            requested_ml=key[3],
            commanded_ml=key[4],
            n=len(values),
            mean_delivered_ml=mean,
            bias_vs_requested_percent=100 * (mean - key[3]) / key[3],
            bias_vs_commanded_percent=100 * (mean - key[4]) / key[4],
            cv_percent=100 * deviation / mean
            if deviation is not None and mean
            else None,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = list(summarize(args.csv))
    import sys

    handle = args.output.open("w", newline="") if args.output else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.output:
            handle.close()


if __name__ == "__main__":
    main()
