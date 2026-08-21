from __future__ import annotations


class VegavisualsError(Exception):
    """Base error for public vegavisuals operations."""


class ValidationError(VegavisualsError):
    """A visualization or asset is not valid."""


class PolicyError(ValidationError):
    """A path, URL, or publication violates the safety policy."""


class ManifestError(ValidationError):
    """A project manifest or lock file is not valid."""


class RenderError(VegavisualsError):
    """The renderer failed or produced an invalid artifact."""
