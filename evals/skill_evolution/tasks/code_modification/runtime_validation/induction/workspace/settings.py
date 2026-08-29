from dataclasses import dataclass


@dataclass(frozen=True)
class BatchSettings:
    default_batch_size: int = 100
