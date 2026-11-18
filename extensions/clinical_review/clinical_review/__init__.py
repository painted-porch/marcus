"""
Hospital: independent review of subverted clinical AI advice as a Marcus chain.

Issue painted-porch/marcus #918. The preregistered design is
``~/marcus-experiments/hospital/METHODS.md``.

The extension imports no kernel code at all. It talks to a Marcus server
over MCP through ``marcus_toolkit.client`` and reads the files a run leaves
behind, which is everything ``docs/architecture/extension-contract.md``
allows an extension to do.

Nothing here is a clinical tool. No patient data is used anywhere. The cases
are the paper's published treatment-point rows and vignettes written to fit
them.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
