#!/bin/bash
for seed in {1..3}; do
    for benchmark in mfh3_good_prior-bad  mfh6_good_prior-bad translatewmt_xformer_64_prior-bad lm1b_transformer_2048_prior-bad   ; do
        for algorithm in hyperband asha random_search successive_halving bohb ; do
           echo $seed $benchmark $algorithm
           HYDRA_FULL_ERROR=1 python -m mf_prior_experiments.run algorithm=$algorithm benchmark=$benchmark experiment_group=run seed=$seed n_workers=1
        done
    done
done
