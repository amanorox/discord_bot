"""TTS backend abstraction layer.

Supports CeVIO AI (Windows) and VOICEVOX Engine (cross-platform).

Backend selection:
  - 環境変数 TTS_BACKEND=cevio|voicevox で明示指定
  - 未指定時: Windows → cevio, その他 → voicevox

VOICEVOX 環境変数:
  VOICEVOX_URL     (default: http://localhost:50021)
  VOICEVOX_SPEAKER (default: 3  # ずんだもん ノーマル)
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from typing import Optional

from cevio_tts import VoiceParams

# ---- VoiceParams は cevio_tts から再エクスポート ----
__all__ = ["VoiceParams", "TTSBackend", "CevioBackend", "VoicevoxBackend", "get_backend"]

PathLike = str | os.PathLike[str]


class TTSBackend(ABC):
    """共通 TTS バックエンドインターフェース。"""

    def start(self) -> None:  # noqa: B027
        """バックエンド起動処理（不要な場合は何もしない）。"""

    @abstractmethod
    def save_wave(
        self,
        text: str,
        output_path: PathLike,
        *,
        params: Optional[VoiceParams] = None,
    ) -> str:
        """テキストを音声ファイルに変換し、絶対パスを返す。"""


# ---------------------------------------------------------------------------
# CeVIO backend (Windows only)
# ---------------------------------------------------------------------------

class CevioBackend(TTSBackend):
    """CeVIO AI COM を使った Windows 専用バックエンド。"""

    def start(self) -> None:
        from cevio_tts import start_cevio as _start
        _start()

    def save_wave(
        self,
        text: str,
        output_path: PathLike,
        *,
        params: Optional[VoiceParams] = None,
    ) -> str:
        from cevio_tts import save_wave as _save
        return _save(text, output_path, params=params)


# ---------------------------------------------------------------------------
# VOICEVOX backend (cross-platform)
# ---------------------------------------------------------------------------

# VoiceParams(speed=0-100, volume=0-100, tone=0-100, alpha=0-100) を
# VOICEVOX AudioQuery パラメータへマッピング:
#   speedScale     = speed  / 50.0          (50 → 1.0)
#   volumeScale    = volume / 50.0          (50 → 1.0)
#   pitchScale     = (tone - 50) / 50 * 0.15  (50 → 0.0, ±0.15 が実用範囲)
#   intonationScale = alpha / 50.0          (50 → 1.0)

def _params_to_query_overrides(params: VoiceParams) -> dict:
    params.validate()
    return {
        "speedScale": round(params.speed / 50.0, 3),
        "volumeScale": round(params.volume / 50.0, 3),
        "pitchScale": round((params.tone - 50) / 50.0 * 0.15, 4),
        "intonationScale": round(params.alpha / 50.0, 3),
    }


class VoicevoxBackend(TTSBackend):
    """VOICEVOX Engine REST API を使ったクロスプラットフォームバックエンド。"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        speaker: Optional[int] = None,
    ) -> None:
        self._base_url = (base_url or os.getenv("VOICEVOX_URL", "http://localhost:50021")).rstrip("/")
        self._speaker = int(speaker or os.getenv("VOICEVOX_SPEAKER", "3"))

    # VOICEVOX は起動済みのサービスを前提とするため start() は疎通確認のみ
    def start(self) -> None:
        import requests  # type: ignore
        try:
            r = requests.get(f"{self._base_url}/version", timeout=5)
            r.raise_for_status()
            print(f"[INFO] VOICEVOX Engine version: {r.text.strip()}")
        except Exception as exc:
            raise RuntimeError(
                f"VOICEVOX Engine ({self._base_url}) に接続できません: {exc}"
            ) from exc

    def save_wave(
        self,
        text: str,
        output_path: PathLike,
        *,
        params: Optional[VoiceParams] = None,
    ) -> str:
        import requests  # type: ignore

        effective_params = params or VoiceParams()

        # 1. audio_query 取得
        query_resp = requests.post(
            f"{self._base_url}/audio_query",
            params={"text": text, "speaker": self._speaker},
            timeout=30,
        )
        query_resp.raise_for_status()
        query_json: dict = query_resp.json()

        # 2. VoiceParams をクエリに反映
        query_json.update(_params_to_query_overrides(effective_params))

        # 3. synthesis → WAV バイト列
        synth_resp = requests.post(
            f"{self._base_url}/synthesis",
            params={"speaker": self._speaker},
            json=query_json,
            timeout=60,
        )
        synth_resp.raise_for_status()

        abs_path = os.path.abspath(str(output_path))
        with open(abs_path, "wb") as f:
            f.write(synth_resp.content)

        return abs_path


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_backend_instance: Optional[TTSBackend] = None


def get_backend() -> TTSBackend:
    """シングルトンで TTS バックエンドを返す。

    TTS_BACKEND 環境変数で "cevio" / "voicevox" を指定可能。
    未指定時は Windows → CeVIO、それ以外 → VOICEVOX。
    """
    global _backend_instance
    if _backend_instance is not None:
        return _backend_instance

    env = os.getenv("TTS_BACKEND", "").lower().strip()
    if env == "cevio":
        _backend_instance = CevioBackend()
    elif env == "voicevox":
        _backend_instance = VoicevoxBackend()
    elif sys.platform == "win32":
        _backend_instance = CevioBackend()
    else:
        _backend_instance = VoicevoxBackend()

    return _backend_instance


