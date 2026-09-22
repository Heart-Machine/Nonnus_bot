"""Check that every input a workflow passes to an action is one that action declares.

GitHub does not fail a run over an input an action does not recognise: it emits
an "Unexpected input(s)" warning and carries on. That is how `script_stop: true`
sat in the deploy workflow doing nothing after appleboy/ssh-action removed it,
while the deploy kept reporting success - including on a deploy that had not
actually happened.

A pull request never runs the deploy workflow, so the actions used only there
get no pre-merge signal at all. This does not run them either - it reads the
`action.yml` each one declares at the exact SHA the workflow pins - but it
catches the class of breakage that actually bit us, and it needs no secrets,
which is what makes it safe to run on a pull request from anywhere.

What it does not catch: an action that still accepts an input but changes what
it does with it, and runtime incompatibilities such as a new Node version.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
API_ROOT = "https://api.github.com"


class ActionRef:
    """An `owner/repo[/subdir]@ref` reference from a workflow's `uses:`."""

    def __init__(self, owner: str, repo: str, subdir: str, ref: str) -> None:
        self.owner = owner
        self.repo = repo
        self.subdir = subdir
        self.ref = ref

    def __str__(self) -> str:
        path = f"/{self.subdir}" if self.subdir else ""
        return f"{self.owner}/{self.repo}{path}@{self.ref}"

    @property
    def manifest_paths(self) -> list[str]:
        prefix = f"{self.subdir}/" if self.subdir else ""
        return [f"{prefix}action.yml", f"{prefix}action.yaml"]


def parse_uses(uses: str) -> Optional[ActionRef]:
    """Local workflows and container actions have no action.yml to read here."""
    if uses.startswith((".", "/")) or uses.startswith("docker://"):
        return None

    reference, _, ref = uses.partition("@")
    if not ref:
        return None

    parts = reference.split("/")
    if len(parts) < 2:
        return None

    return ActionRef(parts[0], parts[1], "/".join(parts[2:]), ref)


def fetch(url: str) -> Optional[str]:
    headers = {
        "Accept": "application/vnd.github.raw",
        "User-Agent": "nonnus-bot-workflow-check",
    }
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def action_inputs(action: ActionRef) -> dict[str, Any]:
    """The inputs an action declares, read at the pinned ref."""
    for manifest_path in action.manifest_paths:
        url = f"{API_ROOT}/repos/{action.owner}/{action.repo}/contents/{manifest_path}?ref={action.ref}"
        content = fetch(url)
        if content is not None:
            manifest = yaml.safe_load(content) or {}
            return manifest.get("inputs") or {}

    raise RuntimeError(f"no action.yml or action.yaml found for {action}")


def workflow_steps(workflow: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and step.get("uses"):
                yield step


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    checked = 0

    for workflow_path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}

        for step in workflow_steps(workflow):
            action = parse_uses(step["uses"])
            if action is None:
                continue

            declared = action_inputs(action)
            checked += 1
            where = f"{workflow_path.name}: {step.get('name') or step['uses']}"

            for key in (step.get("with") or {}):
                if key not in declared:
                    problems.append(f"{where}\n    unknown input {key!r} for {action}")
                    continue

                deprecation = (declared[key] or {}).get("deprecationMessage")
                if deprecation:
                    notes.append(f"{where}\n    input {key!r} is deprecated: {deprecation}")

    for note in notes:
        print(f"warning: {note}")

    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        print(
            f"\n{len(problems)} input(s) are not declared by the action they are passed to. "
            "GitHub would ignore these with only a warning in the run annotations.",
            file=sys.stderr,
        )
        return 1

    print(f"checked {checked} action reference(s); every input is declared")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
