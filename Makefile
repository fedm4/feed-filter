-include .env
export

.PHONY: check pr-review pr-incremental pr-describe pr-improve pr-ask pr-review-local

check:
	uv run --frozen ruff check .
	uv run --frozen ruff format --check .
	uv run --frozen pytest

pr-review:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" review

pr-incremental:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" review --pr_reviewer.incremental=true

pr-describe:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" describe

pr-improve:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" improve

pr-ask:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" ask "$(Q)"

pr-review-local:
	uv run pr-agent --pr_url "$(REPO_PULL_URL)$(PR)" review --config.publish_output=false
