"""Small CeVIO TTS library for Python applications.

This module wraps the CeVIO AI COM interface behind a typed, reusable API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Union

try:
    import win32com.client as win32com_client
except ImportError:  # pragma: no cover - tested via monkeypatch
    win32com_client = None

PathLike = Union[str, os.PathLike[str]]

SERVICE_CONTROL_PROGID = "CeVIO.Talk.RemoteService2.ServiceControl2V40"
TALKER_PROGID = "CeVIO.Talk.RemoteService2.Talker2V40"


class CevioError(RuntimeError):
    """Base exception for CeVIO wrapper errors."""


class CevioUnavailableError(CevioError):
    """Raised when pywin32 or CeVIO COM components are unavailable."""


class CastNotFoundError(CevioError):
    """Raised when the requested cast is not available."""


@dataclass(frozen=True)
class VoiceParams:
    """Speech synthesis parameters expected by CeVIO (0-100)."""

    volume: int = 50
    speed: int = 50
    tone: int = 50
    alpha: int = 50

    def validate(self) -> None:
        for name, value in (
            ("volume", self.volume),
            ("speed", self.speed),
            ("tone", self.tone),
            ("alpha", self.alpha),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"{name} must be between 0 and 100: {value}")


def _dispatch(prog_id: str) -> Any:
    if win32com_client is None:
        raise CevioUnavailableError(
            "pywin32 is not installed. Run: pip install pywin32"
        )

    try:
        return win32com_client.Dispatch(prog_id)
    except Exception as exc:  # pragma: no cover - depends on local COM registration
        raise CevioUnavailableError(
            "Failed to connect CeVIO COM object. Ensure CeVIO AI is installed."
        ) from exc


def start_cevio(background: bool = False) -> Any:
    """Start CeVIO host and return the service COM object."""
    service = _dispatch(SERVICE_CONTROL_PROGID)
    result = service.StartHost(background)
    if result not in (0, 1):
        raise CevioError(f"Unexpected StartHost result: {result}")
    return service


def get_available_casts() -> List[str]:
    """Return all available cast names."""
    talker = _dispatch(TALKER_PROGID)
    casts = talker.AvailableCasts
    return [casts.At(i) for i in range(casts.Length)]


def _select_cast(talker: Any, cast: Optional[str], fallback_to_first: bool) -> str:
    available = get_available_casts()
    if not available:
        raise CevioError("No available casts found.")

    if cast is None:
        selected = available[0]
    elif cast in available:
        selected = cast
    elif fallback_to_first:
        selected = available[0]
    else:
        raise CastNotFoundError(f"Cast not found: {cast}. available={available}")

    talker.Cast = selected
    return selected


def _apply_params(talker: Any, params: VoiceParams) -> None:
    params.validate()
    talker.Volume = params.volume
    talker.Speed = params.speed
    talker.Tone = params.tone
    talker.Alpha = params.alpha


def speak(
    text: str,
    *,
    cast: Optional[str] = None,
    params: Optional[VoiceParams] = None,
    fallback_to_first_cast: bool = True,
) -> str:
    """Speak text synchronously and return the cast that was used."""
    talker = _dispatch(TALKER_PROGID)
    selected_cast = _select_cast(talker, cast, fallback_to_first_cast)
    _apply_params(talker, params or VoiceParams())

    state = talker.Speak(text)
    state.Wait()
    return selected_cast


def save_wave(
    text: str,
    output_path: PathLike,
    *,
    cast: Optional[str] = None,
    params: Optional[VoiceParams] = None,
    fallback_to_first_cast: bool = True,
) -> str:
    """Generate speech into a .wav file and return absolute output path."""
    talker = _dispatch(TALKER_PROGID)
    selected_cast = _select_cast(talker, cast, fallback_to_first_cast)
    _apply_params(talker, params or VoiceParams())

    abs_path = os.path.abspath(os.fspath(output_path))
    ok = talker.OutputWaveToFile(text, abs_path)
    if not ok:
        raise CevioError(
            f"OutputWaveToFile failed. cast={selected_cast}, path={abs_path}"
        )

    return abs_path


__all__: Sequence[str] = (
    "CevioError",
    "CevioUnavailableError",
    "CastNotFoundError",
    "VoiceParams",
    "start_cevio",
    "get_available_casts",
    "speak",
    "save_wave",
)

