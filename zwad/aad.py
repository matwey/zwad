#!/usr/bin/env python

import argparse
import click
import numpy as np
import pandas as pd
import sys
import webbrowser

from coniferest.aadforest import AADForest, AADForestAnomalyDetector
from coniferest.datasets import Label
from coniferest.pineforest import PineForest, PineForestAnomalyDetector

from zwad.ad.transformation import transform_features as transform_lc_features
from zwad.utils import load_data


class ResultsSink:
    def __init__(self, anomalies_filename, answers_filename = None):
        self._anomalies = None
        self._answers = None

        try:
            self._anomalies = open(anomalies_filename, mode="w", encoding="utf-8")

            self._answers = open(answers_filename, mode="w", encoding="utf-8") if answers_filename is not None else None
            if self._answers is not None:
                self._answers.write("oid,is_anomaly\n")
        except:
            self._cleanup()

    def _cleanup(self):
        if self._anomalies is not None:
            self._anomalies.close()
        if self._answers is not None:
            self._answers.close()

    def _handle_anomaly(self, name, decision):
        if not decision:
            return

        self._anomalies.write("{}\n".format(name))
        self._anomalies.flush()

    def _handle_answer(self, name, decision):
        if self._answers is None:
            return

        self._answers.write("{},{:b}\n".format(name, decision))
        self._answers.flush()

    def __call__(self, *args, **kwargs):
        self._handle_anomaly(*args, **kwargs)
        self._handle_answer(*args, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        self._cleanup()

        return False

def load_answers(answers):
    def load_single(filename):
        df = pd.read_csv(filename, dtype={"oid": np.uint64, "is_anomaly": bool})

        return dict(zip(df["oid"], df["is_anomaly"]))

    res = {}
    for f in answers:
        res.update(load_single(f))

    return res

class ConiferestEngine:
    def __init__(self, anomaly_detector, *, budget, non_interactive):
        self._anomaly_detector = anomaly_detector
        self._budget = budget
        self._non_interactive = non_interactive

        try:
            self._browser = webbrowser.get()
        except webbrowser.Error:
            self._browser = None

    def _handle_initial_knowns(self, oid, feature, answers):
        if answers is None or len(answers) == 0:
            return {}

        known_indices = np.where(np.isin(oid, list(answers.keys())))[0]
        known_data = feature[known_indices]
        known_labels = np.full(known_data.shape[0], Label.ANOMALY)
        known_labels[np.asarray(answers.values()) == 0] = Label.REGULAR

        self._anomaly_detector.observe(known_data, known_labels)

        return dict(zip(np.atleast_1d(known_indices), np.atleast_1d(known_labels)))

    def _evaluate(self, name):
        url = "https://ztf.snad.space/dr4/view/{}".format(name)
        if self._browser is not None:
            self._browser.open_new_tab(url)
        else:
            click.echo("Check {} for details".format(url))

        result = click.confirm("Is {} anomaly?".format(name))
        label = Label.ANOMALY if result else Label.REGULAR

        return result, label

    def _run_interactive(self, oid, feature, known):
        budget = min(self._budget, feature.shape[0])

        for _ in range(budget):
            scores = self._anomaly_detector.score(feature)
            scores_idx = np.argsort(scores)
            a = next(filter(lambda idx: (idx not in known), scores_idx))
            a_oid = oid[a]

            result, label = self._evaluate(a_oid)

            self._anomaly_detector.observe(np.atleast_1d(feature[a]), np.atleast_1d(label))
            known[a] = label

            yield (a_oid, result)

    def _run_non_interactive(self, oid, feature):
        budget = min(self._budget, feature.shape[0])

        scores = self._anomaly_detector.score(feature)
        scores_idx = np.argsort(scores)

        for i in range(budget):
            a_oid = oid[scores_idx[i]]

            yield (a_oid, True)

    def run(self, oid, feature, answers):
        self._anomaly_detector.train(feature)

        known = self._handle_initial_knowns(oid, feature, answers)

        if self._non_interactive:
            return self._run_non_interactive(oid, feature)

        return self._run_interactive(oid, feature, known)


class AADEngine(ConiferestEngine):
    def __init__(self, aadforest, *args, **kwargs):
        anomaly_detector = AADForestAnomalyDetector(aadforest)
        super().__init__(anomaly_detector, *args, **kwargs)

    @classmethod
    def from_args(cls, args):
        aadforest = AADForest(
            n_trees      = args.n_trees,
            n_subsamples = args.n_subsamples,
            max_depth    = args.max_depth,
            tau          = args.tau,
            random_seed  = args.random_seed)
        return cls(
            aadforest,
            budget = args.budget,
            non_interactive = args.non_interactive)

class PineForestEngine(ConiferestEngine):
    def __init__(self, pineforest, *args, **kwargs):
        anomaly_detector = PineForestAnomalyDetector(pineforest)
        super().__init__(anomaly_detector, *args, **kwargs)

    @classmethod
    def from_args(cls, args):
        pineforest = PineForest(
            n_trees          = args.n_trees,
            n_subsamples     = args.n_subsamples,
            max_depth        = args.max_depth,
            n_spare_trees    = args.n_spare_trees,
            regenerate_trees = args.regenerate_trees,
            weight_ratio     = args.weight_ratio,
            random_seed      = args.random_seed)
        return cls(
            pineforest,
            budget = args.budget,
            non_interactive = args.non_interactive)

def make_argument_parser():
    parser = argparse.ArgumentParser(description='Active anomaly detection for ZTF data')
    parser.add_argument('--prior_answers', help='Name of the file with the prior expert decisions. The answers are shown to the algorithm but not repeated in output files specified by --answers and --anomalies arguments. May be repeated.', action='append', default=[])
    parser.add_argument('--answers', help='Name of the file to store expert answers. May be used later as the input for --prior_answers argument.')
    parser.add_argument('--anomalies', help='Name of the file to store found anomalies. When run in non-interactive mode all found anomalies within the budget are stored, when run in interactive mode all confirmed anomalies are stored.', required=True)
    parser.add_argument('--oid', help='Name of the file with object IDs. May be repeated.', required=True, action='append')
    parser.add_argument('--feature', help='Name of the file with corresponding features. May be repeated.', required=True, action='append')
    parser.add_argument('--feature-names', help='Name of the file with feature names, one name per line')
    parser.add_argument('--transform', help='Data transformation using nonlinear functions', action='store_true')
    parser.add_argument('-n', '--non-interactive', help='Run in non-interactive mode.', action='store_true', default=False)
    parser.add_argument('--budget', help='Number of data samples to examine.', default=40)
    parser.add_argument('-s', '--random_seed', default=42, type=int, help='Fix the seed for reproducibility. Defaults to 42.')

    subparsers = parser.add_subparsers(required=True, metavar='ALGO')

    parser_aad = subparsers.add_parser('aad')
    parser_aad.add_argument('--n_trees',
        type=int,
        help='Number of trees to keep for estimating anomaly scores.',
        default=100)
    parser_aad.add_argument('--n_subsamples',
        type=int,
        help='How many subsamples should be used to build every tree.',
        default=256)
    parser_aad.add_argument('--max_depth',
        type=int,
        help='Maximum depth of every tree.',
        default=None)
    parser_aad.add_argument('--tau',
        type=float,
        help='The AAD tau quantile.',
        default=0.97)
    parser_aad.set_defaults(cls=AADEngine)

    parser_pineforest = subparsers.add_parser('pineforest')
    parser_pineforest.add_argument('--n_trees',
        type=int,
        help='Number of trees to keep for estimating anomaly scores.',
        default=100)
    parser_pineforest.add_argument('--n_subsamples',
        type=int,
        help='How many subsamples should be used to build every tree.',
        default=256)
    parser_pineforest.add_argument('--max_depth',
        type=int,
        help='Maximum depth of every tree.',
        default=None)
    parser_pineforest.add_argument('--n_spare_trees',
        type=int,
        help='Number of trees to generate additionally for further filtering.',
        default=400)
    parser_pineforest.add_argument('--regenerate_trees',
        action='store_true',
        help='Should we through out all the trees during retraining.',
        default=False)
    parser_pineforest.add_argument('--weight_ratio',
        type=float,
        help='Relative weight of false positives relative to true positives.',
        default=1.0)
    parser_pineforest.set_defaults(cls=PineForestEngine)

    return parser

def parse_arguments(argv):
    parser = make_argument_parser()
    args = parser.parse_args(argv)

    if args.non_interactive and not args.prior_answers:
        raise ValueError("--prior_answers must be supplied when run with --non-interactive")

    if args.non_interactive and args.answers is not None:
        raise ValueError("--answers has no sense when run with --non-interactive")

    if args.transform and args.feature_names is None:
        raise ValueError('--feature-names must be specified when --transform is enabled')

    return args

def main(argv=None):
    args = parse_arguments(argv)

    engine = args.cls.from_args(args)

    answers = load_answers(args.prior_answers)
    oids, features = load_data(args.oid, args.feature)

    if args.transform:
        with open(args.feature_names) as fh:
            feature_names = fh.read().split()
        transform_lc_features(features, feature_names)

    with ResultsSink(args.anomalies, args.answers) as sink:
        for (oid, decision) in engine.run(oids, features, answers):
            sink(oid, decision)

def execute_from_commandline(argv=None):
    main(argv)

if __name__ == "__main__":
    main()
