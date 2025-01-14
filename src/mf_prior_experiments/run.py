import contextlib
import logging
import os
import random
import sys
import time
from pathlib import Path
from typing import Any

import hydra
import numpy as np
import yaml
from gitinfo import gitinfo
from omegaconf import OmegaConf

import torch

logger = logging.getLogger("mf_prior_experiments.run")

# NOTE: If editing this, please look for MIN_SLEEP_TIME
# in `read_results.py` and change it there too
MIN_SLEEP_TIME = 10  # 10s hopefully is enough to simulate wait times for metahyper

# Use this environment variable to force overwrite when running
OVERWRITE = False  # bool(os.environ.get("MF_EXP_OVERWRITE", False))

print(f"{'='*50}\noverwrite={OVERWRITE}\n{'='*50}")


def _set_seeds(seed):
    random.seed(seed)  # important for NePS optimizers
    np.random.seed(seed)  # important for NePS optimizers
    torch.manual_seed(seed)
    # torch.backends.cudnn.benchmark = False
    # torch.backends.cudnn.deterministic = True
    # torch.manual_seed(seed)
    # tf.random.set_seed(seed)


def run_hpbandster(args):
    import uuid

    import ConfigSpace
    import hpbandster.core.nameserver as hpns
    import hpbandster.core.result as hpres
    from hpbandster.core.worker import Worker
    from hpbandster.optimizers.bohb import BOHB
    from hpbandster.optimizers.hyperband import HyperBand
    from mfpbench import Benchmark

    # Added the type here just for editors to be able to get a quick view
    benchmark: Benchmark = hydra.utils.instantiate(args.benchmark.api)

    def compute(**config: Any) -> dict:
        fidelity = config["budget"]
        config = config["config"]

        # transform to Ordinal HPs back
        for hp_name, hp in benchmark.space.items():
            if isinstance(hp, ConfigSpace.OrdinalHyperparameter):
                config[hp_name] = hp.sequence[config[hp_name] - 1]

        result = benchmark.query(config, at=int(fidelity))

        # This design only makes sense in the context of surrogate/tabular
        # benchmarks, where we do not actually need to run the model being
        # queried.
        max_fidelity_result = benchmark.query(config, at=benchmark.end)

        # we need to cast to float here as serpent will break on np.floating that might
        # come from a benchmark (LCBench)
        return {
            "loss": float(result.error),
            "cost": float(result.cost),
            "info": {
                "cost": float(result.cost),
                "val_score": float(result.val_score),
                "test_score": float(result.test_score),
                "fidelity": float(result.fidelity)
                if isinstance(result.fidelity, np.floating)
                else result.fidelity,
                "max_fidelity_loss": float(max_fidelity_result.error),
                "max_fidelity_cost": float(max_fidelity_result.cost),
                "process_id": os.getpid(),
                # val_error: result.val_error
                # test_error: result.test_error
            },
        }

    lower, upper, _ = benchmark.fidelity_range
    fidelity_name = benchmark.fidelity_name
    benchmark_configspace = benchmark.space

    # BOHB does not accept Ordinal HPs
    bohb_configspace = ConfigSpace.ConfigurationSpace(
        name=benchmark_configspace.name, seed=args.seed
    )

    for hp_name, hp in benchmark_configspace.items():
        if isinstance(hp, ConfigSpace.OrdinalHyperparameter):
            int_hp = ConfigSpace.UniformIntegerHyperparameter(
                hp_name, lower=1, upper=len(hp.sequence)
            )
            bohb_configspace.add_hyperparameters([int_hp])
        else:
            bohb_configspace.add_hyperparameters([hp])

    logger.info(f"Using configspace: \n {benchmark_configspace}")
    logger.info(f"Using fidelity: \n {fidelity_name} in {lower}-{upper}")

    max_evaluations_total = 10

    run_id = str(uuid.uuid4())
    NS = hpns.NameServer(
        run_id=run_id, port=0, working_directory="hpbandster_root_directory"
    )
    ns_host, ns_port = NS.start()

    hpbandster_worker = Worker(nameserver=ns_host, nameserver_port=ns_port, run_id=run_id)
    hpbandster_worker.compute = compute
    hpbandster_worker.run(background=True)

    result_logger = hpres.json_result_logger(
        directory="hpbandster_root_directory", overwrite=True
    )
    hpbandster_config = {
        "eta": 3,
        "min_budget": lower,
        "max_budget": upper,
        "run_id": run_id,
    }

    if "model" in args.algorithm and args.algorithm.model:
        hpbandster_cls = BOHB
    else:
        hpbandster_cls = HyperBand

    hpbandster_optimizer = hpbandster_cls(
        configspace=bohb_configspace,
        nameserver=ns_host,
        nameserver_port=ns_port,
        result_logger=result_logger,
        **hpbandster_config,
    )

    logger.info("Starting run...")
    res = hpbandster_optimizer.run(n_iterations=max_evaluations_total)

    hpbandster_optimizer.shutdown(shutdown_workers=True)
    NS.shutdown()

    id2config = res.get_id2config_mapping()
    logger.info(f"A total of {len(id2config.keys())} queries.")


def run_neps(args):
    from mfpbench import Benchmark

    import neps

    # Added the type here just for editors to be able to get a quick view
    benchmark: Benchmark = hydra.utils.instantiate(args.benchmark.api)  # type: ignore

    def run_pipeline(previous_pipeline_directory: Path, **config: Any) -> dict:
        start = time.time()
        if benchmark.fidelity_name in config:
            fidelity = config.pop(benchmark.fidelity_name)
        else:
            fidelity = benchmark.fidelity_range[1]

        result = benchmark.query(config, at=fidelity)

        # This design only makes sense in the context of surrogate/tabular
        # benchmarks, where we do not actually need to run the model being
        # queried.
        max_fidelity_result = benchmark.query(config, at=benchmark.end)

        # To account for continuations of previous configs in the parallel setting,
        # we use the `previous_pipeline_directory` which indicates if there has been
        # a previous lower fidelity evaluation of this config. If that's the case we
        # then subtract the previous fidelity off of this current one to compute
        # the `continuation_fidelity`. Otherwise, the `continuation_fidelity` is
        # just the current one. This is then used to make the worker sleep and
        # so we get a hueristically correct setup for each worker. In contrast,
        # if we do not do this, workers will not have even close to the correct
        # timestamps, and the order in which workers pick up new configurations to
        # evaluate may be in a very different order than if done in a real context.
        if args.n_workers == 1:
            # In the single worker setting, this does not matter and we can use
            # post-processing of the results to calculate the `continuation_fidelity`.
            continuation_fidelity = None
        else:
            # If there's no previous config, we sleep for `fidelity`.
            if previous_pipeline_directory is None:
                continuation_fidelity = None
                fidelity_sleep_time = fidelity

            # If there is a previous config, we calculate the `continuation_fidelity`
            # and sleep for that time instead
            else:
                previous_results_file = previous_pipeline_directory / "result.yaml"
                with previous_results_file.open("r") as f:
                    previous_results = yaml.load(f, Loader=yaml.FullLoader)

                # Calculate the continuation fidelity for sleeping
                current_fidelity = fidelity
                previous_fidelity = previous_results["info_dict"]["fidelity"]
                continuation_fidelity = current_fidelity - previous_fidelity

                logger.info("-"*30)
                logger.info(f"Continuing from: {previous_pipeline_directory}")
                logger.info(f"`continuation_fidelity`={continuation_fidelity}`")
                logger.info(f"{previous_results}")
                logger.info("-"*30)


                fidelity_sleep_time = continuation_fidelity

            time.sleep(fidelity_sleep_time + MIN_SLEEP_TIME)

        end = time.time()
        return {
            "loss": result.error,
            "cost": result.cost,
            "info_dict": {
                "cost": result.cost,
                "val_score": result.val_score,
                "test_score": result.test_score,
                "fidelity": result.fidelity,
                "continuation_fidelity": continuation_fidelity,
                "start_time": start,
                "end_time": end,  # + fidelity,
                "max_fidelity_loss": float(max_fidelity_result.error),
                "max_fidelity_cost": float(max_fidelity_result.cost),
                "process_id": os.getpid(),
                # val_error: result.val_error
                # test_error: result.test_error
            },
        }

    lower, upper, _ = benchmark.fidelity_range
    fidelity_name = benchmark.fidelity_name

    pipeline_space = {"search_space": benchmark.space}
    if args.algorithm.mf:
        if isinstance(lower, float):
            fidelity_param = neps.FloatParameter(
                lower=lower, upper=upper, is_fidelity=True
            )
        else:
            fidelity_param = neps.IntegerParameter(
                lower=lower, upper=upper, is_fidelity=True
            )
        pipeline_space = {**pipeline_space, **{fidelity_name: fidelity_param}}
        logger.info(f"Using fidelity space: \n {fidelity_param}")
    # pipeline_space = {"search_space": benchmark.space, fidelity_name: fidelity_param}
    logger.info(f"Using search space: \n {pipeline_space}")

    # TODO: could we pass budget per benchmark
    # if "budget" in args.benchmark:
    #     budget_args = {"budget": args.benchmark.budget}
    # else:
    #     budget_args = {"max_evaluations_total": 50}

    if "mf" in args.algorithm and args.algorithm.mf:
        max_evaluations_total = 1000
    else:
        max_evaluations_total = 100

    neps.run(
        run_pipeline=run_pipeline,
        pipeline_space=pipeline_space,
        root_directory="neps_root_directory",
        # TODO: figure out how to pass runtime budget and if metahyper internally
        #  calculates continuation costs to subtract from optimization budget
        # **budget_args,
        max_evaluations_total=max_evaluations_total,
        searcher=hydra.utils.instantiate(args.algorithm.searcher, _partial_=True),
        overwrite_working_directory=OVERWRITE,
    )

def run_botorch(args):
    from mfpbench import Benchmark

    from os import makedirs, chdir

    import yaml

    import torch
    from botorch import fit_gpytorch_model
    from botorch.optim.optimize import optimize_acqf
    from botorch.acquisition.fixed_feature import FixedFeatureAcquisitionFunction
    from botorch.models.gp_regression_fidelity import SingleTaskMultiFidelityGP
    from botorch.models.gp_regression import SingleTaskGP
    from gpytorch.mlls.exact_marginal_log_likelihood import ExactMarginalLogLikelihood
    from botorch.utils.sampling import draw_sobol_samples
    from botorch.models.transforms.outcome import Standardize
    from botorch.models.transforms.input import Normalize
    from ConfigSpace.hyperparameters import UniformIntegerHyperparameter

    from botorch.models.transforms.outcome import Standardize
    from botorch.utils.transforms import unnormalize
    from math import log,exp

    config_counter = 0

    def query_benchmark_and_log(x,train_obj, hyperparameter, benchmark):
        start = time.time()
        config = dict()
        for j in range(len(hyperparameter)):
            h = hyperparameter[j]
            if args.algorithm.mf:
                index_ = j+1
            else:
                index_ = j
            if isinstance(h, UniformIntegerHyperparameter):
                if h.log:
                    config[h.name] = int(exp(x[index_]))
                else:
                    config[h.name] = int(x[index_])
            else:
                if h.log:
                    config[h.name] = min(max(float(exp(x[index_])), h.lower), h.upper)
                else:
                    config[h.name] = min(max(float(x[index_]), h.lower), h.upper)
                # breakpoint()
        if args.algorithm.mf:
            result = benchmark.query(config, at=int(x[0]))
        else:
            result = benchmark.query(config, at=benchmark.end)
        max_fidelity_result = benchmark.query(config, at=benchmark.end)
        train_obj = torch.cat([train_obj, torch.tensor([result.error])])
        end = time.time()
        folder = f'config_{config_counter}_0'
        makedirs(folder, exist_ok=True)
        info_dict = {
            "loss": result.error,
            "cost": result.cost,
            "info_dict": {
                "cost": result.cost,
                "val_score": result.val_score,
                "test_score": result.test_score,
                "fidelity": result.fidelity,
                "continuation_fidelity": None,
                "start_time": start,
                "end_time": end,  # + fidelity,
                "max_fidelity_loss": float(max_fidelity_result.error),
                "max_fidelity_cost": float(max_fidelity_result.cost),
                "process_id": os.getpid(),
                },
        }
        with open(folder + "/result.yaml", "w+") as outfile:
            yaml.dump(info_dict, outfile)
        return result.cost, train_obj, result.fidelity


    makedirs("neps_root_directory/results", exist_ok = True)
    chdir("neps_root_directory/results")

    benchmark: Benchmark = hydra.utils.instantiate(args.benchmark.api)  # type: ignore
    pipeline_space =  benchmark.space
    fidelity_min, fidelity_max, _ = benchmark.fidelity_range
    hyperparameter = pipeline_space.get_hyperparameters()
    x = hyperparameter[0]
    cost_total = 0.0
    fidelity_total = 0.0
    lowers = list()
    uppers = list()
    if args.algorithm.mf:
        lowers.append(fidelity_min)
        uppers.append(fidelity_max)
    for i in hyperparameter:
        if i.log:
            lowers.append(log(i.lower))
            uppers.append(log(i.upper))
        else:
            lowers.append(i.lower)
            uppers.append(i.upper)
    bounds = torch.tensor([lowers, uppers])


    # initialize model
    INITIAL_DESIGN_SIZE=8
    train_x = draw_sobol_samples(bounds, 1, INITIAL_DESIGN_SIZE).squeeze()
    train_obj = torch.Tensor()
    for i in range(INITIAL_DESIGN_SIZE):
        cost, train_obj, fidelity_current = query_benchmark_and_log(train_x[i], train_obj, hyperparameter, benchmark)
        cost_total = cost_total + cost
        fidelity_total = fidelity_total + fidelity_current
        config_counter = config_counter + 1

    # train model
    # train_obj = train_obj.reshape(-1,1)
    while fidelity_total/fidelity_max  <100:
        if args.algorithm.mf:
            model = SingleTaskMultiFidelityGP(
                train_x,
                train_obj.unsqueeze(1),
                outcome_transform=Standardize(m=1),
                input_transform=Normalize(len(bounds[0]), bounds=bounds),
                iteration_fidelity=0
            )
        else:
            model = SingleTaskGP(
                train_x,
                train_obj.unsqueeze(1),
                outcome_transform=Standardize(m=1),
                input_transform=Normalize(len(bounds[0]), bounds=bounds),
            )
        mll = ExactMarginalLogLikelihood(model.likelihood, model)
        fit_gpytorch_model(mll)
        candidate_set = draw_sobol_samples(bounds, 1,1024).squeeze()
        if "jes" in args.algorithm.name:
            num_optima = 8
            posterior = model.posterior(candidate_set)
            posterior_samples = posterior.rsample(sample_shape=torch.Size([num_optima])).squeeze()
            f_star, optimal_indices = torch.max(posterior_samples, dim=1)
            f_star = f_star.unsqueeze(1)
            X_star = candidate_set[optimal_indices]
            # optimal_out = train_obj.argmin()
            # optimal_in = train_x[optimal_out]
            breakpoint()
            acquisition_function = hydra.utils.instantiate(args.algorithm.searcher, model= model, maximize = False, optimal_outputs=f_star, optimal_inputs=X_star, num_samples=num_optima)
        else:
            acquisition_function = hydra.utils.instantiate(args.algorithm.searcher, model= model, candidate_set = candidate_set, maximize = False)
        candidate, _ = optimize_acqf(acquisition_function, bounds, 1, num_restarts=20, raw_samples=1024)
        candidate = candidate.detach()
        train_x = torch.cat([train_x, candidate])
        candidate= candidate.squeeze()
        cost, train_obj, fidelity_current = query_benchmark_and_log(candidate, train_obj, hyperparameter, benchmark)
        config_counter = config_counter + 1
        cost_total = cost_total + cost
        fidelity_total = fidelity_total + fidelity_current
        print(cost_total, fidelity_total / fidelity_max)

    # breakpoint()



    # result = benchmark.query(config, at=fidelity)

    # max_fidelity_result = benchmark.query(config, at=benchmark.end)

    # end = time.time()
    # info_dict = {
    #     "loss": result.error,
    #     "cost": result.cost,
    #     "info_dict": {
    #         "cost": result.cost,
    #         "val_score": result.val_score,
    #         "test_score": result.test_score,
    #         "fidelity": result.fidelity,
    #         "continuation_fidelity": None,
    #         "start_time": start,
    #         "end_time": end,  # + fidelity,
    #         "max_fidelity_loss": float(max_fidelity_result.error),
    #         "max_fidelity_cost": float(max_fidelity_result.cost),
    #         "process_id": os.getpid(),
    #         },
    # }

    lower, upper, _ = benchmark.fidelity_range
    fidelity_name = benchmark.fidelity_name

    if args.algorithm.mf:
        pass
    # breakpoint()

@hydra.main(config_path="configs", config_name="run", version_base="1.2")
def run(args):
    # Print arguments to stderr (useful on cluster)
    sys.stderr.write(f"{' '.join(sys.argv)}\n")
    sys.stderr.write(f"args = {args}\n\n")
    sys.stderr.flush()

    _set_seeds(args.seed)
    working_directory = Path().cwd()

    # Log general information
    logger.info(f"Using working_directory={working_directory}")
    with contextlib.suppress(TypeError):
        git_info = gitinfo.get_git_info()
        logger.info(f"Commit hash: {git_info['commit']}")
        logger.info(f"Commit date: {git_info['author_date']}")
    logger.info(f"Arguments:\n{OmegaConf.to_yaml(args)}")

    # Actually run
    hydra.utils.call(args.algorithm.run_function, args)
    logger.info("Run finished")


if __name__ == "__main__":
    run()  # pylint: disable=no-value-for-parameter
