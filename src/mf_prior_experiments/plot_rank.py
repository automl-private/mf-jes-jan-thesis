import argparse
import errno
import os
import time
from multiprocessing import Manager
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from attrdict import AttrDict
from joblib import Parallel, delayed, parallel_backend
from scipy import stats

from .configs.plotting.read_results import get_seed_info
from .configs.plotting.styles import ALGORITHMS, COLOR_MARKER_DICT, X_LABEL
from .configs.plotting.utils import interpolate_time, save_fig, set_general_plot_style

benchmark_configs_path = os.path.join(os.path.dirname(__file__), "configs/benchmark/")


def _process_seed(
    _path, seed, algorithm, key_to_extract, cost_as_runtime, results, n_workers
):
    print(
        f"[{time.strftime('%H:%M:%S', time.localtime())}] "
        f"[-] [{algorithm}] Processing seed {seed}..."
    )
    try:
        # `algorithm` is passed to calculate continuation costs
        losses, infos, max_cost = get_seed_info(
            _path,
            seed,
            algorithm=algorithm,
            cost_as_runtime=cost_as_runtime,
            n_workers=n_workers,
        )
        incumbent = np.minimum.accumulate(losses)
        cost = [i[key_to_extract] for i in infos]
        results["incumbents"].append(incumbent)
        results["costs"].append(cost)
        results["max_costs"].append(max_cost)
    except Exception as e:
        print(repr(e))
        print(f"Seed {seed} did not work from {_path}/{algorithm}")


def plot(args):

    starttime = time.time()

    BASE_PATH = (
        Path(__file__).parent / "../.."
        if args.base_path is None
        else Path(args.base_path)
    )

    KEY_TO_EXTRACT = "cost" if args.cost_as_runtime else "fidelity"

    set_general_plot_style()

    ncols = 1
    nrows = 1
    ncol = len(args.algorithms) // 2

    bbox_to_anchor = (0.5, -0.32)
    figsize = (4 * ncols, 3 * nrows)

    fig, axs = plt.subplots(
        nrows=nrows,
        ncols=ncols,
        figsize=figsize,
    )

    base_path = BASE_PATH / "results" / args.experiment_group
    output_dir = BASE_PATH / "plots" / args.experiment_group
    print(
        f"[{time.strftime('%H:%M:%S', time.localtime())}]"
        f" Processing {len(args.benchmarks)} benchmarks "
        f"and {len(args.algorithms)} algorithms..."
    )

    all_results = dict()
    all_indexes = list()
    for benchmark_idx, benchmark in enumerate(args.benchmarks):
        print(
            f"[{time.strftime('%H:%M:%S', time.localtime())}] "
            f"[{benchmark_idx}] Processing {benchmark} benchmark..."
        )

        # loading the benchmark yaml
        _bench_spec_path = (
            BASE_PATH
            / "src"
            / "mf_prior_experiments"
            / "configs"
            / "benchmark"
            / f"{benchmark}.yaml"
        )

        _base_path = os.path.join(base_path, f"benchmark={benchmark}")
        if not os.path.isdir(_base_path):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), _base_path)

        for algorithm in args.algorithms:
            _path = os.path.join(_base_path, f"algorithm={algorithm}")
            if not os.path.isdir(_path):
                raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), _path)

            algorithm_starttime = time.time()
            seeds = sorted(os.listdir(_path))

            if args.parallel:
                manager = Manager()
                results = manager.dict(
                    incumbents=manager.list(),
                    costs=manager.list(),
                    max_costs=manager.list(),
                )
                with parallel_backend(args.parallel_backend, n_jobs=-1):
                    Parallel()(
                        delayed(_process_seed)(
                            _path,
                            seed,
                            algorithm,
                            KEY_TO_EXTRACT,
                            args.cost_as_runtime,
                            results,
                            args.n_workers,
                        )
                        for seed in seeds
                    )

            else:
                results = dict(incumbents=[], costs=[], max_costs=[])
                # pylint: disable=expression-not-assigned
                [
                    _process_seed(
                        _path,
                        seed,
                        algorithm,
                        KEY_TO_EXTRACT,
                        args.cost_as_runtime,
                        results,
                        args.n_workers,
                    )
                    for seed in seeds
                ]

            print(f"Time to process algorithm data: {time.time() - algorithm_starttime}")

            x = results["costs"][:]
            y = results["incumbents"][:]
            max_cost = None if args.cost_as_runtime else max(results["max_costs"][:])

            if isinstance(x, list):
                x = np.array(x)
            if isinstance(y, list):
                y = np.array(y)

            df = interpolate_time(
                incumbents=y, costs=x, x_range=args.x_range, scale_x=max_cost
            )

            import pandas as pd

            x_max = np.inf if args.x_range is None else int(args.x_range[-1])
            new_entry = {c: np.nan for c in df.columns}
            _df = pd.DataFrame.from_dict(new_entry, orient="index").T
            _df.index = [x_max]
            df = pd.concat((df, _df)).sort_index()
            df = df.fillna(method="backfill", axis=0).fillna(method="ffill", axis=0)

            if benchmark not in all_results:
                all_results[benchmark] = dict()
            all_results[benchmark][algorithm] = df
            all_indexes.extend(df.index.to_list())

    # NEW PART
    # same axis for all results
    all_indexes = sorted(set(all_indexes))
    all_columns = [list(v.keys()) for v in all_results.values()][0]
    initial_mean_rank = (len(all_columns) + 1) / 2

    new_results = dict()
    for benchmark, algorithms_results in all_results.items():
        if benchmark not in new_results:
            new_results[benchmark] = dict()
        for algorithm, seed_results in algorithms_results.items():

            _results = seed_results.loc[~seed_results.index.duplicated(), :]
            _results = _results.reindex(all_indexes)
            _results = _results.fillna(method="backfill", axis=0).fillna(
                method="ffill", axis=0
            )

            _results_dict = _results.to_dict()
            for _seed, _result in _results_dict.items():
                if _seed not in new_results[benchmark]:
                    new_results[benchmark][_seed] = dict()
                if algorithm not in new_results[benchmark][_seed]:
                    new_results[benchmark][_seed][algorithm] = dict()
                new_results[benchmark][_seed][algorithm] = _result

    ranks = []
    for benchmark, benchmark_results in new_results.items():
        dfs = []
        for _, results in benchmark_results.items():
            df = pd.DataFrame.from_dict(results).rank(axis=1, ascending=False)
            # df.index = [0]
            df.loc[df.index == 0] = initial_mean_rank
            dfs.append(df.to_numpy())
        ranks.append(np.average(dfs, axis=0))
    final_ranks = np.average(ranks, axis=0)
    final_stds = stats.sem(ranks, axis=0)

    for i, algorithm in enumerate(all_columns):
        axs.step(
            all_indexes,
            final_ranks.T[i],
            label=ALGORITHMS[algorithm],
            color=COLOR_MARKER_DICT[algorithm],
            linestyle="-" if "prior" in algorithm else "-",
            linewidth=1,
            where="post",
        )
        axs.fill_between(
            all_indexes,
            final_ranks.T[i] - final_stds.T[i],
            final_ranks.T[i] + final_stds.T[i],
            color=COLOR_MARKER_DICT[algorithm],
            alpha=0.1,
            step="post",
        )

    axs.set_ylim(auto=True)
    axs.set_xlabel(X_LABEL[args.cost_as_runtime], fontsize=18, color=(0, 0, 0, 0.69))
    axs.set_ylabel("Relative rank", fontsize=18, color=(0, 0, 0, 0.69))
    # Black with some alpha
    axs.tick_params(axis="both", which="major", labelsize=18, labelcolor=(0, 0, 0, 0.69))
    axs.grid(True, which="both", ls="-", alpha=0.8)
    sns.despine(fig)

    handles, labels = axs.get_legend_handles_labels()

    leg = fig.legend(
        handles,
        labels,
        fontsize="xx-large",
        loc="lower center",
        bbox_to_anchor=bbox_to_anchor,
        ncol=ncol,
        frameon=True,
    )

    for legend_item in leg.legendHandles:
        legend_item.set_linewidth(2.0)

    fig.tight_layout(pad=0, h_pad=0.5)

    filename = args.filename
    if filename is None:
        filename = f"{args.experiment_group}_rank_{args.plot_id}"
    save_fig(
        fig,
        filename=filename,
        output_dir=output_dir,
        extension=args.ext,
        dpi=args.dpi,
    )

    print(f"Plotting took {time.time() - starttime}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="mf-prior-exp plotting",
    )
    parser.add_argument(
        "--base_path", type=str, default=None, help="path where `results/` exists"
    )
    parser.add_argument("--experiment_group", type=str, default="")
    parser.add_argument(
        "--n_workers",
        type=int,
        default=1,
        help="for multiple workers we plot based on end timestamps on "
        "x-axis (no continuation considered); any value > 1 is adequate",
    )
    parser.add_argument("--benchmarks", nargs="+", default=None)
    parser.add_argument("--algorithms", nargs="+", default=None)
    parser.add_argument("--plot_id", type=str, default="1")
    parser.add_argument("--research_question", type=int, default=1)
    parser.add_argument(
        "--which_prior",
        type=str,
        choices=["good", "bad"],
        default="bad",
        help="for RQ2 choose whether to plot good or bad",
    )
    parser.add_argument("--x_range", nargs="+", default=None, type=float)
    parser.add_argument("--log_x", action="store_true")
    parser.add_argument("--log_y", action="store_true")
    parser.add_argument(
        "--filename", type=str, default=None, help="name out pdf file generated"
    )
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument(
        "--ext",
        type=str,
        choices=["pdf", "png"],
        default="pdf",
        help="the file extension or the plot file type",
    )
    parser.add_argument(
        "--cost_as_runtime",
        default=False,
        action="store_true",
        help="Default behaviour to use fidelities on the x-axis. "
        "This parameter uses the training cost/runtime on the x-axis",
    )
    parser.add_argument(
        "--parallel",
        default=False,
        action="store_true",
        help="whether to process data in parallel or not",
    )
    parser.add_argument(
        "--parallel_backend",
        type=str,
        choices=["multiprocessing", "threading"],
        default="multiprocessing",
        help="which backend use for parallel",
    )

    args = AttrDict(parser.parse_args().__dict__)

    if args.x_range is not None:
        assert len(args.x_range) == 2

    # budget = None
    # # reading benchmark budget if only one benchmark is being plotted
    # if len(args.benchmarks) == 1:
    #     with open(
    #         os.path.join(benchmark_configs_path, f"{args.benchmarks[0]}.yaml"),
    #         encoding="utf-8",
    #     ) as f:
    #         _args = AttrDict(yaml.load(f, yaml.Loader))
    #         if "budget" in _args:
    #             budget = _args.budget
    # # TODO: make log scaling of plots also a feature of the benchmark
    # args.update({"budget": budget})
    plot(args)  # pylint: disable=no-value-for-parameter