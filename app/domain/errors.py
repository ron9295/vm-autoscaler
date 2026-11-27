"""Exceptions raised by adapters. The service layer only knows these, never httpx errors."""


class AutoscalerError(Exception):
    """Base class for all errors raised by this service."""


class GitOpsError(AutoscalerError):
    """Base class for errors talking to the internal GitOps backend."""


class GitOpsUnavailableError(GitOpsError):
    """Connection error, timeout or 5xx. Retried for reads; never sync Argo after this."""


class GitOpsConflictError(GitOpsError):
    """409: someone else changed the component. The service re-reads once and retries."""


class GitOpsNotFoundError(GitOpsError):
    """404: the backend does not know this target."""


class GitOpsRejectedError(GitOpsError):
    """Any other 4xx. The message contains the status and a truncated response body."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"GitOps backend rejected the request: HTTP {status_code}: {body}")


class GitOpsResponseError(GitOpsError):
    """The response was not valid JSON or did not match the expected shape."""


class ArgoCdError(AutoscalerError):
    """Argo CD returned an error we cannot handle."""


class ArgoCdUnavailableError(ArgoCdError):
    """Argo CD could not be reached (connection error, timeout, 5xx)."""


class AlertParseError(AutoscalerError):
    """The Grafana payload is missing something we need (e.g. a required label)."""


class ConfigError(AutoscalerError):
    """The YAML config file is invalid."""


class GitOpsManifestError(GitOpsError):
    """The manifest file cannot be edited safely (path missing, ambiguous document, ...)."""
