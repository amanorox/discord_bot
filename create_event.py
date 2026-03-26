import asyncio
import datetime
import os
import threading
import tkinter as tk
from tkinter import scrolledtext

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
            response = client.responses.create(
                model="gpt-5.4-mini",
                input=[
                    {"role": "developer", "content": "You are a helpful assistant."},
                    {"role": "user", "content": user_content},
                ],
            )
            await message.reply(response.output_text)
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


async def synthesize_and_play(text: str, target_channel_id: int):
    if not text:
        raise RuntimeError("テキストが空です。")

    channel = bot.get_channel(target_channel_id)
    if not isinstance(channel, discord.VoiceChannel):
        raise RuntimeError(f"指定したチャンネルID {target_channel_id} はボイスチャンネルではありません。")

    await asyncio.to_thread(save_wave, text, output_path=AUDIOFILE, params=VoiceParams(speed=55))
    await play_audio_file_in_channel(channel, AUDIOFILE)


@bot.command()
async def create_event(ctx, event_name: str, date_str: str):
    try:
        current_year = datetime.datetime.now().year
        full_date_str = f"{current_year}-{date_str}"
        event_date = datetime.datetime.strptime(full_date_str, "%Y-%m-%d").date()

        start_time_jst = datetime.datetime.combine(event_date, datetime.time(22, 0, 0))
        start_time = start_time_jst - datetime.timedelta(hours=9)
        start_time = start_time.replace(tzinfo=datetime.timezone.utc)
        end_time = start_time + datetime.timedelta(hours=2)

        event = await ctx.guild.create_scheduled_event(
            name=event_name,
            description="Python Bot による自動作成イベント",
            start_time=start_time,
            end_time=end_time,
            location="Gaia DC",
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=discord.EntityType.external,
        )

        await ctx.send(
            f"イベントを作成しました: {event.name} (開始: {start_time_jst.strftime('%Y-%m-%d %H:%M JST')}, 終了: {(start_time_jst + datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M JST')})"
        )
    except ValueError:
        await ctx.send("日付の形式が正しくありません。MM-DD 形式で指定してください。")


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
    root = tk.Tk()
    root.title("Discord Bot Controller")
    root.geometry("560x340")

    title = tk.Label(root, text="読み上げテキスト", anchor="w")
    title.pack(fill="x", padx=12, pady=(12, 4))

    input_box = scrolledtext.ScrolledText(root, wrap=tk.WORD, height=10)
    input_box.pack(fill="both", expand=True, padx=12)
    input_box.insert("1.0", "ここに読み上げたいテキストを入力してください。")

    status_var = tk.StringVar(value="Bot起動中...")
    status = tk.Label(root, textvariable=status_var, anchor="w")
    status.pack(fill="x", padx=12, pady=(8, 4))

    button_frame = tk.Frame(root)
    button_frame.pack(fill="x", padx=12, pady=(0, 12))

    play_button = tk.Button(button_frame, text="再生")
    play_button.pack(side="left", fill="x", expand=True)

    leave_button = tk.Button(button_frame, text="VC退室")
    leave_button.pack(side="left", fill="x", expand=True, padx=(8, 0))

    def update_ui(message: str, enable_play: bool = True, enable_leave: bool = True):
        status_var.set(message)
        play_button.config(state=tk.NORMAL if enable_play else tk.DISABLED)
        leave_button.config(state=tk.NORMAL if enable_leave else tk.DISABLED)

    def on_play_click():
        text = input_box.get("1.0", tk.END).strip()
        if not text:
            update_ui("テキストを入力してください。")
            return

        if not bot_ready_event.is_set():
            update_ui("Botの接続完了を待っています。")
            return

        update_ui("音声を生成して再生中です...", enable_play=False, enable_leave=False)
        future = asyncio.run_coroutine_threadsafe(synthesize_and_play(text, VOICE_CHANNEL_ID), bot.loop)

        def done_callback(done_future):
            try:
                done_future.result()
                root.after(0, lambda: update_ui("再生が完了しました。"))
            except Exception as e:
                root.after(0, lambda: update_ui(f"再生に失敗しました: {e}"))

        future.add_done_callback(done_callback)

    def on_leave_click():
        if not bot_ready_event.is_set():
            update_ui("Botの接続完了を待っています。")
            return

        update_ui("VCから退室しています...", enable_play=False, enable_leave=False)
        future = asyncio.run_coroutine_threadsafe(leave_voice_channel(VOICE_CHANNEL_ID), bot.loop)

        def done_callback(done_future):
            try:
                disconnected = done_future.result()
                if disconnected:
                    root.after(0, lambda: update_ui("VCから退室しました。"))
                else:
                    root.after(0, lambda: update_ui("BotはVCに接続していません。"))
            except Exception as e:
                root.after(0, lambda: update_ui(f"VC退室に失敗しました: {e}"))

        future.add_done_callback(done_callback)

    def on_close():
        if bot_ready_event.is_set() and not bot.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(bot.close(), bot.loop).result(timeout=5)
            except Exception as e:
                print(f"[WARN] bot.close failed: {e}")
        root.destroy()

    play_button.config(command=on_play_click)
    leave_button.config(command=on_leave_click)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


def start_bot_in_background():
    def run_bot():
        bot.run(TOKEN)

    thread = threading.Thread(target=run_bot, daemon=True)
    thread.start()
    return thread


def main():
    start_cevio()
    start_bot_in_background()
    build_gui()


if __name__ == "__main__":
    main()

