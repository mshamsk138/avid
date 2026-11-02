#!/usr/bin/env python3
"""Print a synthetic, reproducible mid-price path for an avid-engine simulation scenario.

    scripts/make-sim-prices.py --start 60000 --steps 96 --sigma 0.003 --seed 20260105

Geometric random walk with Gaussian steps from random.Random(seed): the same arguments always give
the same numbers. The output is SYNTHETIC test data, not market data.
"""
import argparse
import json
import math
import random


def path(start, steps, sigma, seed, drift=0.0):
    rng = random.Random(seed)
    p, out = float(start), []
    for _ in range(steps):
        out.append(round(p, 2))
        p *= math.exp(drift + rng.gauss(0.0, sigma))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--start', type=float, required=True)
    ap.add_argument('--steps', type=int, required=True)
    ap.add_argument('--sigma', type=float, default=0.003)
    ap.add_argument('--drift', type=float, default=0.0)
    ap.add_argument('--seed', type=int, required=True)
    a = ap.parse_args(argv)
    print(json.dumps(path(a.start, a.steps, a.sigma, a.seed, a.drift)))


if __name__ == '__main__':
    main()
