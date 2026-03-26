import discord
from discord.ext import commands
import datetime
import asyncio
import os
import openai
from dotenv import load_dotenv

from cevio_tts import VoiceParams, get_available_casts, save_wave, speak, start_cevio

start_cevio()
used_cast = speak("Hello from CeVIO library API.", params=VoiceParams(speed=55))
print(f"[INFO] Spoke with cast: {used_cast}")

out = save_wave("こんばんは、テストです。", output_path="test.wav", params=VoiceParams(speed=55), )

AUDIOFILE = "test.wav"

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")
if not OPENAI_API_KEY:
    raise RuntimeError("環境変数 OPENAI_API_KEY が未設定です。")

intents = discord.Intents.default()
intents.message_content = True  # コマンドを読み取るために必須
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)

# OpenAI Client setup
client = openai.OpenAI(api_key=OPENAI_API_KEY)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # メンションされた場合の処理
    if bot.user in message.mentions:
        try:
            # ユーザーのメッセージ内容を取得（メンションを除去）
            user_content = message.content.replace(f'<@{bot.user.id}>', '').strip()

            # OpenAI APIを呼び出し
            response = client.responses.create(
                model="gpt-5.4-mini",
                input=[
                    {"role": "developer", "content": "You are a helpful assistant."},
                    {"role": "user", "content": user_content}
                ]
            )

            answer = response.output_text
            await message.reply(answer)

        except Exception as e:
            print(f"Error: {e}")
            await message.channel.send(f"エラーが発生しました: {e}")

    # コマンドを処理するために必要
    await bot.process_commands(message)


@bot.command()
async def create_event(ctx, event_name: str, date_str: str):
    try:
        # 現在の年を取得
        current_year = datetime.datetime.now().year

        # 日付をパース (MM-DD 形式で現在の年を使用)
        full_date_str = f"{current_year}-{date_str}"
        event_date = datetime.datetime.strptime(full_date_str, "%Y-%m-%d").date()

        # 指定日の22:00 JST を開始時刻に設定 (naive datetime)
        start_time_jst = datetime.datetime.combine(event_date, datetime.time(22, 0, 0))

        # JST to UTC (JST is UTC+9)
        start_time = start_time_jst - datetime.timedelta(hours=9)
        start_time = start_time.replace(tzinfo=datetime.timezone.utc)

        # 終了時刻は24:00 JST (2時間後)
        end_time = start_time + datetime.timedelta(hours=2)

        guild = ctx.guild

        event = await guild.create_scheduled_event(
            name=event_name,
            description="Python Bot による自動作成イベント",
            start_time=start_time,
            end_time=end_time,
            location="Gaia DC",
            privacy_level=discord.PrivacyLevel.guild_only,
            entity_type=discord.EntityType.external
        )

        await ctx.send(
            f"イベントを作成しました: {event.name} (開始: {start_time_jst.strftime('%Y-%m-%d %H:%M JST')}, 終了: {(start_time_jst + datetime.timedelta(hours=2)).strftime('%Y-%m-%d %H:%M JST')})")
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

    target_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    if voice_client is None:
        voice_client = await target_channel.connect()
    elif voice_client.channel != target_channel:
        await voice_client.move_to(target_channel)

    if voice_client.is_playing():
        await ctx.send("現在ほかの音声を再生中です。終わってから再実行してください。")
        return

    loop = bot.loop

    def after_playback(error):
        if error:
            print(f"Playback error: {error}")
        future = voice_client.disconnect()
        loop.call_soon_threadsafe(asyncio.create_task, future)

    try:
        source = discord.FFmpegPCMAudio(AUDIOFILE,
                                        executable="C:\\Users\\amano\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe")
        voice_client.play(source, after=after_playback)
        await ctx.send(f"{target_channel.mention} で `{AUDIOFILE}` を再生します。")
    except Exception as e:
        await ctx.send(f"再生に失敗しました: {e}")


bot.run(TOKEN)
