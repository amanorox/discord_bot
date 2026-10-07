import argparse
import asyncio
import datetime
import hashlib
import json
import os
import threading
import time
import traceback
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
from agents import Agent, MaxTurnsExceeded, Runner, function_tool, set_default_openai_key
import uvicorn
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from tts_backend import VoiceParams, get_backend
from web_api import WebController

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN:
    raise RuntimeError("環境変数 DISCORD_BOT_TOKEN が未設定です。")
if not OPENAI_API_KEY:
    raise RuntimeError("環境変数 OPENAI_API_KEY が未設定です。")

AUDIOFILE = "test.wav"
WAV_DIR = Path(__file__).parent / "wav"
TTS_PARAMS = VoiceParams(speed=55)


def wav_path_for(text: str) -> Path:
    """テキスト・TTSバックエンド・パラメータから決まるキャッシュ wav パス。"""
    key = json.dumps(
        [get_backend().cache_id(), text, vars(TTS_PARAMS)],
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return WAV_DIR / (hashlib.sha256(key.encode("utf-8")).hexdigest()[:24] + ".wav")


VOICE_CHANNEL_ID = int(os.getenv("VOICE_CHANNEL_ID", "1482359368992161918"))
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
DEFAULT_FFMPEG = "C:\\Users\\amano\\AppData\\Local\\Microsoft\\WinGet\\Links\\ffmpeg.exe"
FFMPEG_EXECUTABLE = os.getenv("FFMPEG_PATH") or (DEFAULT_FFMPEG if os.path.exists(DEFAULT_FFMPEG) else "ffmpeg")
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents)
set_default_openai_key(OPENAI_API_KEY)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")

bot_ready_event = threading.Event()
MAX_REPLY_CHAIN_MESSAGES = 10
MAX_REPLY_MESSAGE_CHARS = 5000
MAX_TOOL_CALL_ROUNDS = 5

# ---------- Lightweight persistent scheduling polls ----------
JST = ZoneInfo("Asia/Tokyo")
POLL_STORE = Path(__file__).with_name("schedule_polls.json")
schedule_polls: dict[str, dict] = {}


def save_schedule_polls() -> None:
    """Persist poll state atomically so polls survive a process restart."""
    temporary = POLL_STORE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(schedule_polls, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, POLL_STORE)


def load_schedule_polls() -> None:
    global schedule_polls
    try:
        schedule_polls = json.loads(POLL_STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        schedule_polls = {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[WARN] 日程調整データを読み込めません: {exc}")
        schedule_polls = {}


def poll_is_open(poll: dict) -> bool:
    deadline = poll.get("deadline")
    return not poll.get("closed", False) and (not deadline or datetime.datetime.now(datetime.timezone.utc) < datetime.datetime.fromisoformat(deadline))


def poll_embed(poll: dict, *, results: bool = False) -> discord.Embed:
    candidates = poll["candidates"]
    responses = poll.get("responses", {})
    embed = discord.Embed(title=f"📅 {poll['title']}", color=0x5865F2)
    embed.description = "候補日時（日本時間）\n" + "\n".join(
        f"{i + 1}. {datetime.datetime.fromisoformat(value).astimezone(JST).strftime('%Y/%m/%d (%a) %H:%M')}"
        for i, value in enumerate(candidates)
    )
    if poll.get("deadline"):
        embed.description += "\n\n締切: " + datetime.datetime.fromisoformat(poll["deadline"]).astimezone(JST).strftime("%Y/%m/%d %H:%M JST")
    counts = []
    for i in range(len(candidates)):
        answers = [values[i] for values in responses.values() if i < len(values)]
        counts.append(f"{i + 1}: ○ {answers.count('○')}　△ {answers.count('△')}　× {answers.count('×')}")
    embed.add_field(name="回答状況", value="\n".join(counts) or "回答なし", inline=False)
    embed.set_footer(text=f"回答者 {len(responses)} 人 ・ {('締切済み' if not poll_is_open(poll) else '回答受付中')}")
    if responses:
        rows = [f"<@{user_id}>: " + " ".join(values) for user_id, values in responses.items()]
        # Embed field values are limited to 1024 chars. The public result action
        # below provides the complete list when it does not fit in this summary.
        visible_rows = []
        for row in rows:
            addition = row if not visible_rows else "\n" + row
            if len("".join(visible_rows) + addition) > 1000:
                break
            visible_rows.append(addition)
        value = "".join(visible_rows)
        omitted = len(rows) - len(visible_rows)
        if omitted:
            value += f"\nほか {omitted} 人（「結果を見る」で全員分を表示）"
        embed.add_field(name="回答者別の回答", value=value or "回答あり", inline=False)
    return embed


class CandidateAnswerButton(discord.ui.Button):
    def __init__(self, answer_view: "PollAnswerView", index: int, choice: str, label: str, style: discord.ButtonStyle, row: int):
        selected = answer_view.values[index] == choice
        super().__init__(label=("✓ " if selected else "") + label, style=style, row=row)
        self.answer_view = answer_view
        self.index = index
        self.choice = choice

    async def callback(self, interaction: discord.Interaction):
        self.answer_view.values[self.index] = self.choice
        self.answer_view.rebuild()
        await interaction.response.edit_message(content=self.answer_view.prompt(), view=self.answer_view)


class PollAnswerNavButton(discord.ui.Button):
    def __init__(self, answer_view: "PollAnswerView", action: str, label: str, style: discord.ButtonStyle, row: int = 4):
        super().__init__(label=label, style=style, row=row)
        self.answer_view = answer_view
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        view = self.answer_view
        if self.action == "save":
            poll = schedule_polls.get(view.poll_id)
            if not poll or not poll_is_open(poll):
                await interaction.response.edit_message(content="この日程調整は締切済みです。", view=None)
                return
            if any(value not in {"○", "△", "×"} for value in view.values):
                await interaction.response.send_message("未回答の候補があります。各候補で ○ / △ / × を選んでください。", ephemeral=True)
                return
            poll.setdefault("responses", {})[str(interaction.user.id)] = list(view.values)
            save_schedule_polls()
            await interaction.response.edit_message(content="回答を保存しました。変更する場合は「回答する」から再度入力できます。", view=None)
            await refresh_poll_message(view.poll_id)
            return
        view.page += -1 if self.action == "prev" else 1
        view.rebuild()
        await interaction.response.edit_message(content=view.prompt(), view=view)


class PollAnswerView(discord.ui.View):
    """Private, Chouseisan-like date rows with three clear choice buttons."""
    PAGE_SIZE = 3

    def __init__(self, poll_id: str, user_id: int):
        super().__init__(timeout=900)
        self.poll_id = poll_id
        self.user_id = user_id
        poll = schedule_polls[poll_id]
        saved = poll.get("responses", {}).get(str(user_id), [])
        self.values = [saved[i] if i < len(saved) and saved[i] in {"○", "△", "×"} else "" for i in range(len(poll["candidates"]))]
        self.page = 0
        self.rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("この回答画面は開いた本人だけが操作できます。", ephemeral=True)
            return False
        return True

    def prompt(self) -> str:
        count = len(schedule_polls[self.poll_id]["candidates"])
        pages = (count + self.PAGE_SIZE - 1) // self.PAGE_SIZE
        poll = schedule_polls[self.poll_id]
        start = self.page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, count)
        dates = "\n".join(
            f"**{i + 1}. {datetime.datetime.fromisoformat(poll['candidates'][i]).astimezone(JST).strftime('%Y/%m/%d (%a) %H:%M')}**"
            for i in range(start, end)
        )
        return f"**予定を入力してください**　({self.page + 1}/{pages})\n{dates}\n\n各候補のボタンから回答を選択してください。選択済みには ✓ が付きます。"

    def rebuild(self):
        self.clear_items()
        poll = schedule_polls[self.poll_id]
        start = self.page * self.PAGE_SIZE
        end = min(start + self.PAGE_SIZE, len(poll["candidates"]))
        for index in range(start, end):
            row = index - start
            self.add_item(CandidateAnswerButton(self, index, "○", "🟢 ○ 参加", discord.ButtonStyle.success, row))
            self.add_item(CandidateAnswerButton(self, index, "△", "🟡 △ 未定", discord.ButtonStyle.primary, row))
            self.add_item(CandidateAnswerButton(self, index, "×", "🔴 × 不参加", discord.ButtonStyle.danger, row))
        if self.page > 0:
            self.add_item(PollAnswerNavButton(self, "prev", "前へ", discord.ButtonStyle.secondary))
        if end < len(poll["candidates"]):
            self.add_item(PollAnswerNavButton(self, "next", "次へ", discord.ButtonStyle.secondary))
        else:
            self.add_item(PollAnswerNavButton(self, "save", "回答を保存", discord.ButtonStyle.success))


class PollCreateModal(discord.ui.Modal, title="日程調整を作成"):
    title_input = discord.ui.TextInput(label="イベント名", max_length=100)
    candidates_input = discord.ui.TextInput(label="候補日時（1行に1件、YYYY-MM-DD HH:MM）", style=discord.TextStyle.paragraph, placeholder="2026-10-10 21:00\n2026-10-11 21:00", max_length=1000)
    deadline_input = discord.ui.TextInput(label="締切（任意、YYYY-MM-DD HH:MM）", required=False, placeholder="2026-10-09 23:00", max_length=16)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dates = [datetime.datetime.strptime(line.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=JST) for line in self.candidates_input.value.splitlines() if line.strip()]
            if not 2 <= len(dates) <= 20 or len(set(dates)) != len(dates):
                raise ValueError("候補日時は重複なしで2〜20件入力してください。")
            if any(value <= datetime.datetime.now(JST) for value in dates):
                raise ValueError("候補日時は現在より後の日時にしてください。")
            deadline = None
            if self.deadline_input.value.strip():
                deadline_dt = datetime.datetime.strptime(self.deadline_input.value.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=JST)
                if deadline_dt <= datetime.datetime.now(JST):
                    raise ValueError("締切は現在より後にしてください。")
                deadline = deadline_dt.astimezone(datetime.timezone.utc).isoformat()
        except ValueError as exc:
            await interaction.response.send_message(f"入力を確認してください: {exc}", ephemeral=True)
            return
        poll_id = hashlib.sha1(f"{interaction.guild_id}:{interaction.id}".encode()).hexdigest()[:12]
        poll = {"id": poll_id, "guild_id": interaction.guild_id, "channel_id": interaction.channel_id,
                "message_id": None, "title": self.title_input.value, "creator_id": interaction.user.id,
                "candidates": [value.astimezone(datetime.timezone.utc).isoformat() for value in dates],
                "deadline": deadline, "closed": False, "responses": {}}
        schedule_polls[poll_id] = poll
        message = await interaction.channel.send(embed=poll_embed(poll), view=PollView(poll_id))
        poll["message_id"] = message.id
        save_schedule_polls()
        await interaction.response.send_message("日程調整を作成しました。", ephemeral=True)


class PollCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="候補日時を入力", style=discord.ButtonStyle.primary)
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PollCreateModal())


class PollView(discord.ui.View):
    def __init__(self, poll_id: str):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        for button in self.children:
            button.custom_id = f"schedule:{poll_id}:{button.label}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        poll = schedule_polls.get(self.poll_id)
        if not poll:
            await interaction.response.send_message("日程調整が見つかりません。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="回答する", style=discord.ButtonStyle.success)
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll = schedule_polls[self.poll_id]
        if not poll_is_open(poll):
            await interaction.response.send_message("この日程調整は締切済みです。", ephemeral=True)
            return
        view = PollAnswerView(self.poll_id, interaction.user.id)
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)

    @discord.ui.button(label="結果を見る", style=discord.ButtonStyle.secondary)
    async def results(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll = schedule_polls[self.poll_id]
        if not poll.get("responses"):
            await interaction.response.send_message("まだ回答はありません。", ephemeral=True)
            return
        responses = list(poll["responses"].items())
        result_embeds = []
        candidates = poll["candidates"]
        for candidate_start in range(0, len(candidates), 3):
            candidate_indexes = range(candidate_start, min(candidate_start + 3, len(candidates)))
            for user_start in range(0, len(responses), 20):
                user_batch = responses[user_start:user_start + 20]
                embed = discord.Embed(
                    title=f"📊 {poll['title']} — 回答一覧",
                    description=f"回答者 {user_start + 1}〜{user_start + len(user_batch)} 人目",
                    color=0x5865F2,
                )
                for index in candidate_indexes:
                    date_label = datetime.datetime.fromisoformat(candidates[index]).astimezone(JST).strftime("%m/%d (%a) %H:%M")
                    values = [answers[index] if index < len(answers) else "—" for _, answers in user_batch]
                    field_value = "\n".join(f"<@{user_id}>　{value}" for (user_id, _), value in zip(user_batch, values)) or "回答なし"
                    embed.add_field(name=date_label, value=field_value, inline=True)
                result_embeds.append(embed)
        await interaction.response.send_message(embed=result_embeds[0], ephemeral=False)
        for embed in result_embeds[1:]:
            await interaction.followup.send(embed=embed, ephemeral=False)

    @discord.ui.button(label="締め切る", style=discord.ButtonStyle.danger)
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        poll = schedule_polls[self.poll_id]
        if interaction.user.id != poll["creator_id"] and not interaction.user.guild_permissions.manage_events:
            await interaction.response.send_message("作成者またはイベント管理権限を持つユーザーのみ締め切れます。", ephemeral=True)
            return
        poll["closed"] = True
        save_schedule_polls()
        await interaction.response.edit_message(embed=poll_embed(poll), view=self)


async def refresh_poll_message(poll_id: str) -> None:
    poll = schedule_polls.get(poll_id)
    if not poll or not poll.get("message_id"):
        return
    channel = bot.get_channel(poll["channel_id"])
    if channel is None:
        return
    try:
        message = await channel.fetch_message(poll["message_id"])
        await message.edit(embed=poll_embed(poll), view=PollView(poll_id))
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass

# ---------- Web controller state ----------
web_playback_state: dict = {"future": None}

# ---------- OpenAI Agents SDK tools ----------


@function_tool
async def get_webpage(url: str) -> str:
    """Fetch and extract readable main text from a webpage. Webページの本文テキストを取得します。

    Args:
        url: Target page URL (http/https).
    """
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


@function_tool
async def list_all_event() -> str:
    """Get all scheduled events. 作成済みのイベント一覧を取得します。"""
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


chat_agent = Agent(
    name="Tranquility Bot",
    instructions="You are a Discord bot for Final Fantasy XIV Guild Tranquility.",
    model=OPENAI_MODEL,
    tools=[list_all_event, get_webpage],
)
def build_message_text_for_openai(message: discord.Message) -> str:
    text = message.content.strip()
    if not text:
        return ""
    if len(text) > MAX_REPLY_MESSAGE_CHARS:
        text = text[:MAX_REPLY_MESSAGE_CHARS] + "..."
    return text


def split_text_for_discord(text: str, limit: int = 1900) -> list[str]:
    """Split text into chunks <= limit, preferring newline boundaries."""
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip() or not chunks:
        chunks.append(remaining)
    return [c for c in chunks if c.strip()] or [text[:limit] or "回答を生成できませんでした。"]


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
    load_schedule_polls()
    for poll_id, poll in schedule_polls.items():
        if poll.get("message_id"):
            bot.add_view(PollView(poll_id), message_id=poll["message_id"])
    try:
        await bot.tree.sync()
    except discord.HTTPException as exc:
        print(f"[WARN] スラッシュコマンドを同期できません: {exc}")
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
            openai_input: list[dict] = []

            for chain_message in chain_messages:
                chain_text = build_message_text_for_openai(chain_message)
                if not chain_text:
                    continue
                role = "assistant" if bot.user is not None and chain_message.author.id == bot.user.id else "user"
                openai_input.append({"role": role, "content": chain_text})

            openai_input.append({"role": "user", "content": user_content})
            try:
                result = await Runner.run(chat_agent, openai_input, max_turns=MAX_TOOL_CALL_ROUNDS + 1)
            except MaxTurnsExceeded:
                await message.reply("ツール呼び出しの上限に達したため、処理を中断しました。")
                return
            reply_text = str(result.final_output or "").strip() or "回答を生成できませんでした。"
            chunks = split_text_for_discord(reply_text)
            await message.reply(chunks[0])
            for chunk in chunks[1:]:
                await message.channel.send(chunk)
        except Exception as e:
            traceback.print_exc()
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

    await asyncio.to_thread(build_wavs, [(0, text)])
    await play_audio_file_in_channel(channel, str(wav_path_for(text)))


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
        if not wav_path_for(text).exists():
            raise RuntimeError(f"音声が未ビルドです。先に「ビルド」を実行してください: {text}")

    for elapsed_seconds, text in timed_lines:
        remaining = origin_time + elapsed_seconds - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        await play_audio_file_in_channel(channel, str(wav_path_for(text)))


def build_wavs(timed_lines: list[tuple[int, str]], progress=None) -> tuple[int, int]:
    """未生成の音声のみ TTS で合成し wav/ に保存する。(生成数, スキップ数) を返す。"""
    WAV_DIR.mkdir(exist_ok=True)
    backend = get_backend()
    texts = list(dict.fromkeys(text for _, text in timed_lines))
    built = skipped = 0
    for i, text in enumerate(texts, start=1):
        path = wav_path_for(text)
        if path.exists():
            skipped += 1
        else:
            tmp = path.with_suffix(".tmp.wav")
            backend.save_wave(text, tmp, params=TTS_PARAMS)
            os.replace(tmp, path)
            built += 1
        if progress:
            progress(f"ビルド中... {i}/{len(texts)}")
    return built, skipped


web_controller = WebController(
    bot=bot,
    bot_ready_event=bot_ready_event,
    default_channel_id=VOICE_CHANNEL_ID,
    static_dir=Path(__file__).parent / "static",
    playback_state=web_playback_state,
    parse_timed_lines=parse_timed_lines,
    build_wavs=build_wavs,
    wav_path_for=wav_path_for,
    synthesize_and_play_timeline=synthesize_and_play_timeline,
    stop_current_playback=stop_current_playback,
    leave_voice_channel=leave_voice_channel,
)
app = web_controller.app
_schedule_broadcast = web_controller.schedule_broadcast


# ---------- Discord commands ----------
@bot.tree.command(name="schedule", description="Discord内で日程調整を作成します")
async def schedule_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        "日程調整の候補日時を入力してください。日時は日本時間です。",
        view=PollCreateView(), ephemeral=True,
    )


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
