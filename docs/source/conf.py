"""Sphinx configuration for Marcus AI documentation."""

import os
import sys
from datetime import datetime, timezone

# Add project root to path for autodoc
sys.path.insert(0, os.path.abspath("../.."))

# -- Project information -----------------------------------------------------
project = "Marcus AI"
copyright = f"{datetime.now(timezone.utc).year}, Marcus AI Contributors"
author = "Marcus AI Team"
release = "0.3.9"  # Update from your package version

# -- General configuration ---------------------------------------------------
extensions = [
    # Core Sphinx
    "sphinx.ext.autodoc",  # Auto-generate from docstrings
    "sphinx.ext.autosummary",  # Generate summary tables
    "sphinx.ext.napoleon",  # NumPy/Google docstring support
    "sphinx.ext.intersphinx",  # Link to other projects
    "sphinx.ext.viewcode",  # Add source code links
    "sphinx.ext.githubpages",  # GitHub Pages support
    # Enhanced features
    "sphinx_autodoc_typehints",  # Type hints in docs
    "sphinx_design",  # Cards, grids, tabs
    "myst_parser",  # Markdown support
    "sphinx_copybutton",  # Copy button for code blocks
    "sphinx_tabs.tabs",  # Tabbed content
    "sphinxcontrib.mermaid",  # Diagrams
]

# Autodoc settings
autodoc_default_options = {
    "members": True,
    "member-order": "bysource",
    "special-members": "__init__",
    "undoc-members": True,
    "exclude-members": "__weakref__",
}
autodoc_typehints = "description"
autodoc_class_signature = "separated"

# Mock imports for modules that are not needed for documentation
# This prevents import errors during doc builds
autodoc_mock_imports = [
    "anthropic",
    "mcp",
    "structlog",
    "psutil",
    "aiofiles",
]

# Autosummary settings
autosummary_generate = True
autosummary_generate_overwrite = True

# Napoleon settings (for NumPy docstrings)
napoleon_google_docstring = True
napoleon_numpy_docstring = True
napoleon_include_init_with_doc = True
napoleon_include_private_with_doc = False
napoleon_include_special_with_doc = True
napoleon_use_admonition_for_examples = True
napoleon_use_admonition_for_notes = True
napoleon_use_admonition_for_references = True
napoleon_use_ivar = False
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = False
napoleon_type_aliases = None
napoleon_attr_annotations = True

# MyST settings (Markdown)
myst_enable_extensions = [
    "colon_fence",  # ::: fences
    "deflist",  # Definition lists
    "fieldlist",  # Field lists
    "html_admonition",  # HTML admonitions
    "html_image",  # HTML images
    "linkify",  # Auto-link URLs
    "replacements",  # Replacements
    "smartquotes",  # Smart quotes
    "substitution",  # Substitutions
    "tasklist",  # Task lists
]
myst_heading_anchors = 3

# Suppress common warnings
suppress_warnings = [
    "myst.xref_missing",  # Suppress missing cross-reference warnings
    "misc.highlighting_failure",  # Suppress Pygments highlighting warnings
]

# Mermaid configuration
mermaid_version = "latest"

# Templates
templates_path = ["_templates"]
exclude_patterns = []

# -- Options for HTML output -------------------------------------------------
html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]

html_theme_options = {
    # Header
    "logo": {
        "text": "Marcus AI",
    },
    # Navigation
    "navbar_start": ["navbar-logo"],
    "navbar_center": ["navbar-nav"],  # Shows top-level navigation
    "navbar_end": ["navbar-icon-links", "theme-switcher"],
    "navbar_persistent": ["search-button"],
    # Icons in header
    "icon_links": [
        {
            "name": "GitHub",
            "url": "https://github.com/lwgray/marcus",
            "icon": "fa-brands fa-github",
            "type": "fontawesome",
        },
        {
            "name": "Discord",
            "url": "https://discord.gg/marcus",
            "icon": "fa-brands fa-discord",
            "type": "fontawesome",
        },
    ],
    # Footer
    "footer_start": ["copyright"],
    "footer_end": ["sphinx-version", "theme-version"],
    # Sidebar
    "show_toc_level": 2,
    "navigation_depth": 4,
    "show_nav_level": 2,
    "collapse_navigation": False,
    # Search
    "search_bar_text": "Search docs...",
    # Misc
    "use_edit_page_button": True,
    "show_version_warning_banner": True,
}

html_context = {
    "github_user": "lwgray",
    "github_repo": "marcus",
    "github_version": "main",
    "doc_path": "docs_sphinx/source",
}

html_sidebars = {"**": ["sidebar-nav-bs", "sidebar-ethical-ads"]}

# Custom CSS/JS
html_css_files = [
    "custom.css",
]

# -- Intersphinx configuration -----------------------------------------------
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
}

# -- Copy button configuration -----------------------------------------------
copybutton_prompt_text = r">>> |\.\.\. |\$ |In \[\d*\]: | {2,5}\.\.\.: | {5,8}: "
copybutton_prompt_is_regexp = True
