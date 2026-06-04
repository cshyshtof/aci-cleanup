.DEFAULT_GOAL := help

SCRIPT := aci-cleanup.py
VCENTER_SCRIPT := cleanup-vcenter-aci.py
GITCONFIG := .gitconfig

.PHONY: help init run run-aci run-aci-check run-vcenter run-vcenter-check format check clean
.PHONY: git-init sync push wip

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*?## "; printf "\nUsage: make \033[36m<target>\033[0m\n\n"} \
		/^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}' \
		$(MAKEFILE_LIST)
	@echo ""

init: ## Create virtual environment and install dependencies
	uv sync

run: ## Run ACI and vCenter cleanup
	make run-vcenter
	make run-aci

run-check: ## Run ACI and vCenter cleanup check
	make run-vcenter-check
	make run-aci-check

run-aci: ## Run ACI cleanup (requires ACI_HOST, ACI_USER, ACI_PASS)
	uv run $(SCRIPT)

run-aci-check: ## Run ACI cleanup check (requires ACI_HOST, ACI_USER, ACI_PASS)
	uv run $(SCRIPT) --check

run-vcenter: ## Run APIC/vCenter DVS cleanup
	uv run $(VCENTER_SCRIPT)

run-vcenter-check: ## Run APIC/vCenter DVS cleanup connectivity check
	uv run $(VCENTER_SCRIPT) --check

format: ## Format code with ruff (modifies file)
	uv run ruff format $(SCRIPT) $(VCENTER_SCRIPT)

check: ## Verify linting and formatting without modifying files
	uv run ruff check $(SCRIPT) $(VCENTER_SCRIPT)
	uv run ruff format --check $(SCRIPT) $(VCENTER_SCRIPT)

clean: ## Remove virtual environment and cache directories
	rm -rf .venv .ruff_cache __pycache__

git-init: ## Initialize local directory and set local email
	echo "Initializing local Git repository..."
	@if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then \
		echo "Git repository is already initialized."; \
	else \
		git init; \
	fi
	@if email=$$(git config --local --get user.email); [ -n "$$email" ]; then \
		echo "Local Git user.email is already set to $$email"; \
	else \
		read -p "Enter local Git user.email: " email; \
		git config --local user.email "$$email"; \
	fi
	@echo "Setting up Git configuration..."
	@if [ -f $(GITCONFIG) ]; then \
		echo "Including local Git configuration..."; \
		git config --local include.path $(shell pwd)/$(GITCONFIG); \
	fi

sync: ## Sync local repository with remote repository
	@if git remote get-url origin >/dev/null 2>&1; then \
		echo "Fetching latest changes from origin/main..."; \
		git fetch origin main; \
		echo "Rebasing onto origin/main..."; \
		git rebase origin/main; \
	else \
		echo "No remote 'origin' configured — skipping fetch/pull."; \
	fi
	@echo "Main branch is up to date."

push: ## Push local repository to remote repository
	@if git remote get-url origin >/dev/null 2>&1; then \
		echo "Pushing main to origin..."; \
		git push -u origin main; \
		echo "Push completed successfully."; \
	else \
		echo "No remote 'origin' configured — skipping push."; \
	fi

wip: ## Create a WIP commit
	@echo "Creating WIP commit..."
	@git add -A
	@if git diff --cached --quiet; then \
		echo "Nothing to commit, working tree clean."; \
	else \
		read -p "Enter WIP commit message: " message; \
		git commit -m "WIP: $$message"; \
	fi
