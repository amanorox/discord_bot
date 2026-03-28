import argparse
import asyncio
import concurrent.futures
import datetime
import inspect
import json
import os
import threading
import time

import discord
import openai
from discord.ext import commands
from dotenv import load_dotenv

from cevio_tts import VoiceParams, save_wave, start_cevio

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")
if not OPENAI_API_KEY:
    raise RuntimeError("環境変数 OPENAI_API_KEY が未設定です。")

AUDIOFILE = "test.wav"
VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "1482359368992161918"))
DEFAULT_FFMPEG = "C:\\Users\\amano\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe"
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_PATH") or (DEFAULT_FFMPEG if os.path.exists(DEFAULT_FFMPEG) else "ffmpeg")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
client = openai.OpenAI(api_key=OPENAI_API_KEY)

bot_ready_event = threading.Event()
MAX_REPLY_CHAIN_MESSAGES = 10
MAX_REPLY_MESSAGE_CHARS = 5000
MAX_TOOL_CALL_ROUNDS = 5

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
            if args:
                raise RuntimeError("このツールは引数を受け取りません。")

            result = tool_fn()
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
                if event.start_time is None:
                    start_text = "開始時刻未設定"
                else:
                    start_text = event.start_time.astimezone(jst).strftime("%Y-%m-%d %H:%M JST")

                if event.end_time is None:
                    end_text = "終了時刻未設定"
                else:
                    end_text = event.end_time.astimezone(jst).strftime("%Y-%m-%d %H:%M JST")

                lines.append(f"- {event.name}: {start_text} - {end_text}")
    except Exception as e:
        return f"イベント一覧の取得に失敗しました: {e}"

    if not lines:
        return "現在予定されているイベントはありません。"

    return "\n".join(lines)


TOOL_REGISTRY = {
    "list_all_event": list_all_event,
}


def build_message_text_for_openai(message: discord.Message) -> str:
    text = message.content.strip()
    if not text:
        return ""

    if len(text) > MAX_REPLY_MESSAGE_CHARS:
        text = text[:MAX_REPLY_MESSAGE_CHARS] + "..."

    return text


async def collect_reply_chain_messages(message: discord.Message, max_messages: int = MAX_REPLY_CHAIN_MESSAGES) -> list[
    discord.Message]:
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


@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user}")
    bot_ready_event.set()


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
            openai_input = [{"role": "developer", "content": "You are a Discord bot for Final Fantasy XIV Guild Tranquility."}]

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

            reply_text = (response.output_text or "").strip()
            if not reply_text:
                reply_text = "回答を生成できませんでした。"

            await message.reply(reply_text)
        except Exception as e:
            print(f"Error: {e}")
            await message.channel.send(f"エラーが発生しました: {e}")

    await bot.process_commands(message)


async def play_audio_file_in_channel(target_channel: discord.VoiceChannel, audio_path: str):
    voice_client = discord.utils.get(bot.voice_clients, guild=target_channel.guild)

    if voice_client is None:
        voice_client = await target_channel.connect()
    elif voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)

    if voice_client.is_playing():
        raise RuntimeError("現在ほかの音声を再生中です。")

    playback_done = asyncio.Event()
    playback_error = {"error": None}

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

    await asyncio.to_thread(save_wave, text, output_path=AUDIOFILE, params=VoiceParams(speed=55))
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


async def synthesize_and_play_timeline(timed_lines: list[tuple[int, str]], target_channel_id: int, origin_time: float):
    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    for elapsed_seconds, text in timed_lines:
        remaining = origin_time + elapsed_seconds - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        await asyncio.to_thread(save_wave, text, output_path=AUDIOFILE, params=VoiceParams(speed=55))
        await play_audio_file_in_channel(channel, AUDIOFILE)


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
            full_date_str = f"{current_year}-{date_str}"
            parsed_date = datetime.datetime.strptime(full_date_str, "%Y-%m-%d").date()
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
        start_time = start_time_jst - datetime.timedelta(hours=9)
        start_time = start_time.replace(tzinfo=datetime.timezone.utc)
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
                f"- {event.name}: {start_time_jst.strftime('%Y-%m-%d %H:%M JST')} - {(start_time_jst + datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M JST')}"
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

    if not response_lines:
        response_lines.append("処理対象の日付がありませんでした。")

    await ctx.send("\n".join(response_lines))


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


def build_gui():
    import tkinter as tk
    from tkinter import scrolledtext

    root = tk.Tk()
    root.title("Discord Bot Controller")
    root.geometry("560x380")

    title = tk.Label(root, text="読み上げテキスト", anchor="w")
    title.pack(fill="x", padx=12, pady=(12, 4))

    offset_frame = tk.Frame(root)
    offset_frame.pack(fill="x", padx=12, pady=(0, 8))
    offset_label = tk.Label(offset_frame, text="再生開始オフセット秒数（正負の整数）")
    offset_label.pack(side="left")
    offset_entry = tk.Entry(offset_frame, width=8)
    offset_entry.pack(side="left", padx=(8, 0))
    offset_entry.insert(0, "0")

    input_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=10)
    input_box.pack(fill="both", expand=True, padx=12)
    input_box.insert("1.0", "00:00 ここに読み上げたいテキストを入力してください。")

    status_var = tk.StringVar(value="Bot起動中...")
    status = tk.Label(root, textvariable=status_var, anchor="w")
    status.pack(fill="x", padx=12, pady=(8, 4))

    button_frame = tk.Frame(root)
    button_frame.pack(fill="x", padx=12, pady=(0, 12))

    play_button = tk.Button(button_frame, text="再生")
    play_button.pack(side="left", fill="x", expand=True)

    stop_button = tk.Button(button_frame, text="再生停止")
    stop_button.pack(side="left", fill="x", expand=True, padx=(8, 0))

    leave_button = tk.Button(button_frame, text="VC退室")
    leave_button.pack(side="left", fill="x", expand=True, padx=(8, 0))

    playback_state = {"future": None}

    def update_ui(
            message: str,
            enable_play: bool = True,
            enable_stop: bool = False,
            enable_leave: bool = True,
            enable_offset: bool = True,
    ):
        status_var.set(message)
        play_button.config(state=tk.NORMAL if enable_play else tk.DISABLED)
        stop_button.config(state=tk.NORMAL if enable_stop else tk.DISABLED)
        leave_button.config(state=tk.NORMAL if enable_leave else tk.DISABLED)
        offset_entry.config(state=tk.NORMAL if enable_offset else tk.DISABLED)

    def finish_playback(message: str):
        playback_state["future"] = None
        update_ui(message, enable_play=True, enable_stop=False, enable_leave=True, enable_offset=True)

    def on_play_click():
        current_future = playback_state["future"]
        if current_future is not None and not current_future.done():
            update_ui(
                "すでに再生中です。停止してから再実行してください。",
                enable_play=False,
                enable_stop=True,
                enable_leave=False,
                enable_offset=False,
            )
            return

        text = input_box.get("1.0", tk.END)

        try:
            timed_lines = parse_timed_lines(text)
        except RuntimeError as e:
            update_ui(str(e))
            return

        if not bot_ready_event.is_set():
            update_ui("Botの接続完了を待っています。")
            return

        offset_raw = offset_entry.get().strip()
        if offset_raw == "":
            offset_seconds = 0
        else:
            try:
                offset_seconds = int(offset_raw)
            except ValueError:
                update_ui("オフセット秒数は正負の整数で入力してください。")
                return

        origin_time = time.monotonic() + offset_seconds
        update_ui(
            f"スケジュール再生を開始しました... (オフセット: {offset_seconds:+d}秒)",
            enable_play=False,
            enable_stop=True,
            enable_leave=False,
            enable_offset=False,
        )
        future = asyncio.run_coroutine_threadsafe(
            synthesize_and_play_timeline(timed_lines, VOICE_CHANNEL_ID, origin_time),
            bot.loop,
        )
        playback_state["future"] = future

        def done_callback(done_future):
            try:
                done_future.result()
                root.after(0, lambda: finish_playback("再生が完了しました。"))
            except concurrent.futures.CancelledError:
                root.after(0, lambda: finish_playback("再生を中断しました。"))
            except Exception as e:
                root.after(0, lambda: finish_playback(f"再生に失敗しました: {e}"))

        future.add_done_callback(done_callback)

    def on_stop_click():
        if not bot_ready_event.is_set():
            update_ui("Botの接続完了を待っています。")
            return

        current_future = playback_state["future"]
        has_running_timeline = current_future is not None and not current_future.done()
        if has_running_timeline:
            current_future.cancel()

        update_ui("再生を停止しています...", enable_play=False, enable_stop=False, enable_leave=False,
                  enable_offset=False)
        future = asyncio.run_coroutine_threadsafe(stop_current_playback(VOICE_CHANNEL_ID), bot.loop)

        def done_callback(done_future):
            try:
                stopped_now = done_future.result()
                if has_running_timeline:
                    return
                if stopped_now:
                    root.after(0, lambda: update_ui("現在の再生を停止しました。", enable_play=True, enable_stop=False,
                                                    enable_leave=True, enable_offset=True))
                else:
                    root.after(0, lambda: update_ui("停止対象の再生はありません。", enable_play=True, enable_stop=False,
                                                    enable_leave=True, enable_offset=True))
            except Exception as e:
                root.after(0, lambda: update_ui(f"停止に失敗しました: {e}", enable_play=True, enable_stop=False,
                                                enable_leave=True, enable_offset=True))

        future.add_done_callback(done_callback)

    def on_leave_click():
        if not bot_ready_event.is_set():
            update_ui("Botの接続完了を待っています。")
            return

        current_future = playback_state["future"]
        if current_future is not None and not current_future.done():
            current_future.cancel()

        update_ui("VCから退室しています...", enable_play=False, enable_stop=False, enable_leave=False,
                  enable_offset=False)
        future = asyncio.run_coroutine_threadsafe(leave_voice_channel(VOICE_CHANNEL_ID), bot.loop)

        def done_callback(done_future):
            try:
                playback_state["future"] = None
                disconnected = done_future.result()
                if disconnected:
                    root.after(0, lambda: update_ui("VCから退室しました。", enable_play=True, enable_stop=False,
                                                    enable_leave=True, enable_offset=True))
                else:
                    root.after(0, lambda: update_ui("BotはVCに接続していません。", enable_play=True, enable_stop=False,
                                                    enable_leave=True, enable_offset=True))
            except Exception as e:
                root.after(0, lambda: update_ui(f"VC退室に失敗しました: {e}", enable_play=True, enable_stop=False,
                                                enable_leave=True, enable_offset=True))

        future.add_done_callback(done_callback)

    def on_close():
        current_future = playback_state["future"]
        if current_future is not None and not current_future.done():
            current_future.cancel()

        if bot_ready_event.is_set() and not bot.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(bot.close(), bot.loop).result(timeout=5)
            except Exception as e:
                print(f"[WARN] bot.close failed: {e}")
        root.destroy()

    play_button.config(command=on_play_click)
    stop_button.config(command=on_stop_click)
    leave_button.config(command=on_leave_click)
    update_ui("Bot起動中...", enable_play=True, enable_stop=False, enable_leave=True)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def start_bot_in_background():
    def run_bot():
        bot.run(TOKEN)

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    return thread


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Discord event bot runner")
    parser.add_argument(
        "--tts",
        action="store_true",
        help="CeVIO TTS と GUI コントローラーを有効化します。",
    )
    return parser.parse_args(argv)


def run_bot_forever():
    bot.run(TOKEN)


def main(argv=None):
    args = parse_args(argv)

    if args.tts:
        start_cevio()
        start_bot_in_background()
        build_gui()
        return

    run_bot_forever()


if __name__ == "__main__":
    main()
