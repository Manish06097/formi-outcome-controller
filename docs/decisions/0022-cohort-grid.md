# 0022. Cohort grid

Status: accepted (2026-11-06), milestone M4, track T4

## Context
The simulator allows at most 20 cohorts, first match wins, AND across features and OR within a
list. There are about 108 to 252 feature cells, so cohort design is compression. Every module
(experiments, detectors, schedule, dry runs, the supervisor view) and every call event must name
the same cohort, and Product asked for "one shared cohort object". The default config of a hidden
scenario may already define cohorts, mixed allocations or hour tables. History ran on a flat
schedule under the default config, so it shows per-feature hour-of-day pickup curves but no
variant effects (unless the business mixed variants).

## Decision
1. **Cells**: one per observed (lang, tier, age, value), a dictionary built from lead events
   (an unseen language just adds cells). Every rollup is keyed by cell.
2. **Business cohorts first**: kept verbatim, in order, at the front, supervisor-owned (never
   redefined, reordered or deleted; their tables and allocations change only by an approval that
   names them). Our grid takes only the cells they leave, and at most `20 - n_business` cohorts.
3. **F1** = the feature whose levels have the most different hour curves: deviance of the
   additive logistic model `connected ~ hour + f` against the saturated `hour x f` table, per
   degree of freedom, on first-attempt history rows, required p < 0.001 (chi-square tail). It is
   chosen for the schedule, the lever that history can actually inform. No significant feature:
   no F1, and the grid is F2 alone.
4. **F2** = `lang` if 2 to 5 languages each hold at least 10% of leads (a stated prior: welcome
   and voice are language bound), else the remaining feature with the most balanced mass
   (highest normalised entropy).
5. **Minimum sizes, two roles**: a targeting cohort holds at least 5% of daily leads (about 200
   history dials per window hour, the hour-curve shrinkage strength); a winner scope needs at
   least 15% of daily connected calls AND an arm projected to reach the n = 800 look before the
   freeze along the real ramp (5.5). Cells below 5% merge with the F1 level whose hour curve is
   closest inside the same F2 level; a whole F2 level below 5% moves to an `other` bucket per F1
   level (placed last); buckets still below 5% fall to `default`. Above the cap, the smallest F2
   levels fold into the buckets until it fits.
6. **Identity**: `cohort_key = slug + "~" + sha1(workflow_id | canonical where | order index)[:6]`
   (for example `age60p_ta~3fa2c1`), and that key IS the `id` written into the config, so
   `cohort_id` on every call event is our key. A changed definition is a new key. A supervisor's
   `cohorts` edit is adopted as a new set version (known cohorts keep their origin, new ones are
   supervisor-owned). Regrid is cut.

"Closest neighbour" replaces the design's unspecified "F1 neighbour": F1 levels have no natural
order for every feature (value bands sort as high, low, mid), and the merged cohort's job is to
carry one hour table, so merging the most similar curves loses the least schedule value.

## Alternatives
- One cohort per cell: 108+ cells against a 20 cap, and most cells too small for an hour curve.
- Cluster cells on hour curves directly: cohorts would be unreadable in the supervisor view and
  in an approval summary ("Compliance: worse for a cohort of our customers" needs a nameable
  cohort), and the clusters would not be language bound for welcome and voice.
- A regrid when effects appear: cut; the effect model estimates off-grid heterogeneity through
  its feature terms and acts inside existing cohorts.

## Consequences
- On a synthetic world where age drives hour curves the grid picks F1 = age, F2 = lang and builds
  about 10 cohorts, each at least 5% of leads (unit-tested), and ranks young cohorts' evening
  hours up, which is what the schedule needs.
- A 5% cohort never hosts a cohort winner inside a 10-day dev run; small-cohort effects act only
  through allocation and population winners.
- The winner-scope ramp shares (0.038, 0.12, 0.25, 0.30, 0.40, 0.50 per day) are copied from 5.5
  as a module constant; the T4 report asks for them as a setting.
