import asyncio
import concurrent.futures
import json
import time
from pathlib import Path
from threading import Event
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


class PlayBody(BaseModel):
    script: str
    offset: int = 0
    # Discord snowflake IDs can exceed JavaScript's safe-integer range.
    channel_id: str | int | None = None


class BuildBody(BaseModel):
    script: str


class ChannelActionBody(BaseModel):
    channel_id: str | int | None = None


class WebController:
    """FastAPI web controller, wired to the Discord bot through callbacks."""

    def __init__(
        self,
        *,
        bot: Any,
        bot_ready_event: Event,
        default_channel_id: int,
        static_dir: Path,
        playback_state: dict,
        parse_timed_lines: Callable,
        build_wavs: Callable,
        wav_path_for: Callable,
        synthesize_and_play_timeline: Callable,
        stop_current_playback: Callable,
        leave_voice_channel: Callable,
    ) -> None:
        self.bot = bot
        self.bot_ready_event = bot_ready_event
        self.default_channel_id = default_channel_id
        self.static_dir = static_dir
        self.playback_state = playback_state
        self.parse_timed_lines = parse_timed_lines
        self.build_wavs = build_wavs
        self.wav_path_for = wav_path_for
        self.synthesize_and_play_timeline = synthesize_and_play_timeline
        self.stop_current_playback = stop_current_playback
        self.leave_voice_channel = leave_voice_channel
        self.ws_clients: set[WebSocket] = set()
        self._ws_loop: asyncio.AbstractEventLoop | None = None

        self.static_dir.mkdir(exist_ok=True)
        self.app = FastAPI()
        self.app.mount("/static", StaticFiles(directory=str(self.static_dir)), name="static")
        self._register_routes()

    async def broadcast_status(self, message: str, is_playing: bool) -> None:
        payload = json.dumps({"message": message, "is_playing": is_playing})
        dead: set[WebSocket] = set()
        for ws in list(self.ws_clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.add(ws)
        self.ws_clients.difference_update(dead)

    def schedule_broadcast(self, message: str, is_playing: bool) -> None:
        """Thread-safe broadcast trigger from non-async context."""
        if self._ws_loop is not None and not self._ws_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.broadcast_status(message, is_playing), self._ws_loop
            )

    def resolve_channel_id(self, channel_id: str | int | None) -> int:
        if channel_id is None or channel_id == "":
            return self.default_channel_id
        try:
            return int(channel_id)
        except (TypeError, ValueError):
            return self.default_channel_id

    def _register_routes(self) -> None:
        app = self.app

        @app.get("/")
        async def index():
            return FileResponse(str(self.static_dir / "index.html"))

        @app.get("/status")
        async def status():
            future = self.playback_state["future"]
            return JSONResponse({
                "bot_ready": self.bot_ready_event.is_set(),
                "is_playing": future is not None and not future.done(),
            })

        @app.get("/voice_channels")
        async def api_voice_channels():
            if not self.bot_ready_event.is_set():
                return JSONResponse(
                    {"ok": False, "message": "Botの接続完了を待っています。"},
                    status_code=503,
                )

            channels: list[dict] = []
            for guild in self.bot.guilds:
                for channel in guild.voice_channels:
                    channels.append({
                        "id": str(channel.id),
                        "name": channel.name,
                        "guild_name": guild.name,
                    })

            channels.sort(key=lambda channel: (channel["guild_name"], channel["name"]))
            return JSONResponse({
                "ok": True,
                "channels": channels,
                "default_channel_id": str(self.default_channel_id),
            })

        @app.post("/build")
        async def api_build(body: BuildBody):
            if self.playback_state.get("building"):
                return JSONResponse(
                    {"ok": False, "message": "すでにビルド中です。"}, status_code=409
                )

            try:
                timed_lines = self.parse_timed_lines(body.script)
            except RuntimeError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

            self.playback_state["building"] = True
            loop = asyncio.get_running_loop()
            await self.broadcast_status("ビルドを開始しました...", False)

            def progress(msg: str) -> None:
                asyncio.run_coroutine_threadsafe(self.broadcast_status(msg, False), loop)

            try:
                built, skipped = await asyncio.to_thread(
                    lambda: self.build_wavs(timed_lines, progress)
                )
            except Exception as exc:
                msg = f"ビルドに失敗しました: {exc}"
                await self.broadcast_status(msg, False)
                return JSONResponse({"ok": False, "message": msg}, status_code=500)
            finally:
                self.playback_state["building"] = False

            msg = f"ビルド完了: 新規 {built} 件 / 既存 {skipped} 件 (wav/ に保存)"
            await self.broadcast_status(msg, False)
            return JSONResponse({
                "ok": True, "message": msg, "built": built, "skipped": skipped,
            })

        @app.post("/play")
        async def api_play(body: PlayBody):
            if not self.bot_ready_event.is_set():
                return JSONResponse(
                    {"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503
                )

            current = self.playback_state["future"]
            if current is not None and not current.done():
                return JSONResponse(
                    {"ok": False, "message": "すでに再生中です。停止してから再実行してください。"},
                    status_code=409,
                )

            try:
                timed_lines = self.parse_timed_lines(body.script)
            except RuntimeError as exc:
                return JSONResponse({"ok": False, "message": str(exc)}, status_code=400)

            if self.playback_state.get("building"):
                return JSONResponse(
                    {"ok": False, "message": "ビルド中です。完了後に再実行してください。"},
                    status_code=409,
                )

            missing = [text for _, text in timed_lines if not self.wav_path_for(text).exists()]
            if missing:
                return JSONResponse(
                    {
                        "ok": False,
                        "message": f"未ビルドの音声が {len(set(missing))} 件あります。先に「ビルド」を押してください。",
                    },
                    status_code=400,
                )

            target_channel_id = self.resolve_channel_id(body.channel_id)
            origin_time = time.monotonic() + body.offset
            await self.broadcast_status(
                f"スケジュール再生を開始しました... (オフセット: {body.offset:+d}秒)", True
            )

            future = asyncio.run_coroutine_threadsafe(
                self.synthesize_and_play_timeline(timed_lines, target_channel_id, origin_time),
                self.bot.loop,
            )
            self.playback_state["future"] = future

            def done_callback(done_future: concurrent.futures.Future) -> None:
                try:
                    done_future.result()
                    self.playback_state["future"] = None
                    self.schedule_broadcast("再生が完了しました。", False)
                except concurrent.futures.CancelledError:
                    self.playback_state["future"] = None
                    self.schedule_broadcast("再生を中断しました。", False)
                except Exception as exc:
                    self.playback_state["future"] = None
                    self.schedule_broadcast(f"再生に失敗しました: {exc}", False)

            future.add_done_callback(done_callback)
            return JSONResponse({
                "ok": True,
                "message": f"スケジュール再生を開始しました。(オフセット: {body.offset:+d}秒)",
            })

        @app.post("/stop")
        async def api_stop(body: ChannelActionBody):
            if not self.bot_ready_event.is_set():
                return JSONResponse(
                    {"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503
                )

            current = self.playback_state["future"]
            has_running = current is not None and not current.done()
            if has_running:
                current.cancel()

            await self.broadcast_status("再生を停止しています...", False)
            target_channel_id = self.resolve_channel_id(body.channel_id)
            future = asyncio.run_coroutine_threadsafe(
                self.stop_current_playback(target_channel_id), self.bot.loop
            )
            try:
                stopped = future.result(timeout=10)
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "message": f"停止に失敗しました: {exc}"}, status_code=500
                )

            msg = "現在の再生を停止しました。" if has_running or stopped else "停止対象の再生はありません。"
            await self.broadcast_status(msg, False)
            return JSONResponse({"ok": True, "message": msg})

        @app.post("/leave")
        async def api_leave(body: ChannelActionBody):
            if not self.bot_ready_event.is_set():
                return JSONResponse(
                    {"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503
                )

            current = self.playback_state["future"]
            if current is not None and not current.done():
                current.cancel()
            self.playback_state["future"] = None

            await self.broadcast_status("VCから退室しています...", False)
            target_channel_id = self.resolve_channel_id(body.channel_id)
            future = asyncio.run_coroutine_threadsafe(
                self.leave_voice_channel(target_channel_id), self.bot.loop
            )
            try:
                disconnected = future.result(timeout=10)
            except Exception as exc:
                return JSONResponse(
                    {"ok": False, "message": f"VC退室に失敗しました: {exc}"}, status_code=500
                )

            msg = "VCから退室しました。" if disconnected else "BotはVCに接続していません。"
            await self.broadcast_status(msg, False)
            return JSONResponse({"ok": True, "message": msg})

        @app.websocket("/ws")
        async def websocket_endpoint(websocket: WebSocket):
            self._ws_loop = asyncio.get_running_loop()
            await websocket.accept()
            self.ws_clients.add(websocket)
            future = self.playback_state["future"]
            is_playing = future is not None and not future.done()
            await websocket.send_text(json.dumps({
                "message": "接続しました。" if self.bot_ready_event.is_set() else "Bot起動中...",
                "is_playing": is_playing,
            }))
            try:
                while True:
                    await websocket.receive_text()
            except WebSocketDisconnect:
                self.ws_clients.discard(websocket)

