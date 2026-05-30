import argparse
import asyncio
import concurrent.futures
import datetime
import inspect
import json
import os
import threading
import time
from pathlib import Path

import discord
import openai
import uvicorn
from discord.ext import commands
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from tts_backend import VoiceParams, get_backend

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")
if not OPENAI_API_KEY:
    raise RuntimeError("環境変数 OPENAI_API_KEY が未設定です。")

AUDIOFILE = "test.wav"
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "1482359368992161918"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
DEFAULT_FFMPEG = "C:\\Users\\amano\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe"
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_PATH") or (DEFAULT_FFMPEG if os.path.exists(DEFAULT_FFMPEG) else "ffmpeg")
STATIC_DIR = Path(__file__).parent / "static"

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = openai.OpenAI(api_key=OPENAI_API_KEY)

bot_ready_event = threading.Event()
MAX_REPLY_CHAIN_MESSAGES = 10
MAX_REPLY_MESSAGE_CHARS = 5000
MAX_TOOL_CALL_ROUNDS = 5

# ---------- Web server state ----------
web_playback_state: dict = {"future": None}
ws_clients: set[WebSocket] = set()
_ws_loop: asyncio.AbstractEventLoop | None = None


async def broadcast_status(message: str, is_playing: bool) -> None:
    payload = json.dumps({"message": message, "is_playing": is_playing})
    dead: set[WebSocket] = set()
    for ws in list(ws_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


def _schedule_broadcast(message: str, is_playing: bool) -> None:
    """Thread-safe broadcast trigger from non-async context."""
    if _ws_loop is not None and not _ws_loop.is_closed():
        asyncio.run_coroutine_threadsafe(broadcast_status(message, is_playing), _ws_loop)


# ---------- FastAPI app ----------
app = FastAPI()

STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/status")
async def status():
    is_playing = (
        web_playback_state["future"] is not None
        and not web_playback_state["future"].done()
    )
    return JSONResponse({
        "bot_ready": bot_ready_event.is_set(),
        "is_playing": is_playing,
    })


class PlayRequest:
    def __init__(self, script: str, offset: int = 0):
        self.script = script
        self.offset = offset


from pydantic import BaseModel


class PlayBody(BaseModel):
    script: str
    offset: int = 0


@app.post("/play")
async def api_play(body: PlayBody):
    if not bot_ready_event.is_set():
        return JSONResponse({"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503)

    current = web_playback_state["future"]
    if current is not None and not current.done():
        return JSONResponse({"ok": False, "message": "すでに再生中です。停止してから再実行してください。"}, status_code=409)

    try:
        timed_lines = parse_timed_lines(body.script)
    except RuntimeError as e:
        return JSONResponse({"ok": False, "message": str(e)}, status_code=400)

    origin_time = time.monotonic() + body.offset
    await broadcast_status(f"スケジュール再生を開始しました... (オフセット: {body.offset:+d}秒)", True)

    future = asyncio.run_coroutine_threadsafe(
        synthesize_and_play_timeline(timed_lines, VOICE_CHANNEL_ID, origin_time),
        bot.loop,
    )
    web_playback_state["future"] = future

    def done_callback(done_future: concurrent.futures.Future):
        try:
            done_future.result()
            web_playback_state["future"] = None
            _schedule_broadcast("再生が完了しました。", False)
        except concurrent.futures.CancelledError:
            web_playback_state["future"] = None
            _schedule_broadcast("再生を中断しました。", False)
        except Exception as exc:
            web_playback_state["future"] = None
            _schedule_broadcast(f"再生に失敗しました: {exc}", False)

    future.add_done_callback(done_callback)
    return JSONResponse({"ok": True, "message": f"スケジュール再生を開始しました。(オフセット: {body.offset:+d}秒)"})


@app.post("/stop")
async def api_stop():
    if not bot_ready_event.is_set():
        return JSONResponse({"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503)

    current = web_playback_state["future"]
    has_running = current is not None and not current.done()
    if has_running:
        current.cancel()

    await broadcast_status("再生を停止しています...", False)

    loop = bot.loop
    future = asyncio.run_coroutine_threadsafe(stop_current_playback(VOICE_CHANNEL_ID), loop)
    try:
        stopped = future.result(timeout=10)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"停止に失敗しました: {exc}"}, status_code=500)

    if has_running or stopped:
        msg = "現在の再生を停止しました。"
    else:
        msg = "停止対象の再生はありません。"
    await broadcast_status(msg, False)
    return JSONResponse({"ok": True, "message": msg})


@app.post("/leave")
async def api_leave():
    if not bot_ready_event.is_set():
        return JSONResponse({"ok": False, "message": "Botの接続完了を待っています。"}, status_code=503)

    current = web_playback_state["future"]
    if current is not None and not current.done():
        current.cancel()
    web_playback_state["future"] = None

    await broadcast_status("VCから退室しています...", False)

    future = asyncio.run_coroutine_threadsafe(leave_voice_channel(VOICE_CHANNEL_ID), bot.loop)
    try:
        disconnected = future.result(timeout=10)
    except Exception as exc:
        return JSONResponse({"ok": False, "message": f"VC退室に失敗しました: {exc}"}, status_code=500)

    msg = "VCから退室しました。" if disconnected else "BotはVCに接続していません。"
    await broadcast_status(msg, False)
    return JSONResponse({"ok": True, "message": msg})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global _ws_loop
    _ws_loop = asyncio.get_event_loop()
    await websocket.accept()
    ws_clients.add(websocket)
    is_playing = (
        web_playback_state["future"] is not None
        and not web_playback_state["future"].done()
    )
    await websocket.send_text(json.dumps({
        "message": "接続しました。" if bot_ready_event.is_set() else "Bot起動中...",
        "is_playing": is_playing,
    }))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(websocket)


# ---------- OpenAI tools ----------
tools = [
    {
        "type": "function",
        "name": "list_all_event",
        "description": "Get all scheduled events. 作成済みのイベント一覧を取得します。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "get_webpage",
        "description": "Fetch and extract readable main text from a webpage. Webページの本文テキストを取得します。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Target page URL (http/https).",
                }
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    }
]


def parse_tool_arguments(arguments_raw) -> dict:
    if arguments_raw is None:
        return {}
    if isinstance(arguments_raw, dict):
        return arguments_raw
    if not isinstance(arguments_raw, str):
        raise RuntimeError("ツール引数の形式が不正です。")
    text = arguments_raw.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ツール引数のJSON解析に失敗しました。") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("ツール引数はJSONオブジェクトである必要があります。")
    return parsed


async def build_tool_outputs(response) -> list[dict[str, str]]:
    tool_outputs: list[dict[str, str]] = []

    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "function_call":
            continue

        call_id = getattr(item, "call_id", None)
        tool_name = getattr(item, "name", "")
        arguments_raw = getattr(item, "arguments", None)

        if not call_id:
            continue

        tool_fn = TOOL_REGISTRY.get(tool_name)
        if tool_fn is None:
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": f"未対応のツールです: {tool_name}",
                }
            )
            continue

        try:
            args = parse_tool_arguments(arguments_raw)
            result = tool_fn(**args) if args else tool_fn()
            if inspect.isawaitable(result):
                result = await result
            output = str(result)
        except Exception as exc:
            output = f"ツール実行中にエラーが発生しました: {exc}"

        tool_outputs.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            }
        )

    return tool_outputs


async def get_webpage(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        return "URLが空です。"

    target_url = url.strip()
    print(target_url)
    if not (target_url.startswith("http://") or target_url.startswith("https://")):
        return "URLは http:// または https:// で始めてください。"

    def fetch_and_extract() -> str:
        try:
            from docling.document_converter import DocumentConverter
        except Exception as exc:
            return f"依存ライブラリの読み込みに失敗しました: {exc}"

        try:
            converter = DocumentConverter()
            doc = converter.convert(target_url).document
            # print(doc.export_to_markdown())
        except Exception as exc:
            return f"Webページの取得に失敗しました: {exc}"

        text = doc.export_to_markdown()
        normalized = "\n".join(line for line in (x.strip() for x in text.splitlines()) if line)
        return normalized if normalized else "本文を抽出できませんでした。"

    return await asyncio.to_thread(fetch_and_extract)


async def list_all_event() -> str:
    if not bot_ready_event.is_set() or bot.user is None:
        return "Botの接続完了前のため、イベント一覧を取得できません。"
    if bot.is_closed():
        return "Botのイベントループが停止しているため、イベント一覧を取得できません。"

    jst = datetime.timezone(datetime.timedelta(hours=9), name="JST")
    lines: list[str] = []

    try:
        for guild in bot.guilds:
            try:
                scheduled_events = await guild.fetch_scheduled_events()
            except Exception:
                scheduled_events = list(getattr(guild, "scheduled_events", []))

            if not scheduled_events:
                continue

            scheduled_events.sort(
                key=lambda ev: ev.start_time if ev.start_time is not None else datetime.datetime.max.replace(
                    tzinfo=datetime.timezone.utc
                )
            )

            lines.append(f"[{guild.name}]")
            for event in scheduled_events:
                start_text = (
                    event.start_time.astimezone(jst).strftime("%Y-%m-%d %H:%M JST")
                    if event.start_time else "開始時刻未設定"
                )
                end_text = (
                    event.end_time.astimezone(jst).strftime("%Y-%m-%d %H:%M JST")
                    if event.end_time else "終了時刻未設定"
                )
                lines.append(f"- {event.name}: {start_text} - {end_text}")
    except Exception as e:
        return f"イベント一覧の取得に失敗しました: {e}"

    return "\n".join(lines) if lines else "現在予定されているイベントはありません。"


TOOL_REGISTRY = {
    "list_all_event": list_all_event,
    "get_webpage": get_webpage,
}


def build_message_text_for_openai(message: discord.Message) -> str:
    text = message.content.strip()
    if not text:
        return ""
    if len(text) > MAX_REPLY_MESSAGE_CHARS:
        text = text[:MAX_REPLY_MESSAGE_CHARS] + "..."
    return text


async def collect_reply_chain_messages(
    message: discord.Message, max_messages: int = MAX_REPLY_CHAIN_MESSAGES
) -> list[discord.Message]:
    chain: list[discord.Message] = []
    visited_ids: set[int] = set()
    current = message

    for _ in range(max_messages):
        reference = current.reference
        if reference is None or reference.message_id is None:
            break
        ref_message_id = reference.message_id
        if ref_message_id in visited_ids:
            break
        visited_ids.add(ref_message_id)

        referenced = reference.resolved if isinstance(reference.resolved, discord.Message) else None
        if referenced is None:
            try:
                referenced = await current.channel.fetch_message(ref_message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                break

        chain.append(referenced)
        current = referenced

    chain.reverse()
    return chain


# ---------- Discord events ----------
@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user}")
    for guild in bot.guilds:
        print(f"[INFO] Guild: {guild.name} (id={guild.id})")
        for channel in guild.channels:
            print(f"[INFO]   #{channel.name} (id={channel.id}, type={channel.type})")
    bot_ready_event.set()
    _schedule_broadcast("Bot接続完了。", False)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if bot.user in message.mentions:
        try:
            user_content = message.content.replace(f"<@{bot.user.id}>", "").strip()
            if not user_content:
                await message.reply("メンションの後に質問内容を入力してください。")
                return

            chain_messages = await collect_reply_chain_messages(message)
            openai_input = [
                {"role": "developer", "content": "You are a Discord bot for Final Fantasy XIV Guild Tranquility."}
            ]

            for chain_message in chain_messages:
                chain_text = build_message_text_for_openai(chain_message)
                if not chain_text:
                    continue
                role = "assistant" if bot.user is not None and chain_message.author.id == bot.user.id else "user"
                openai_input.append({"role": role, "content": chain_text})

            openai_input.append({"role": "user", "content": user_content})
            response = client.responses.create(model="gpt-5.4-mini", tools=tools, input=openai_input)

            rounds = 0
            while rounds < MAX_TOOL_CALL_ROUNDS:
                tool_outputs = await build_tool_outputs(response)
                if not tool_outputs:
                    break
                response = client.responses.create(
                    model="gpt-5.4-mini",
                    tools=tools,
                    previous_response_id=response.id,
                    input=tool_outputs,
                )
                rounds += 1

            if rounds >= MAX_TOOL_CALL_ROUNDS and await build_tool_outputs(response):
                await message.reply("ツール呼び出しの上限に達したため、処理を中断しました。")
                return

            reply_text = (response.output_text or "").strip() or "回答を生成できませんでした。"
            reply_text = reply_text[:1900] + ("..." if len(reply_text) > 1900 else "")
            await message.reply(reply_text)
        except Exception as e:
            print(f"Error: {e}")
            await message.channel.send(f"エラーが発生しました: {e}")

    await bot.process_commands(message)


# ---------- Voice helpers ----------
async def play_audio_file_in_channel(target_channel: discord.VoiceChannel, audio_path: str):
    voice_client = discord.utils.get(bot.voice_clients, guild=target_channel.guild)

    if voice_client is None:
        voice_client = await target_channel.connect()
    elif voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)

    if voice_client.is_playing():
        raise RuntimeError("現在ほかの音声を再生中です。")

    playback_done = asyncio.Event()
    playback_error: dict = {"error": None}

    def after_playback(error):
        playback_error["error"] = error
        bot.loop.call_soon_threadsafe(playback_done.set)

    source = discord.FFmpegPCMAudio(audio_path, executable=FFMPEG_EXECUTABLE)
    voice_client.play(source, after=after_playback)
    await playback_done.wait()

    if playback_error["error"] is not None:
        raise RuntimeError(f"Playback error: {playback_error['error']}")


async def leave_voice_channel(target_channel_id: int) -> bool:
    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    voice_client = discord.utils.get(bot.voice_clients, guild=channel.guild)
    if voice_client is None or not voice_client.is_connected():
        return False

    if voice_client.is_playing():
        voice_client.stop()

    await voice_client.disconnect()
    return True


async def stop_current_playback(target_channel_id: int) -> bool:
    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    voice_client = discord.utils.get(bot.voice_clients, guild=channel.guild)
    if voice_client is None or not voice_client.is_connected():
        return False

    if voice_client.is_playing():
        voice_client.stop()
        return True

    return False


async def synthesize_and_play(text: str, target_channel_id: int):
    if not text:
        raise RuntimeError("テキストが空です。")

    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    await asyncio.to_thread(get_backend().save_wave, text, AUDIOFILE, params=VoiceParams(speed=55))
    await play_audio_file_in_channel(channel, AUDIOFILE)


def parse_timed_lines(script_text: str) -> list[tuple[int, str]]:
    timed_lines: list[tuple[int, str]] = []
    last_seconds = -1

    for line_no, raw_line in enumerate(script_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError(f"{line_no}行目の形式が不正です。`MM:SS 読み上げテキスト` 形式で入力してください。")

        time_part, text = parts[0], parts[1].strip()
        if not text:
            raise RuntimeError(f"{line_no}行目に読み上げテキストがありません。")

        try:
            mm_str, ss_str = time_part.split(":", maxsplit=1)
            minutes = int(mm_str)
            seconds = int(ss_str)
        except ValueError as exc:
            raise RuntimeError(f"{line_no}行目の時刻が不正です。`MM:SS` 形式で入力してください。") from exc

        if minutes < 0 or seconds < 0 or seconds >= 60:
            raise RuntimeError(f"{line_no}行目の時刻が不正です。秒は 00-59 の範囲で入力してください。")

        elapsed_seconds = minutes * 60 + seconds
        if elapsed_seconds < last_seconds:
            raise RuntimeError(f"{line_no}行目の時刻が前の行より小さいです。時刻は昇順で入力してください。")

        timed_lines.append((elapsed_seconds, text))
        last_seconds = elapsed_seconds

    if not timed_lines:
        raise RuntimeError("有効な読み上げデータがありません。")

    return timed_lines


async def synthesize_and_play_timeline(
    timed_lines: list[tuple[int, str]], target_channel_id: int, origin_time: float
):
    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    for elapsed_seconds, text in timed_lines:
        remaining = origin_time + elapsed_seconds - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        await asyncio.to_thread(get_backend().save_wave, text, AUDIOFILE, params=VoiceParams(speed=55))
        await play_audio_file_in_channel(channel, AUDIOFILE)


# ---------- Discord commands ----------
@bot.command()
async def create_event(ctx, event_name: str, *date_strs: str):
    if not date_strs:
        await ctx.send("日付を1つ以上指定してください。MM-DD 形式をスペース区切りで複数指定できます。")
        return

    current_year = datetime.datetime.now().year
    valid_dates: list[tuple[str, datetime.date]] = []
    invalid_inputs: list[str] = []

    for date_str in date_strs:
        try:
            parsed_date = datetime.datetime.strptime(f"{current_year}-{date_str}", "%Y-%m-%d").date()
            valid_dates.append((date_str, parsed_date))
        except ValueError:
            invalid_inputs.append(date_str)

    seen_dates: set[datetime.date] = set()
    duplicate_inputs: list[str] = []
    unique_valid_dates: list[tuple[str, datetime.date]] = []
    for original_input, parsed_date in valid_dates:
        if parsed_date in seen_dates:
            duplicate_inputs.append(original_input)
            continue
        seen_dates.add(parsed_date)
        unique_valid_dates.append((original_input, parsed_date))

    success_messages: list[str] = []
    failed_messages: list[str] = []

    for _, event_date in unique_valid_dates:
        start_time_jst = datetime.datetime.combine(event_date, datetime.time(22, 0, 0))
        start_time = (start_time_jst - datetime.timedelta(hours=9)).replace(tzinfo=datetime.timezone.utc)
        end_time = start_time + datetime.timedelta(hours=2)

        try:
            event = await ctx.guild.create_scheduled_event(
                name=event_name,
                description="Python Bot による自動作成イベント",
                start_time=start_time,
                end_time=end_time,
                location="Gaia DC",
                privacy_level=discord.PrivacyLevel.guild_only,
                entity_type=discord.EntityType.external,
            )
            success_messages.append(
                f"- {event.name}: {start_time_jst.strftime('%Y-%m-%d %H:%M JST')} - "
                f"{(start_time_jst + datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M JST')}"
            )
        except Exception as e:
            failed_messages.append(f"- {event_date.strftime('%Y-%m-%d')}: {e}")

    if invalid_inputs:
        failed_messages.append(f"- 形式不正: {', '.join(invalid_inputs)} (MM-DD 形式で指定してください)")
    if duplicate_inputs:
        failed_messages.append(f"- 重複入力のためスキップ: {', '.join(duplicate_inputs)}")

    response_lines: list[str] = []
    if success_messages:
        response_lines.append("イベントを作成しました:")
        response_lines.extend(success_messages)
    if failed_messages:
        if success_messages:
            response_lines.append("")
        response_lines.append("作成できなかった項目:")
        response_lines.extend(failed_messages)

    await ctx.send("\n".join(response_lines) or "処理対象の日付がありませんでした。")


@bot.command()
async def play_test(ctx):
    if ctx.author.voice is None or ctx.author.voice.channel is None:
        await ctx.send("先にボイスチャンネルへ参加してから実行してください。")
        return

    if not os.path.exists(AUDIOFILE):
        await ctx.send(f"音声ファイルが見つかりません: {AUDIOFILE}")
        return

    try:
        await play_audio_file_in_channel(ctx.author.voice.channel, AUDIOFILE)
        await ctx.send(f"{ctx.author.voice.channel.mention} で `{AUDIOFILE}` を再生しました。")
    except Exception as e:
        await ctx.send(f"再生に失敗しました: {e}")


# ---------- Startup helpers ----------
def start_bot_in_background():
    def run_bot():
        bot.run(TOKEN)

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    return thread


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Discord event bot runner")
    parser.add_argument(
        "--web",
        action="store_true",
        help="Webコントローラーを有効化します（ポートは WEB_PORT 環境変数、デフォルト 8080）。",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)


    if args.web:
        get_backend().start()
        start_bot_in_background()
        print(f"[INFO] Web controller starting on http://0.0.0.0:{WEB_PORT}")
        uvicorn.run(app, host="0.0.0.0", port=WEB_PORT)
    else:
        bot.run(TOKEN)


if __name__ == "__main__":
    main()
