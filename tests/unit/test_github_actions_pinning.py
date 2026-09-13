import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"
EXTERNAL_ACTION_PATTERN = re.compile(
    r"^\s*uses:\s*(?!\./)(?P<action>[^\s@]+)@(?P<reference>[^\s#]+)",
    re.MULTILINE,
)
FULL_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PRODUCTION_WORKFLOW_PATH = WORKFLOWS_DIRECTORY / "prod-docker-release.yml"


def test_external_github_actions_are_pinned_to_full_commit_shas() -> None:
    unpinned_actions: list[str] = []

    for workflow_path in sorted(WORKFLOWS_DIRECTORY.glob("*.yml")):
        workflow = workflow_path.read_text(encoding="utf-8")
        for match in EXTERNAL_ACTION_PATTERN.finditer(workflow):
            reference = match.group("reference")
            if FULL_COMMIT_SHA_PATTERN.fullmatch(reference) is None:
                unpinned_actions.append(
                    f"{workflow_path.name}: {match.group('action')}@{reference}"
                )

    assert unpinned_actions == [], (
        "External GitHub Actions must use immutable 40-character commit SHAs: "
        + ", ".join(unpinned_actions)
    )


def test_production_release_uses_one_exact_release_tag_source() -> None:
    workflow = PRODUCTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "ref: main" not in workflow
    assert "github.ref_name" not in workflow
    assert "release_tag:" in workflow
    assert "required: true" in workflow
    assert (
        "RELEASE_TAG: ${{ github.event_name == 'release' "
        "&& github.event.release.tag_name || inputs.release_tag }}"
    ) in workflow
    assert "ref: ${{ env.RELEASE_TAG }}" in workflow
    assert "tag_name: ${{ env.RELEASE_TAG }}" in workflow
    assert "BUILD_BRANCH=${{ env.RELEASE_TAG }}" in workflow
    assert "BUILD_TAG=${{ env.RELEASE_TAG }}" in workflow
    assert "contents: write" in workflow
    assert "packages: write" in workflow
    assert "actions: read" in workflow
    assert "cancel-in-progress: false" in workflow


def test_production_release_requires_quality_for_exact_tag_commit_before_push() -> None:
    workflow = PRODUCTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    gate = workflow.index("Require successful Quality run for the exact release commit")
    push = workflow.index("Build and Push Docker Image")
    assert gate < push
    assert 'TAG_SHA=$(git rev-parse --verify "refs/tags/${RELEASE_TAG}^{commit}")' in workflow
    assert 'test "${RELEASE_SHA}" = "${TAG_SHA}"' in workflow
    assert "quality.yml/runs?head_sha=${RELEASE_SHA}&status=success" in workflow


def test_production_release_publishes_fork_image_and_deployment_digest() -> None:
    workflow = PRODUCTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'IMAGE_REPOSITORY="${GITHUB_REPOSITORY,,}"' in workflow
    assert "ghcr.io/snoups/remnashop" not in workflow
    assert "tags: ${{ steps.vars.outputs.image_tags }}" in workflow
    assert "id: build" in workflow
    assert "REMNASHOP_IMAGE_DIGEST=${IMAGE_DIGEST}" in workflow
    assert "files: remnashop-release.env" in workflow


def test_production_release_cannot_move_latest_back_to_an_older_tag() -> None:
    workflow = PRODUCTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "fetch-depth: 0" in workflow
    assert "Release tag must be a Docker-safe semantic version" in workflow
    assert "LATEST_STABLE_TAG=$(git tag --list --sort=-v:refname" in workflow
    assert '[ "${RELEASE_TAG}" = "${LATEST_STABLE_TAG}" ]' in workflow
    assert 'IMAGE_TAGS="${IMAGE_REPO}:latest,${IMAGE_REPO}:${RELEASE_TAG}"' not in workflow


def test_production_release_marks_semantic_version_suffix_as_prerelease() -> None:
    workflow = PRODUCTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'if [[ "${RELEASE_TAG}" == *-* ]]; then' in workflow
    assert "IS_PRERELEASE=true" in workflow
    assert 'echo "prerelease=${IS_PRERELEASE}" >> "$GITHUB_OUTPUT"' in workflow
    assert "prerelease: ${{ steps.vars.outputs.prerelease }}" in workflow
    assert "prerelease: false" not in workflow
