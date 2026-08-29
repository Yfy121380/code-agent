from dataclasses import dataclass


@dataclass(frozen=True)
class DispatchSettings:
    default_timeout_ms: int = 5000
