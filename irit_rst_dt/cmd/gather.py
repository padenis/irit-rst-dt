# Author: Eric Kow
# License: CeCILL-B (French BSD3-like)

"""
gather features
"""

from __future__ import print_function
import os

from attelo.harness.util import call, force_symlink

from ..local import (TRAINING_CORPUS, PTB_DIR, FEATURE_SET)
from ..util import\
    current_tmp, latest_tmp

NAME = 'gather'


def config_argparser(parser):
    """
    Subcommand flags.

    You should create and pass in the subparser to which the flags
    are to be added.
    """
    parser.set_defaults(func=main)


def main(_):
    """
    Subcommand main.

    You shouldn't need to call this yourself if you're using
    `config_argparser`
    """
    tdir = current_tmp()
    call(["rst-dt-learning", "extract", TRAINING_CORPUS, PTB_DIR, tdir,
          '--feature_set', FEATURE_SET])
    with open(os.path.join(tdir, "versions-gather.txt"), "w") as stream:
        call(["pip", "freeze"], stdout=stream)
    latest_dir = latest_tmp()
    force_symlink(os.path.basename(tdir), latest_dir)
