# 0024. KPI drift detector

Status: accepted (2026-11-06), milestone M4, track T4

## Context
README section 2 asks the controller to detect drift in pickup and engagement. The DID detector
(7.3) needs a comparator DID, so it is blind on a single-DID workflow, when every DID fails
together, and to drift with no DID or prompt cause. A raw before/after comparison is biased by
the hour mix, the attempt mix (later hours over-represent hard-to-reach leads), the cell mix, and
our own changes (a DID spread or a winning arm would read as drift).

## Decision
Per workflow and per KPI, a comparator-free observed-over-expected statistic per closed dialling
hour (block b, closed at the first stop after the hour ends):

```
pickup:      O_b = connects;  E_b = sum over b's dials of p_hist(cell, hour, attempt bucket)
             (history r_pickup, shrunk to the pooled (hour, bucket) rate with 200 dials, as 5.8)
             x the block's DID factor relative to the history DID mix (our DID states' r_d), so
             our own spreads and drains do not read as drift
engagement:  O_b = engaged;   E_b = connects x the history engagement rate of the live prompt,
             or the effect model's prediction when T3 injects it (so our arms do not read as drift)
z_b = (O_b - E_b) / sqrt(E_b (1 - rate_bar))
downward Page CUSUM S_b = max(0, S_{b-1} - z_b - k), k = 0.5, alarm at S_b >= h = 6
upward CUSUM with the same k, h for the view only ("doing better than expected")
armed after 2,000 baseline dials (pickup) or 1,000 baseline connects (engagement);
no history: the first 2 dialling days of our own data are the baseline
```

ARL0 by Siegmund (`b = h + 1.166`): about 2,570 blocks, about 214 dialling days; measured by a
seeded simulation of the production rule: mean in-control run length within 15% of that (at
least 2,000 blocks). Delays (blocks = dialling hours), table vs simulation within 20%: -10%
cancel pickup about 10 h, -20% about 3.8 h, -10% cancel engagement about 14 h, -10% KYC pickup
about 20 h. False alarms: dev, 60 KPI-days per run, about 0.28 per run; at 300 workflows about
2.8 a day across the fleet.

**An alarm never changes config.** It is a `KPI_DRIFT` digest item with the block evidence;
pickup drift makes every DID be re-tested and, with no comparator, marks the sole DID suspect and
starts probes (which create the comparator the DID detector needs); engagement drift checks the
flag CUSUM and pays for a FULL verdict on the incumbent only if that statistic is above h/2.

## Alternatives
- Shewhart limits on daily KPIs: one look a day, and a 10% pickup drop at KYC volume is a 1.2 SE
  daily event, so it would take days and alarm on noise when tightened.
- Raw before/after rates: biased by the 08:00 batch, attempt selection and DID load cycling.
- Changing config on a drift alarm: drift has no attributable cause by construction (that is
  what the other detectors are for), so acting on it would be acting without evidence of which
  lever moved.

## Consequences
- The detector is the comparator of last resort for caller IDs and the only pickup detector on a
  sole-DID workflow: about 2 days for a -10% drop at KYC volume, which is slow, and stated.
- Engagement expectations are only as good as the injected effect model; without it, our own
  winning arms can read as upward drift (view only) and a decaying arm as downward drift, which
  the decay monitor (T3) also sees.
- The blocks and the reflecting barrier are per dialling hour; `drift_monitor.last_block` stores
  the last processed absolute sim hour, so a restart resumes exactly.
