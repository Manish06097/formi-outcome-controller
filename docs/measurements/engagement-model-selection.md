# Engagement model selection (DESIGN.md 17.2, ADR 0003)

Raw table, step I4. Command:

    .venv/bin/python scripts/variants/model_selection.py --out runs/i4/select -j 2 --port 8741 \
        --dsn "host=127.0.0.1 port=55481 user=postgres password=ctl dbname=postgres" \
        --write docs/measurements/engagement-model-selection.md

Each cell is the real controller (`CTL_ENGAGEMENT_MODEL` set to the model) against a served simulator,
graded by `sim.grader` against the no-op baseline of the same scenario. 13 scenarios: public_01,
public_02 and 11 private variants (seeds, effect sizes and signs, resize, sign-flip, decaying winner at
two volumes). Wall seconds are with 2 cells in parallel on a shared machine.

Decision rule (model_selection.py docstring): ridge-logit becomes the default only if it captures more
gap summed over engagement-primary workflows, has no more harmful engagement changes, and none on any
scenario. Result: it captures less gap (2.8% mean against 6.0%) and ties on harm (0 and 0). The rule
keeps the Beta-binomial. The default was not changed in code; the lead decides and updates ADR 0003.

| scenario | model | engagement gap captured per engagement-primary workflow | harmful engagement | harmful all | changes | wall s |
|---|---|---|---|---|---|---|
| public_01 | beta_binomial | ks_cancel +8.7%, nw_renewal +8.4% | 0 | 0 | 20 | 153.8 |
| public_01 | ridge_logit | ks_cancel +10.2%, nw_renewal +1.1% | 0 | 0 | 15 | 242.1 |
| public_02 | beta_binomial | ks_cancel +12.3%, nw_renewal -14.2% | 0 | 0 | 20 | 208.5 |
| public_02 | ridge_logit | ks_cancel +7.3%, nw_renewal -1.2% | 0 | 0 | 18 | 278.3 |
| seed_p01_s1101 | beta_binomial | ks_cancel +8.2%, nw_renewal +14.3% | 0 | 0 | 22 | 145.9 |
| seed_p01_s1101 | ridge_logit | ks_cancel +2.7%, nw_renewal +1.9% | 0 | 0 | 13 | 185.5 |
| seed_p01_s1102 | beta_binomial | ks_cancel +15.6%, nw_renewal +9.4% | 0 | 0 | 21 | 134.4 |
| seed_p01_s1102 | ridge_logit | ks_cancel +1.1%, nw_renewal +1.4% | 0 | 0 | 11 | 199.7 |
| seed_p02_s2201 | beta_binomial | ks_cancel +5.8%, nw_renewal -16.4% | 0 | 0 | 21 | 221.1 |
| seed_p02_s2201 | ridge_logit | ks_cancel +0.8%, nw_renewal -5.4% | 0 | 0 | 17 | 310.9 |
| seed_p02_s2202 | beta_binomial | ks_cancel +8.4%, nw_renewal -6.3% | 0 | 0 | 22 | 199.2 |
| seed_p02_s2202 | ridge_logit | ks_cancel +7.5%, nw_renewal -1.6% | 0 | 0 | 17 | 342.0 |
| effects_flipped_p01 | beta_binomial | ks_cancel +7.2%, nw_renewal -8.9% | 0 | 0 | 16 | 142.2 |
| effects_flipped_p01 | ridge_logit | ks_cancel +1.4%, nw_renewal -0.6% | 0 | 0 | 13 | 211.8 |
| effects_small_p01 | beta_binomial | ks_cancel +1.4%, nw_renewal +8.5% | 0 | 0 | 23 | 152.7 |
| effects_small_p01 | ridge_logit | ks_cancel -0.3%, nw_renewal +1.3% | 0 | 0 | 10 | 199.1 |
| effects_large_p01 | beta_binomial | ks_cancel +18.0%, nw_renewal +13.3% | 0 | 0 | 25 | 154.5 |
| effects_large_p01 | ridge_logit | ks_cancel +2.2%, nw_renewal +10.6% | 0 | 0 | 20 | 278.5 |
| effects_flip_resize_p02 | beta_binomial | ks_cancel -7.2%, nw_renewal +21.8% | 0 | 0 | 25 | 174.1 |
| effects_flip_resize_p02 | ridge_logit | ks_cancel -2.1%, nw_renewal +1.8% | 0 | 0 | 13 | 274.2 |
| last_listed_signflip_p01 | beta_binomial | ks_cancel +5.4%, nw_renewal +6.2% | 0 | 0 | 24 | 152.2 |
| last_listed_signflip_p01 | ridge_logit | ks_cancel +1.8%, nw_renewal +5.3% | 0 | 0 | 24 | 255.3 |
| decaying_winner_dev_p01 | beta_binomial | ks_cancel +6.2%, nw_renewal +8.3% | 0 | 0 | 16 | 151.4 |
| decaying_winner_dev_p01 | ridge_logit | ks_cancel +5.6%, nw_renewal +3.2% | 0 | 0 | 18 | 249.8 |
| decaying_winner_highvol_p01 | beta_binomial | ks_cancel +7.4%, nw_renewal +13.6% | 0 | 0 | 22 | 182.6 |
| decaying_winner_highvol_p01 | ridge_logit | ks_cancel +13.7%, nw_renewal +2.9% | 0 | 0 | 21 | 251.4 |

| model | scenarios with a result | mean gap captured | workflows at or below 0 | harmful engagement, total | scenarios with any |
|---|---|---|---|---|---|
| beta_binomial | 13 | +6.0% | 5 of 26 | 0 | 0 |
| ridge_logit | 13 | +2.8% | 6 of 26 | 0 | 0 |
