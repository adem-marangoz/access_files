from __future__ import annotations

import pytest

from tpms_agent.agent import build_parser


@pytest.mark.parametrize(
    "argv, command, config, verbose",
    [
        ([], None, None, False),
        (["-c", "a.yaml"], None, "a.yaml", False),
        (["-c", "a.yaml", "run"], "run", "a.yaml", False),
        (["run", "-c", "b.yaml", "-v"], "run", "b.yaml", True),
        (["-v", "report", "-p", "X", "--config", "c.yaml"], "report", "c.yaml", True),
    ],
)
def test_common_options_before_or_after_subcommand(argv, command, config, verbose):
    args = build_parser().parse_args(argv)
    assert args.command == command
    assert getattr(args, "config", None) == config
    assert getattr(args, "verbose", False) is verbose
