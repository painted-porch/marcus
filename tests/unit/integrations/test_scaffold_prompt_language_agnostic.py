"""Unit tests for bug #649 root cause 1 — scaffold prompt must be language-agnostic.

Background
----------
Bug #649 root cause 1: the ``_SCAFFOLD_PROMPT`` template at
``src/integrations/nlp_tools.py`` previously hardcoded TypeScript file
extensions and config names in its allowed-files list and example
output (``main.tsx``, ``App.tsx``, ``tsconfig.json``).  Large language
models follow concrete examples; the scaffold LLM produced
TypeScript artifacts even when the project description explicitly
asked for vanilla JavaScript (the verify-snake-3 failure mode).

These tests pin the post-fix shape of the prompt: hardcoded
TypeScript examples are removed, an explicit "honor stated language
constraints" instruction is present, and the example output is
parameterized rather than naming ``main.tsx`` outright.

The shared-foundation prompt at ``_synthesize_shared_foundation``
underwent the same fix; this file covers both.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _SCAFFOLD_PROMPT — language-agnostic structure
# ---------------------------------------------------------------------------


class TestScaffoldPromptLanguageAgnostic:
    """The scaffold prompt must not bias the LLM toward TypeScript.

    Verifies the static structure of the ``_SCAFFOLD_PROMPT`` module
    constant rather than mocking an end-to-end run.  Static-text
    assertions are sufficient because the LLM's bias was driven by
    the example text — fix the example, fix the bias.
    """

    @pytest.fixture
    def prompt(self) -> str:
        """The scaffold prompt template as a string."""
        from src.integrations.nlp_tools import _SCAFFOLD_PROMPT

        return _SCAFFOLD_PROMPT

    def test_no_hardcoded_main_tsx_in_example_output(self, prompt: str) -> None:
        """The example output must not name ``main.tsx`` outright.

        Pre-fix, the example output ended with
        ``{"path": "src/main.tsx", "content": "..."}`` which the LLM
        copied for vanilla-JS specs.  The fix replaces it with a
        parameterized placeholder so the LLM picks an extension
        matching the stated stack.
        """
        # The literal example output line must be gone.
        assert '"path": "src/main.tsx"' not in prompt
        # And the entry-point bullet must not list main.tsx as the
        # first/primary example before main.js / main.py.
        assert "main.tsx," not in prompt.split("Entry point")[1].split("\n")[0]

    def test_no_hardcoded_app_tsx_as_app_shell(self, prompt: str) -> None:
        """The app-shell bullet must not name ``App.tsx`` as the canonical form."""
        # "App.tsx or equivalent" framing biased TS as the canonical
        # shell.  Post-fix the bullet describes the role
        # language-agnostically.
        assert "App.tsx or equivalent" not in prompt

    def test_no_hardcoded_tsconfig_in_build_config(self, prompt: str) -> None:
        """The build-config bullet must not list ``tsconfig`` as the lead example.

        Pre-fix: "Build configuration (tsconfig, vite.config, ...)".
        Post-fix: bullet uses language-neutral examples or names the
        TS form only conditionally on language match.
        """
        # The pre-fix lead-with-tsconfig framing is gone.
        assert "Build configuration (tsconfig," not in prompt

    def test_explicit_honor_stated_language_instruction(self, prompt: str) -> None:
        """The prompt must explicitly tell the LLM to honor the spec's language.

        Without this instruction the LLM defaults to whatever the
        examples suggest (TypeScript pre-fix).  With the instruction,
        the LLM treats "vanilla JavaScript" in the spec as a hard
        constraint.
        """
        # Lowercase compare so wording can evolve without breaking the test.
        prompt_lower = prompt.lower()
        assert "vanilla javascript" in prompt_lower or "honor" in prompt_lower
        # Specifically: must name "vanilla javascript" as the canonical
        # opt-out example so the LLM recognizes the phrase from the spec.
        assert "vanilla javascript" in prompt_lower

    def test_forbids_files_in_unrequested_language(self, prompt: str) -> None:
        """The forbidden-files list must mention not producing files in
        a language the spec did not ask for.

        Pre-fix the forbidden list only banned "TypeScript interfaces"
        as a code-density concern, not as a language constraint.
        Post-fix it bans cross-language drift outright.
        """
        prompt_lower = prompt.lower()
        # Some phrasing equivalent to "no .ts files when spec says vanilla JS"
        # must be present.
        assert ".ts" in prompt_lower and "vanilla" in prompt_lower

    def test_gitignore_guidance_does_not_block_js_in_src(self, prompt: str) -> None:
        """The .gitignore guidance must not block ``*.js`` in ``src/``
        unconditionally.

        Pre-fix the prompt said ``"*.js (in src/)"`` must be in
        gitignore — which is correct for a TypeScript project (build
        output) but wrong for a vanilla-JS project where .js files
        ARE the source.
        """
        # The unconditional "*.js (in src/)" instruction must be gone.
        assert "*.js (in src/)" not in prompt


# ---------------------------------------------------------------------------
# _synthesize_shared_foundation prompt — language-agnostic Tech Foundation
# ---------------------------------------------------------------------------


class TestSharedFoundationPromptLanguageAgnostic:
    """The foundation prompt must not bias the LLM toward TypeScript.

    Verifies the prompt structure inside
    ``_synthesize_shared_foundation`` by reading the method source.
    A full call-and-capture test would require constructing a
    NaturalLanguageProjectCreator and mocking the LLM — overkill for
    a static text-shape assertion.
    """

    @pytest.fixture
    def method_source(self) -> str:
        """Return the source of ``_synthesize_shared_foundation`` as text."""
        import inspect

        from src.integrations.nlp_tools import NaturalLanguageProjectCreator

        return inspect.getsource(
            NaturalLanguageProjectCreator._synthesize_shared_foundation
        )

    def test_tech_foundation_bullet_no_longer_says_typescript_config(
        self, method_source: str
    ) -> None:
        """The Tech Foundation example must not say "TypeScript config".

        Pre-fix: ``"Tech Foundation: shared configuration (TypeScript
        config, routing, test harness)"``.  The TypeScript-as-example
        biased the LLM to recommend TS-foundation tasks even for
        non-TS projects.
        """
        # The exact pre-fix string must be gone.
        assert "TypeScript config, " not in method_source

    def test_tech_foundation_prompt_acknowledges_language_constraint(
        self, method_source: str
    ) -> None:
        """The Tech Foundation prompt must instruct the LLM to honor
        the spec's stated language.

        Post-fix the prompt names the language constraint as
        authoritative (e.g., "honor the language the spec actually
        states") so the LLM does not default to TypeScript when the
        spec asked for vanilla JavaScript.
        """
        # Wording can evolve; we accept any phrase that names
        # language honoring or constraint matching.
        lower = method_source.lower()
        assert (
            "honor the language" in lower
            or "language the spec" in lower
            or "stated tech stack" in lower
        )
