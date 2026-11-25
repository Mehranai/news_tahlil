"""Publication-time bounds for a scheduled news run."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PublicationWindow:
    """Include articles published after start and through end."""

    start: datetime
    end: datetime

    def as_dict(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}
