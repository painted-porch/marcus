# Contributing to Marcus

Welcome to the Marcus contributor guide! This page provides an overview of how to contribute to Marcus, with links to detailed guides for specific topics.

```{note}
This is the Sphinx documentation version of our contributing guide. For the complete markdown version, see [CONTRIBUTING.md](https://github.com/painted-porch/marcus/blob/develop/CONTRIBUTING.md) in the repository root.
```

## Quick Links

::::{grid} 2
:gutter: 3

:::{grid-item-card} 🚀 First Time Contributing?
:link: #first-time-contributors
Jump to our beginner-friendly guide
:::

:::{grid-item-card} 💻 Setup Development Environment
:link: local-development
:link-type: doc
Complete local development setup guide
:::

:::{grid-item-card} 🔄 Development Workflow
:link: development-workflow
:link-type: doc
Git workflow and best practices
:::

:::{grid-item-card} ⚙️ Configuration
:link: configuration
:link-type: doc
Environment and service configuration
:::

::::

## Ways to Contribute

Marcus welcomes contributions beyond just code! Here are the many ways you can help:

### Code Contributions
- **Bug Fixes**: Fix issues and improve stability
- **New Features**: Implement new functionality
- **Performance**: Optimize existing code
- **Refactoring**: Improve code quality and maintainability

### Non-Code Contributions
- **Documentation**: Write guides, fix typos, add examples
- **Testing**: Write tests, improve coverage, find bugs
- **Design**: Create diagrams, improve UX, design assets
- **Community**: Answer questions, write tutorials, give talks
- **Translation**: Help make Marcus accessible globally
- **Ideas**: Suggest features, improvements, use cases

## First Time Contributors

New to open source? Marcus is a great place to start!

### Find Your First Issue

1. **Look for beginner-friendly issues**:
   - Browse issues labeled [`good first issue`](https://github.com/painted-porch/marcus/labels/good%20first%20issue)
   - These are specifically chosen for newcomers
   - Usually involve small, focused changes

2. **Areas needing help**:
   - 🧪 Test coverage (especially integration tests)
   - 📚 Documentation and tutorials
   - 🐛 Bug fixes
   - 🔧 Provider support (Jira, Trello, Linear)

3. **Don't see something you like?**
   - Ask in [GitHub Discussions](https://github.com/painted-porch/marcus/discussions)
   - We'll help you find a suitable task!

### First Pull Request Checklist

Before submitting your first PR, make sure you've:

- [ ] Read the [Local Development Setup](local-development.md) guide
- [ ] Set up your development environment
- [ ] Found an issue to work on (or created one)
- [ ] Made your changes in a new branch from `develop`
- [ ] Tested your changes locally
- [ ] All pre-commit hooks pass
- [ ] Opened a Pull Request targeting `develop`

```{tip}
Your first PR doesn't need to be perfect! Start small (fix a typo, improve an error message) and learn from feedback.
```

## Development Prerequisites

### Required Software

- **Python 3.11+**: Core runtime
- **Docker & Docker Compose**: For running Planka
- **Git**: Version control
- **Node.js 16+**: For kanban-mcp

### AI Model (Choose One)

::::{grid} 2
:gutter: 3

:::{grid-item-card} 🆓 Free Option (Recommended)
**Ollama with Qwen2.5-Coder**

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Pull model
ollama pull qwen2.5-coder:7b

# Use free config
cp .env.dev.example .env
```

✅ No cost, runs locally, excellent code quality
:::

:::{grid-item-card} 💳 Paid Options
**Cloud AI Providers**

- Anthropic (Claude)
- OpenAI (GPT-4)
- Google (Gemini)

Requires API keys and usage fees
:::

::::

See [Setup Local LLM Guide](../getting-started/setup-local-llm.md) for complete free setup instructions.

## Branching Strategy

Marcus uses a **develop branch workflow** to manage contributions efficiently:

```{important}
All pull requests must target the `develop` branch, not `main`.
```

### Branch Overview

- **`main`**: Production-ready code (protected, no direct pushes)
- **`develop`**: Primary development branch (all PRs target here)
- **Feature branches**: Created in your fork from `develop`

### Why This Approach?

✅ Reduces merge conflicts by keeping development synchronized
✅ Allows testing before production release
✅ Keeps your fork clean and organized
✅ Easy to stay up-to-date with latest changes

### Quick Workflow

```bash
# 1. Fork the repository on GitHub

# 2. Clone your fork
git clone https://github.com/YOUR_USERNAME/marcus.git
cd marcus

# 3. Add upstream remote
git remote add upstream https://github.com/painted-porch/marcus.git

# 4. Always branch from develop
git checkout develop
git pull upstream develop

# 5. Create feature branch
git checkout -b feature/your-feature-name

# 6. Make changes and submit PR targeting develop
```

See [Development Workflow](development-workflow.md) for the complete guide.

## Code Quality Standards

All contributions must meet our quality standards:

### Pre-Commit Hooks

We use [pre-commit](https://pre-commit.com/) hooks that run automatically:

- **MyPy**: Static type checking
- **Black**: Code formatting
- **Ruff**: Fast linting
- **isort**: Import organization
- **detect-secrets**: Prevent committing secrets

```bash
# Install hooks (one-time)
pre-commit install

# Run manually on all files
pre-commit run --all-files

# Run on staged files
pre-commit run
```

### Quality Checklist

Before submitting a PR:

- [ ] All pre-commit hooks pass
- [ ] Tests pass locally (`pytest`)
- [ ] MyPy type checking passes (`mypy src/`)
- [ ] Code is formatted with Black
- [ ] Imports organized with isort
- [ ] No secrets detected
- [ ] Documentation updated
- [ ] Commit messages follow convention

## Coding Standards

### Python Style

We follow [PEP 8](https://peps.python.org/pep-0008/) with these requirements:

1. **Type Hints**: Always use for function arguments and returns
2. **Docstrings**: NumPy-style for all public functions/classes
3. **Error Handling**: Use Marcus Error Framework for user-facing errors
4. **Logging**: Use structured logging, not print statements
5. **Constants**: Define at module level in UPPER_CASE
6. **Tests**: Aim for 80% coverage, test edge cases

### Example

```python
def assign_task(agent_id: str, task_id: str, priority: int = 1) -> TaskAssignment:
    """
    Assign a task to an agent with optional priority.

    Parameters
    ----------
    agent_id : str
        Unique identifier of the agent
    task_id : str
        Unique identifier of the task
    priority : int, optional
        Task priority (1-5), by default 1

    Returns
    -------
    TaskAssignment
        Assignment details including agent, task, and timestamp

    Raises
    ------
    AgentNotFoundError
        If agent doesn't exist
    TaskNotFoundError
        If task doesn't exist
    """
    # Implementation here
    pass
```

### Commit Message Convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation only
- `style`: Code style (formatting)
- `refactor`: Code restructuring
- `perf`: Performance improvement
- `test`: Adding/correcting tests
- `chore`: Maintenance tasks

**Examples:**

```bash
# Good
git commit -m "feat(worker): add exponential backoff for retries"
git commit -m "fix(kanban): handle GitHub API rate limits"
git commit -m "docs(contributing): add code review guidelines"

# Bad
git commit -m "fixed stuff"
git commit -m "WIP"
```

## Testing Requirements

### Test Organization

```
tests/
├── unit/                    # Fast, isolated unit tests
│   ├── ai/
│   ├── core/
│   ├── mcp/
│   └── visualization/
├── integration/             # Tests requiring external services
│   ├── e2e/
│   ├── api/
│   └── external/
├── future_features/         # TDD tests for unimplemented features
└── performance/             # Performance tests
```

### Running Tests

```bash
# All tests
pytest

# With coverage report
pytest --cov=src --cov-report=html

# Only unit tests (fast)
pytest tests/unit/

# Specific test file
pytest tests/unit/core/test_task_manager.py

# Skip slow tests
pytest -m "not slow"
```

### Coverage Requirements

- **New code**: Minimum 80% coverage
- **Changed code**: Maintain or improve existing coverage
- **Edge cases**: Test error conditions and boundaries
- **Integration tests**: For features requiring external services

See [CLAUDE.md - Test Writing Instructions](https://github.com/painted-porch/marcus/blob/develop/CLAUDE.md#test-writing-instructions) for detailed testing guidelines.

## Documentation

### When to Update Docs

Update documentation when you:
- Add a new feature
- Change existing behavior
- Fix confusing documentation
- Add examples or tutorials

### Documentation Structure

```
docs/source/
├── getting-started/     # New user guides
├── guides/              # How-to guides
├── developer/           # Developer guides (you are here)
├── concepts/            # Design philosophy
├── systems/             # Technical architecture
├── api/                 # API reference
└── roadmap/             # Future plans
```

### Placement Guidelines

1. **Determine audience first:**
   - End users → `/docs/source/guides/`
   - Developers → `/docs/source/developer/`
   - Concepts → `/docs/source/concepts/`
   - Architecture → `/docs/source/systems/`

2. **Use descriptive filenames**: `setup-github-integration.md` not `github.md`

3. **Check if docs exist**: Update rather than duplicate

See [CLAUDE.md - Documentation Placement Rules](https://github.com/painted-porch/marcus/blob/develop/CLAUDE.md#documentation-placement-rules) for complete guidelines.

## Pull Request Process

### Before Submitting

Run through this checklist:

- [ ] All pre-commit hooks pass
- [ ] Tests pass locally
- [ ] MyPy type checking passes
- [ ] Documentation updated
- [ ] Commit messages follow convention
- [ ] PR description explains changes
- [ ] Branch is up-to-date with `upstream/develop`

### PR Template

When you open a PR, our template will guide you through providing:

- **Description**: What does this PR do?
- **Type of Change**: Bug fix, feature, docs, etc.
- **Branch Information**: Confirms targeting `develop`
- **Testing**: What tests were added/updated?
- **Documentation**: What docs were updated?
- **Checklist**: Final quality checks

### Review Process

1. **Automated Checks**: CI runs tests, linting, security scans
2. **Code Review**: Maintainers review for quality and fit
3. **Feedback**: Address requested changes
4. **Approval**: Once approved, we'll merge your PR!

### After Merge

```bash
# 1. Delete your feature branch
git branch -d feature/your-feature-name
git push origin --delete feature/your-feature-name

# 2. Update your fork's develop branch
git checkout develop
git pull upstream develop
git push origin develop

# 3. Celebrate! 🎉
```

## Code Review Guidelines

When reviewing code (or having yours reviewed), focus on:

### Review Priorities

1. **Correctness**: Does it solve the problem?
2. **Tests**: Are edge cases covered?
3. **Clarity**: Is the code understandable?
4. **Performance**: Are there obvious inefficiencies?
5. **Security**: Any potential vulnerabilities?
6. **Compatibility**: Does it break existing functionality?

### Giving Feedback

- Be respectful and constructive
- Explain the "why" behind suggestions
- Distinguish between "must fix" and "nice to have"
- Acknowledge good work
- Ask questions rather than making demands

### Receiving Feedback

- Assume good intentions
- Ask for clarification if unclear
- Discuss disagreements professionally
- Learn from suggestions
- Thank reviewers for their time

```{tip}
Code review is a learning opportunity for both reviewer and author. Approach it with curiosity and respect.
```

## Recognition System

We value all contributions! Contributors are recognized through:

- **CONTRIBUTORS.md**: Automatic listing of all contributors
- **Release Notes**: Significant contributions highlighted
- **Project README**: Major contributors featured

Every contribution, no matter how small, makes Marcus better!

## Getting Help

### Where to Ask

- **[GitHub Discussions](https://github.com/painted-porch/marcus/discussions)**: General questions and ideas
- **Issue Comments**: Questions about specific issues
- **[Discord](https://discord.gg/DZWTbXr4)**: Real-time community chat

### Tips for Getting Help

1. **Search first**: Check docs, issues, and discussions
2. **Be specific**: Include error messages, code samples, context
3. **Show your work**: Explain what you've tried
4. **Be patient**: Maintainers are volunteers

## Additional Resources

### Marcus Documentation

- [Core Concepts](../getting-started/core-concepts.md)
- [Agent Workflow Guide](../guides/agent-workflows/agent-workflow.md)
- [Philosophy](../concepts/philosophy.md)
- [Systems Architecture](../systems/README.md)

### Developer Guides

- [Local Development Setup](local-development.md)
- [Development Workflow](development-workflow.md)
- [Configuration Reference](configuration.md)
- [CLAUDE.md](https://github.com/painted-porch/marcus/blob/develop/CLAUDE.md) - Project-specific guidelines

### Learn More

- [Python Testing Guide](https://realpython.com/python-testing/)
- [Conventional Commits](https://www.conventionalcommits.org/)
- [How to Write Good Documentation](https://www.writethedocs.org/guide/)
- [Pre-commit Documentation](https://pre-commit.com/)

### Tools We Use

- [Black](https://black.readthedocs.io/) - Code formatter
- [Pytest](https://docs.pytest.org/) - Testing framework
- [MyPy](https://mypy.readthedocs.io/) - Type checking
- [Ruff](https://docs.astral.sh/ruff/) - Fast linting
- [Sphinx](https://www.sphinx-doc.org/) - Documentation generator

---

## Thank You!

Every contribution makes Marcus better. Whether this is your first open source contribution or your thousandth, we're grateful you're here.

Welcome to the Marcus community! 🚀

```{seealso}
For the complete contributing guide with all details, see [CONTRIBUTING.md](https://github.com/painted-porch/marcus/blob/develop/CONTRIBUTING.md) in the repository root.
```
