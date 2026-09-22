"""Tests for the `uses:` parsing in tools/check_action_inputs.py.

The network-facing half of that script is exercised by CI running it for real
against the workflows; what is worth pinning down here is the reference
parsing, which decides what gets checked and what is skipped.
"""
import pytest

from tools.check_action_inputs import parse_uses


def test_parses_a_plain_action_reference():
    action = parse_uses("actions/checkout@11d5960a326750d5838078e36cf38b85af677262")

    assert (action.owner, action.repo) == ("actions", "checkout")
    assert action.ref == "11d5960a326750d5838078e36cf38b85af677262"
    assert action.subdir == ""
    assert action.manifest_paths == ["action.yml", "action.yaml"]


def test_parses_an_action_in_a_subdirectory():
    action = parse_uses("owner/repo/sub/dir@v1")

    assert (action.owner, action.repo, action.subdir) == ("owner", "repo", "sub/dir")
    assert action.manifest_paths == ["sub/dir/action.yml", "sub/dir/action.yaml"]


def test_reference_renders_back_readably():
    assert str(parse_uses("appleboy/ssh-action@v1.2.5")) == "appleboy/ssh-action@v1.2.5"
    assert str(parse_uses("owner/repo/sub@v1")) == "owner/repo/sub@v1"


@pytest.mark.parametrize(
    "uses",
    [
        "./.github/workflows/ci.yml",  # a reusable workflow in this repo
        "docker://alpine:3",  # a container action, no manifest to read
        "actions/checkout",  # no ref at all
        "checkout@v4",  # not owner/repo
    ],
)
def test_skips_references_with_no_manifest_to_read(uses):
    assert parse_uses(uses) is None
