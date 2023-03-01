#!/bin/bash
python -m mf_prior_experiments.plot --experiment_group run --ext pdf --x_range 0 20 --plot_default --plot_optimum --filename run --algorithm hyperband asha successive_halving random_search  bohb  --benchmark mfh3_good_prior-bad  mfh6_good_prior-bad translatewmt_xformer_64_prior-bad lm1b_transformer_2048_prior-bad
