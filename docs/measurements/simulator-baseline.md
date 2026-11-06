# Simulator-only cost (no controller), measured 2026-11-06

Machine: Apple Silicon Mac (dev laptop), Python 3.11. Command: `python -m sim baseline` (no HTTP, daily advances).

| Scenario | Sim days (history + run) | Events | Wall clock | Peak RSS |
|---|---|---|---|---|
| public_01_dev | 14 + 8 | 297k | 2.6 s | 317 MB |
| public_02_incidents | 14 + 10 | 324k | 3.1 s | 307 MB |
| public_03_load | 7 + 5 | 9.74M | 63.1 s | 805 MB |

public_03 on disk: history 2.3 GB in 23 chunks, run events 1.8 GB in 20 chunks (about 370 MB and 770k events per sim day).

Implications:
- The simulator itself needs about 5 s of CPU per public_03 sim day, so it is not the bottleneck for
  SRE's 10-minute-per-day target. Our ingest path is: about 770k events and 370 MB of NDJSON per day.
- Serving over HTTP adds per-advance overhead; with 15-minute ticks a day is 96 advances.
- The simulator holds about 0.8 GB, leaving roughly 7 GB of the 8 GB box for Postgres and the controller.
- Ingesting history means about 5.9M events (2.3 GB) before D0: plan for that in startup time.
