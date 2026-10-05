from __future__ import annotations

from dataclasses import dataclass


class StitchgenError(Exception):
    """Conversion cannot continue (exit code 2)."""


class UnsupportedSvgError(StitchgenError):
    pass


@dataclass(frozen=True)
class Warning:
    code: str
    message: str

    def to_json(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}
