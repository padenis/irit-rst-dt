# Author: Eric Kow
# License: CeCILL-B (French BSD3-like)

"""
run an experiment
"""

from __future__ import print_function
from os import path as fp
from collections import namedtuple
from itertools import chain
import argparse
import itertools
import json
import os
import shutil
import sys

from joblib import Parallel

from attelo.args import args_to_decoder
from attelo.io import load_data_pack
from attelo.harness.config import CliArgs
from attelo.harness.report import (mk_index)
from attelo.harness.util import\
    timestamp, call, force_symlink
import attelo.cmd as att

from ..local import (EVALUATIONS,
                     TRAINING_CORPORA,
                     ATTELO_CONFIG_FILE)
from ..util import latest_tmp

NAME = 'evaluate'
_DEBUG = 0


# pylint: disable=pointless-string-statement
LoopConfig = namedtuple("LoopConfig",
                        ["eval_dir",
                         "scratch_dir",
                         "folds",
                         "fold_file",
                         "report_only",
                         "dataset"])
"that which is common to outerish loops"


DataConfig = namedtuple("DataConfig",
                        "pack folds")
"data tables we have read"
# pylint: enable=pointless-string-statement

# ---------------------------------------------------------------------
# user feedback
# ---------------------------------------------------------------------


def _exit_ungathered():
    """
    You don't seem to have run the gather command
    """
    sys.exit("""No data to run experiments on.
Please run `irit-rst-dt gather`""")


def _eval_banner(econf, lconf, fold):
    """
    Which combo of eval parameters are we running now?
    """
    return "\n".join(["----------" * 3,
                      "fold %d [%s]" % (fold, lconf.dataset),
                      "learner(s): %s" % econf.learner.key,
                      "decoder: %s" % econf.decoder.key,
                      "----------" * 3])


def _corpus_banner(lconf):
    "banner to announce the corpus"
    return "\n".join(["==========" * 7,
                      lconf.dataset,
                      "==========" * 7])


def _fold_banner(lconf, fold):
    "banner to announce the next fold"
    return "\n".join(["==========" * 6,
                      "fold %d [%s]" % (fold, lconf.dataset),
                      "==========" * 6])

# ---------------------------------------------------------------------
# attelo config
# ---------------------------------------------------------------------


# pylint: disable=too-many-instance-attributes, too-few-public-methods
class FakeEvalArgs(CliArgs):
    """
    Fake argparse object (to be subclassed)
    Things in common between attelo learn/decode

    Note: must be used as a context manager
    """
    def __init__(self, lconf, econf, fold):
        self.lconf = lconf
        self.econf = econf
        self.fold = fold
        super(FakeEvalArgs, self).__init__()

    def parser(self):
        """
        The argparser that would be called on context manager
        entry
        """
        psr = argparse.ArgumentParser()
        att.enfold.config_argparser(psr)

    def argv(self):
        """
        Command line arguments that would correspond to this
        configuration

        :rtype: `[String]`
        """
        econf = self.econf
        lconf = self.lconf
        fold = self.fold

        model_file_a = _eval_model_path(lconf, econf, fold, "attach")
        model_file_r = _eval_model_path(lconf, econf, fold, "relate")

        argv = [_edu_input_path(lconf),
                _pairings_path(lconf),
                _features_path(lconf),
                "--config", ATTELO_CONFIG_FILE,
                "--fold", str(fold),
                "--fold-file", lconf.fold_file,
                "--attachment-model", model_file_a,
                "--relation-model", model_file_r]

        return argv

    # pylint: disable=no-member
    def __exit__(self, ctype, value, traceback):
        "Tidy up any open file handles, etc"
        self.fold_file.close()
        super(FakeEvalArgs, self).__exit__(ctype, value, traceback)
    # pylint: enable=no-member


class FakeEnfoldArgs(CliArgs):
    """
    Fake argparse object that would be generated by attelo enfold
    """
    def __init__(self, lconf):
        self.lconf = lconf
        super(FakeEnfoldArgs, self).__init__()

    def parser(self):
        psr = argparse.ArgumentParser()
        att.enfold.config_argparser(psr)
        return psr

    def argv(self):
        """
        Command line arguments that would correspond to this
        configuration

        :rtype: `[String]`
        """
        lconf = self.lconf
        args = [_edu_input_path(lconf),
                _pairings_path(lconf),
                _features_path(lconf),
                "--config", ATTELO_CONFIG_FILE,
                "--output", lconf.fold_file]
        return args

    # pylint: disable=no-member
    def __exit__(self, ctype, value, traceback):
        "Tidy up any open file handles, etc"
        self.output.close()
        super(FakeEnfoldArgs, self).__exit__(ctype, value, traceback)
    # pylint: enable=no-member


class FakeLearnArgs(FakeEvalArgs):
    """
    Fake argparse object that would be generated by attelo learn.
    """
    def __init__(self, lconf, econf, fold):
        super(FakeLearnArgs, self).__init__(lconf, econf, fold)

    def parser(self):
        psr = argparse.ArgumentParser()
        att.learn.config_argparser(psr)
        return psr

    def argv(self):
        econf = self.econf
        args = super(FakeLearnArgs, self).argv()
        args.extend(["--learner", econf.learner.attach.name])
        args.extend(econf.learner.attach.flags)
        if econf.learner.relate is not None:
            args.extend(["--relation-learner", econf.learner.relate.name])
            # yuck: we assume that learner and relation learner flags
            # are compatible
            args.extend(econf.learner.relate.flags)
        if econf.decoder is not None:
            args.extend(["--decoder", econf.decoder.name])
            args.extend(econf.decoder.flags)
        return args


class FakeDecodeArgs(FakeEvalArgs):
    """
    Fake argparse object that would be generated by attelo decode
    """
    def __init__(self, lconf, econf, fold):
        super(FakeDecodeArgs, self).__init__(lconf, econf, fold)

    def parser(self):
        psr = argparse.ArgumentParser()
        att.decode.config_argparser(psr)
        return psr

    def argv(self):
        lconf = self.lconf
        econf = self.econf
        fold = self.fold
        args = super(FakeDecodeArgs, self).argv()
        args.extend(["--decoder", econf.decoder.name,
                     "--output", _decode_output_path(lconf, econf, fold)])
        args.extend(econf.decoder.flags)
        return args


class FakeReportArgs(CliArgs):
    "args for attelo report"
    def __init__(self, lconf, fold):
        self.lconf = lconf
        self.fold = fold
        super(FakeReportArgs, self).__init__()

    def parser(self):
        """
        The argparser that would be called on context manager
        entry
        """
        psr = argparse.ArgumentParser()
        att.report.config_argparser(psr)
        return psr

    def argv(self):
        """
        Command line arguments that would correspond to this
        configuration

        :rtype: `[String]`
        """
        lconf = self.lconf
        if self.fold is None:
            parent_dir = lconf.scratch_dir
        else:
            parent_dir = _fold_dir_path(lconf, self.fold)
        argv = [_edu_input_path(lconf),
                _pairings_path(lconf),
                _features_path(lconf),
                "--index", fp.join(parent_dir, 'index.json'),
                "--config", ATTELO_CONFIG_FILE,
                "--fold-file", lconf.fold_file,
                "--output", _report_dir(parent_dir, lconf)]
        return argv

    # pylint: disable=no-member
    def __exit__(self, ctype, value, traceback):
        "Tidy up any open file handles, etc"
        self.fold_file.close()
        super(FakeReportArgs, self).__exit__(ctype, value, traceback)
    # pylint: enable=no-member
# pylint: enable=too-many-instance-attributes, too-few-public-methods


# ---------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------

def _link_data_files(data_dir, eval_dir):
    """
    Hard-link all files from the data dir into the evaluation
    directory. This does not cost space and it makes future
    archiving a bit more straightforward
    """
    for fname in os.listdir(data_dir):
        data_file = os.path.join(data_dir, fname)
        eval_file = os.path.join(eval_dir, fname)
        if os.path.isfile(data_file):
            os.link(data_file, eval_file)


def _eval_data_path(lconf, ext):
    """
    Path to data file in the evaluation dir
    """
    return os.path.join(lconf.eval_dir,
                        "%s.%s" % (lconf.dataset, ext))


def _features_path(lconf, stripped=False):
    """
    Path to the feature file in the evaluation dir
    """
    ext = 'relations.sparse'
    if stripped:
        ext += '.stripped'
    return _eval_data_path(lconf, ext)


def _edu_input_path(lconf):
    """
    Path to the feature file in the evaluation dir
    """
    return _features_path(lconf) + '.edu_input'


def _pairings_path(lconf):
    """
    Path to the pairings file in the evaluation dir
    """
    return _features_path(lconf) + '.pairings'


def _fold_dir_basename(fold):
    "Relative directory for working within a given fold"
    return "fold-%d" % fold


def _fold_dir_path(lconf, fold):
    "Scratch directory for working within a given fold"
    return os.path.join(lconf.scratch_dir,
                        _fold_dir_basename(fold))


def _eval_model_path(lconf, econf, fold, mtype):
    "Model for a given loop/eval config and fold"
    lname = econf.learner.key
    fold_dir = _fold_dir_path(lconf, fold)
    return os.path.join(fold_dir,
                        "%s.%s.%s.model" % (lconf.dataset, lname, mtype))


def _decode_output_basename(econf):
    "Model for a given loop/eval config and fold"
    return ".".join(["output", econf.key])


def _decode_output_path(lconf, econf, fold):
    "Model for a given loop/eval config and fold"
    fold_dir = _fold_dir_path(lconf, fold)
    return os.path.join(fold_dir, _decode_output_basename(econf))


def _report_dir(parent_dir, lconf):
    """
    Path to a score file given a parent dir.
    You'll need to tack an extension onto this
    """
    return fp.join(parent_dir, "reports-%s" % lconf.dataset)


def _delayed_learn(lconf, dconf, econf, fold):
    """
    Return possible futures for learning models for this
    fold
    """
    fold_dir = _fold_dir_path(lconf, fold)
    if not os.path.exists(fold_dir):
        os.makedirs(fold_dir)

    with FakeLearnArgs(lconf, econf, fold) as args:
        if fp.exists(args.attachment_model) and fp.exists(args.relation_model):
            print("reusing %s model (already built)" % econf.learner.key,
                  file=sys.stderr)
            return []
        subpack = dconf.pack.training(dconf.folds, fold)
        return att.learn.delayed_main_for_harness(args, subpack)


def _delayed_decode(lconf, dconf, econf, fold):
    """
    Return possible futures for decoding groups within
    this model/decoder combo for the given fold
    """
    if fp.exists(_decode_output_path(lconf, econf, fold)):
        print("skipping %s/%s (already done)" % (econf.learner.key,
                                                 econf.decoder.key),
              file=sys.stderr)
        return []

    fold_dir = _fold_dir_path(lconf, fold)
    if not os.path.exists(fold_dir):
        os.makedirs(fold_dir)
    with FakeDecodeArgs(lconf, econf, fold) as args:
        decoder = args_to_decoder(args)
        subpack = dconf.pack.testing(dconf.folds, fold)
        models = att.decode.load_models(args)
        return att.decode.delayed_main_for_harness(args, decoder,
                                                   subpack, models)


def _post_decode(lconf, dconf, econf, fold):
    """
    Join together output files from this model/decoder combo
    """
    print(_eval_banner(econf, lconf, fold), file=sys.stderr)
    with FakeDecodeArgs(lconf, econf, fold) as args:
        subpack = dconf.pack.testing(dconf.folds, fold)
        return att.decode.concatenate_outputs(args, subpack)


def _generate_fold_file(lconf, dpack):
    """
    Generate the folds file
    """
    with FakeEnfoldArgs(lconf) as args:
        att.enfold.main_for_harness(args, dpack)


def _mk_report(args, index, dconf):
    "helper for report generation"
    with open(args.index, 'w') as ostream:
        json.dump(index, ostream)
    att.report.main_for_harness(args, dconf.pack, args.output)


def _mk_fold_report(lconf, dconf, fold):
    "Generate reports for scores"
    configurations = [(econf, _decode_output_basename(econf))
                      for econf in EVALUATIONS]
    index = mk_index([(fold, '.')], configurations)
    with FakeReportArgs(lconf, fold) as args:
        _mk_report(args, index, dconf)


def _mk_global_report(lconf, dconf):
    "Generate reports for all folds"
    folds = [(f, _fold_dir_basename(f))
             for f in frozenset(dconf.folds.values())]
    configurations = [(econf, _decode_output_basename(econf))
                      for econf in EVALUATIONS]
    index = mk_index(folds, configurations)
    with FakeReportArgs(lconf, None) as args:
        _mk_report(args, index, dconf)
        final_report_dir = fp.join(lconf.eval_dir,
                                   fp.basename(args.output))
        shutil.copytree(args.output, final_report_dir)
        print('Report saved in ', final_report_dir,
              file=sys.stderr)


def _do_fold(lconf, dconf, fold):
    """
    Run all learner/decoder combos within this fold
    """
    fold_dir = _fold_dir_path(lconf, fold)
    print(_fold_banner(lconf, fold), file=sys.stderr)
    if not os.path.exists(fold_dir):
        os.makedirs(fold_dir)

    # learn all models in parallel
    learner_confs = [list(g)[0] for _, g in
                     itertools.groupby(EVALUATIONS, key=lambda x: x.learner)]
    learner_jobs = chain.from_iterable(_delayed_learn(lconf, dconf, econf,
                                                      fold)
                                       for econf in learner_confs)
    Parallel(n_jobs=-1, verbose=5)(learner_jobs)
    # run all model/decoder joblets in parallel
    decoder_jobs = chain.from_iterable(_delayed_decode(lconf, dconf, econf,
                                                       fold)
                                       for econf in EVALUATIONS)
    Parallel(n_jobs=-1, verbose=5)(decoder_jobs)
    for econf in EVALUATIONS:
        _post_decode(lconf, dconf, econf, fold)
    fold_dir = _fold_dir_path(lconf, fold)
    _mk_fold_report(lconf, dconf, fold)


def _do_corpus(lconf):
    "Run evaluation on a corpus"
    print(_corpus_banner(lconf), file=sys.stderr)

    edus_file = _edu_input_path(lconf)
    if not os.path.exists(edus_file):
        _exit_ungathered()

    has_stripped = (lconf.report_only
                    and fp.exists(_features_path(lconf, stripped=True)))
    dpack = load_data_pack(edus_file,
                           _pairings_path(lconf),
                           _features_path(lconf, stripped=has_stripped),
                           verbose=True)

    _generate_fold_file(lconf, dpack)

    with open(lconf.fold_file) as f_in:
        dconf = DataConfig(pack=dpack,
                           folds=json.load(f_in))

    if not lconf.report_only:
        foldset = lconf.folds if lconf.folds is not None\
            else frozenset(dconf.folds.values())
        for fold in foldset:
            _do_fold(lconf, dconf, fold)

    # only generate report if we're not in the middle of cluster mode
    if lconf.folds is None:
        _mk_global_report(lconf, dconf)

# ---------------------------------------------------------------------
# main
# ---------------------------------------------------------------------


def config_argparser(psr):
    """
    Subcommand flags.

    You should create and pass in the subparser to which the flags
    are to be added.
    """
    psr.set_defaults(func=main)
    psr.add_argument("--start", action='store_true',
                     default=False,
                     help="initialise an evaluation but don't run it "
                     "(cluster mode)")
    psr.add_argument("--folds", metavar='N', type=int, nargs='+',
                     help="run only these folds (cluster mode)")
    psr.add_argument("--end", action='store_true',
                     default=False,
                     help="generate report only (cluster mode)")
    psr.add_argument("--resume",
                     default=False, action="store_true",
                     help="resume previous interrupted evaluation")


def _create_eval_dirs(args, data_dir):
    """
    Return eval and scatch directory paths
    """

    eval_current = fp.join(data_dir, "eval-current")
    scratch_current = fp.join(data_dir, "scratch-current")

    if args.resume or args.end or args.folds is not None:
        if not fp.exists(eval_current) or not fp.exists(scratch_current):
            sys.exit("No currently running evaluation to resume!")
        else:
            return eval_current, scratch_current
    else:
        tstamp = "TEST" if _DEBUG else timestamp()
        eval_dir = fp.join(data_dir, "eval-" + tstamp)
        if not fp.exists(eval_dir):
            os.makedirs(eval_dir)
            _link_data_files(data_dir, eval_dir)
            force_symlink(fp.basename(eval_dir), eval_current)
        elif not _DEBUG:
            sys.exit("Try again in literally one second")

        scratch_dir = fp.join(data_dir, "scratch-" + tstamp)
        if not fp.exists(scratch_dir):
            os.makedirs(scratch_dir)
            force_symlink(fp.basename(scratch_dir), scratch_current)

        return eval_dir, scratch_dir


def main(args):
    """
    Subcommand main.

    You shouldn't need to call this yourself if you're using
    `config_argparser`
    """
    sys.setrecursionlimit(10000)
    data_dir = latest_tmp()
    if not os.path.exists(data_dir):
        _exit_ungathered()
    eval_dir, scratch_dir = _create_eval_dirs(args, data_dir)

    with open(os.path.join(eval_dir, "versions.txt"), "w") as stream:
        call(["pip", "freeze"], stdout=stream)

    if args.start:
        # all done! just wanted to create the directory
        return

    for corpus in TRAINING_CORPORA:
        dataset = os.path.basename(corpus)
        fold_file = os.path.join(eval_dir,
                                 "folds-%s.json" % dataset)

        lconf = LoopConfig(eval_dir=eval_dir,
                           scratch_dir=scratch_dir,
                           folds=args.folds,
                           fold_file=fold_file,
                           report_only=bool(args.end),
                           dataset=dataset)
        _do_corpus(lconf)
