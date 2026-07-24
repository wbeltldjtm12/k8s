"""Controlled, time-limited CPU load for a local EviWatch experiment."""

from __future__ import annotations

import argparse
import multiprocessing as mp
import time


def burn_until(deadline: float) -> None:
    value = 1
    while time.monotonic() < deadline:
        value = (value * 1_103_515_245 + 12_345) % 2_147_483_647


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()

    deadline = time.monotonic() + args.seconds
    workers = [mp.Process(target=burn_until, args=(deadline,)) for _ in range(args.workers)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


if __name__ == "__main__":
    main()
