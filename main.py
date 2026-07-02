import os
import sys
import json
import time
import signal
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import uvicorn
import uuid
import sqlalchemy
from databases import Database

# Загружаем переменные из .env файла (если он существует локально)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID")) if os.getenv("GUILD_ID") else None
PORT = int(os.getenv("PORT", 8000))
SITE_URL = os.getenv("SITE_URL", f"http://localhost:{PORT}")
if not SITE_URL.startswith(("http://", "https://")):
    SITE_URL = "https://" + SITE_URL
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Temirlan029.")

# ── Настройки базы данных ──
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./ushunet.db")
# Фикс для Railway: SQLAlchemy ожидает postgresql://, а не postgres://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

database = Database(DATABASE_URL)
metadata = sqlalchemy.MetaData()

comments = sqlalchemy.Table(
    "comments",
    metadata,
    sqlalchemy.Column("id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("user_id", sqlalchemy.String, index=True),
    sqlalchemy.Column("text", sqlalchemy.String),
    sqlalchemy.Column("timestamp", sqlalchemy.String),
    sqlalchemy.Column("target_name", sqlalchemy.String),
)

bios_table = sqlalchemy.Table(
    "bios",
    metadata,
    sqlalchemy.Column("user_id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("text", sqlalchemy.String),
)

# ── Антиспам настройки ──
RATE_LIMIT_SECONDS = 120   # 1 сообщение раз в 2 минуты
MAX_MESSAGE_LENGTH = 500   # Максимум символов

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # Выбираем правильный драйвер в зависимости от типа БД
    if "postgresql" in DATABASE_URL or "postgres" in DATABASE_URL:
        # Для PostgreSQL используем asyncpg напрямую через databases
        await database.connect()
    else:
        # Для SQLite используем SQLAlchemy
        engine = sqlalchemy.create_engine(
            DATABASE_URL, connect_args={"check_same_thread": False}
        )
        metadata.create_all(engine)
        await database.connect()
    yield
    # Shutdown
    await database.disconnect()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Хранилище сообщений и рейт-лимитер ──
rate_limiter: dict[str, float] = {}  # IP -> timestamp последнего сообщения


class MessageIn(BaseModel):
    text: str = Field(..., min_length=2, max_length=MAX_MESSAGE_LENGTH)


# ══════════════════════════════════════════════════════════════
#  КНОПКИ (Views)
# ══════════════════════════════════════════════════════════════

class MembersView(discord.ui.View):
    """Кнопки под командой /members"""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=120)
        self.guild = guild
        # Кнопка-ссылка на сайт (не требует callback)
        self.add_item(discord.ui.Button(
            label="🌐 Открыть сайт",
            style=discord.ButtonStyle.link,
            url=SITE_URL,
        ))

    @discord.ui.button(label="🔄 Обновить", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = build_members_embed(self.guild)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="📊 Подробная статистика", style=discord.ButtonStyle.secondary)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = self.guild
        humans = [m for m in guild.members if not m.bot]
        bots = [m for m in guild.members if m.bot]
        online = [m for m in humans if m.status != discord.Status.offline]

        embed = discord.Embed(
            title="📊 Подробная статистика",
            color=0x5865F2,
        )
        embed.add_field(name="👥 Людей", value=f"```{len(humans)}```", inline=True)
        embed.add_field(name="🤖 Ботов", value=f"```{len(bots)}```", inline=True)
        embed.add_field(name="🟢 Онлайн", value=f"```{len(online)}```", inline=True)
        embed.add_field(name="📁 Каналов", value=f"```{len(guild.channels)}```", inline=True)
        embed.add_field(name="🎭 Ролей", value=f"```{len(guild.roles) - 1}```", inline=True)
        embed.add_field(name="😀 Эмодзи", value=f"```{len(guild.emojis)}```", inline=True)
        embed.set_footer(text="Нажмите «Обновить» для актуальных данных")

        await interaction.response.edit_message(embed=embed, view=self)


class AvatarView(discord.ui.View):
    """Кнопки под командой /avatar"""

    def __init__(self, target: discord.Member):
        super().__init__(timeout=60)
        self.target = target
        # Кнопка-ссылка для скачивания полного аватара
        self.add_item(discord.ui.Button(
            label="⬇️ Скачать оригинал",
            style=discord.ButtonStyle.link,
            url=str(target.display_avatar.replace(size=4096)),
        ))

    @discord.ui.button(label="🖼️ Серверный аватар", style=discord.ButtonStyle.secondary)
    async def guild_avatar(self, interaction: discord.Interaction, button: discord.ui.Button):
        avatar = self.target.guild_avatar
        if avatar:
            embed = discord.Embed(title=f"Серверный аватар — {self.target.display_name}", color=0x5865F2)
            embed.set_image(url=avatar.replace(size=1024))
        else:
            embed = discord.Embed(
                title="❌ Нет серверного аватара",
                description=f"У **{self.target.display_name}** не установлен отдельный серверный аватар.",
                color=0xED4245,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ServerInfoView(discord.ui.View):
    """Кнопки под командой /serverinfo"""

    def __init__(self, guild: discord.Guild):
        super().__init__(timeout=120)
        self.guild = guild
        self.add_item(discord.ui.Button(
            label="🌐 Открыть сайт",
            style=discord.ButtonStyle.link,
            url=SITE_URL,
        ))

    @discord.ui.button(label="🎭 Список ролей", style=discord.ButtonStyle.secondary)
    async def show_roles(self, interaction: discord.Interaction, button: discord.ui.Button):
        roles = [r for r in self.guild.roles if r.name != "@everyone"]
        roles.sort(key=lambda r: r.position, reverse=True)
        role_list = ", ".join(r.mention for r in roles[:25])
        if len(roles) > 25:
            role_list += f"\n…и ещё {len(roles) - 25}"

        embed = discord.Embed(
            title=f"🎭 Роли сервера ({len(roles)})",
            description=role_list or "Нет ролей",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  ХЕЛПЕРЫ
# ══════════════════════════════════════════════════════════════

def build_members_embed(guild: discord.Guild) -> discord.Embed:
    """Создает Embed со списком участников"""
    humans = [m for m in guild.members if not m.bot]
    online = [m for m in humans if m.status != discord.Status.offline]

    # Берём первых 15 для компактного отображения
    display = humans[:15]
    member_lines = []
    for i, m in enumerate(display, 1):
        status_emoji = {
            discord.Status.online: "🟢",
            discord.Status.idle: "🌙",
            discord.Status.dnd: "⛔",
        }.get(m.status, "⚫")
        member_lines.append(f"{status_emoji} **{m.display_name}**")

    description = "\n".join(member_lines)
    if len(humans) > 15:
        description += f"\n\n… и ещё **{len(humans) - 15}** участников"

    embed = discord.Embed(
        title=f"👥 Участники — {guild.name}",
        description=description,
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.set_thumbnail(url=guild.icon.url if guild.icon else None)
    embed.set_footer(text=f"Всего: {len(humans)} • Онлайн: {len(online)}")
    return embed


# ══════════════════════════════════════════════════════════════
#  СЛЭШ-КОМАНДЫ
# ══════════════════════════════════════════════════════════════

@bot.event
async def on_ready():
    print(f"✅ Бот {bot.user.name} подключен к Discord!")
    # Синхронизация слэш-команд с вашим сервером
    if GUILD_ID:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
        print(f"⚡ Слэш-команды синхронизированы для сервера {GUILD_ID}")
    else:
        await bot.tree.sync()
        print("⚡ Слэш-команды синхронизированы глобально")


@bot.tree.command(name="members", description="Показать список участников сервера")
async def cmd_members(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return

    embed = build_members_embed(guild)
    view = MembersView(guild)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="avatar", description="Посмотреть аватар пользователя")
@app_commands.describe(user="Выберите пользователя (или оставьте пустым для своего аватара)")
async def cmd_avatar(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    embed = discord.Embed(
        title=f"🖼️ Аватар — {target.display_name}",
        color=0x5865F2,
    )
    embed.set_image(url=target.display_avatar.replace(size=1024))
    embed.set_footer(text=f"Запросил: {interaction.user.display_name}")

    view = AvatarView(target)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="serverinfo", description="Информация о сервере")
async def cmd_serverinfo(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("❌ Команда работает только на сервере.", ephemeral=True)
        return

    humans = len([m for m in guild.members if not m.bot])
    bots = len([m for m in guild.members if m.bot])

    embed = discord.Embed(
        title=f"ℹ️ {guild.name}",
        color=0x5865F2,
        timestamp=guild.created_at,
    )
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    if guild.banner:
        embed.set_image(url=guild.banner.url)

    embed.add_field(name="👑 Владелец", value=guild.owner.mention if guild.owner else "—", inline=True)
    embed.add_field(name="👥 Участники", value=f"```{humans} людей • {bots} ботов```", inline=False)
    embed.add_field(name="📁 Каналы", value=f"💬 {len(guild.text_channels)} текст. • 🔊 {len(guild.voice_channels)} голос.", inline=True)
    embed.add_field(name="🎭 Ролей", value=f"{len(guild.roles) - 1}", inline=True)
    embed.add_field(name="🔒 Уровень верификации", value=str(guild.verification_level).capitalize(), inline=True)
    embed.set_footer(text="Сервер создан")

    view = ServerInfoView(guild)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="ping", description="Проверить задержку бота")
async def cmd_ping(interaction: discord.Interaction):
    latency_ms = round(bot.latency * 1000)

    if latency_ms < 100:
        color, status = 0x43B581, "🟢 Отличная"
    elif latency_ms < 200:
        color, status = 0xFAA61A, "🟡 Нормальная"
    else:
        color, status = 0xED4245, "🔴 Высокая"

    embed = discord.Embed(
        title="🏓 Понг!",
        description=f"**Задержка:** `{latency_ms}мс`\n**Связь:** {status}",
        color=color,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="bio", description="Рассказать о себе (появится в анкете на сайте)")
@app_commands.describe(text="Ваше описание (оставьте пустым, чтобы удалить)")
async def cmd_bio(interaction: discord.Interaction, text: str = None):
    user_id = str(interaction.user.id)
    if not text:
        query = bios_table.delete().where(bios_table.c.user_id == user_id)
        await database.execute(query)
        await interaction.response.send_message("✅ Ваше описание удалено.", ephemeral=True)
    else:
        text = text.strip()[:200]
        query_check = bios_table.select().where(bios_table.c.user_id == user_id)
        row = await database.fetch_one(query_check)
        if row:
            query_update = bios_table.update().where(bios_table.c.user_id == user_id).values(text=text)
            await database.execute(query_update)
        else:
            query_insert = bios_table.insert().values(user_id=user_id, text=text)
            await database.execute(query_insert)
        await interaction.response.send_message(f"✅ Описание обновлено:\n`{text}`", ephemeral=True)


# ══════════════════════════════════════════════════════════════
#  FastAPI ЭНДПОИНТЫ
# ══════════════════════════════════════════════════════════════

@app.get("/api/members")
async def get_members():
    if not GUILD_ID:
        return {"error": "Переменная окружения GUILD_ID не настроена."}

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return {"error": f"Сервер с ID {GUILD_ID} не найден в кэше бота."}

    # Fetch all bios
    bios_query = bios_table.select()
    bios_rows = await database.fetch_all(bios_query)
    bios_dict = {row["user_id"]: row["text"] for row in bios_rows}
    
    # Fetch comment counts
    count_query = "SELECT user_id, COUNT(*) as cnt FROM comments GROUP BY user_id"
    count_rows = await database.fetch_all(count_query)
    count_dict = {row["user_id"]: row["cnt"] for row in count_rows}
    
    members_list = []
    
    for member in guild.members:
        if member.bot:
            continue
            
        status_str = str(member.status)
        # 0 = активен (online, idle, dnd), 1 = оффлайн (offline)
        sort_weight = 1 if status_str == "offline" else 0
        
        activity_str = None
        if member.activities:
            for act in member.activities:
                if act.type == discord.ActivityType.playing:
                    activity_str = f"🎮 Играет в {act.name}"
                    break
                elif act.type == discord.ActivityType.listening:
                    activity_str = f"🎵 Слушает {act.name}"
                    break
                elif act.type == discord.ActivityType.streaming:
                    activity_str = f"🔴 Стримит {act.name}"
                    break
            if not activity_str:
                activity_str = member.activities[0].name
                
        user_id = str(member.id)
        
        members_list.append({
            "id": user_id,
            "global_name": member.global_name or member.name,
            "server_name": member.display_name,
            "avatar_url": str(member.display_avatar.url),
            "joined_at": member.joined_at.isoformat() if member.joined_at else None,
            "roles": [r.name for r in member.roles if r.name != "@everyone"],
            "status": status_str,
            "activity": activity_str,
            "bio": bios_dict.get(user_id),
            "comment_count": count_dict.get(user_id, 0),
            "_weight": sort_weight
        })
        
    # Сортируем: сначала активные, затем по алфавиту
    members_list.sort(key=lambda m: (m["_weight"], m["server_name"].lower()))

    # Удаляем служебное поле _weight перед отправкой
    for m in members_list:
        m.pop("_weight", None)

    return {"total_members": len(members_list), "members": members_list}


# ── Анонимные комментарии к анкетам ──

@app.get("/api/comments/{user_id}")
async def get_comments(user_id: str):
    query = comments.select().where(comments.c.user_id == user_id).order_by(comments.c.timestamp.desc())
    rows = await database.fetch_all(query)
    return {"comments": [dict(row) for row in rows]}


@app.post("/api/comments/{user_id}")
async def post_comment(user_id: str, msg: MessageIn, request: Request):
    # Определяем IP клиента (за прокси Railway — через заголовок)
    client_ip = request.headers.get("x-forwarded-for", request.client.host)

    # Антиспам: проверяем рейт-лимит
    now = time.time()
    last_time = rate_limiter.get(client_ip, 0)
    wait_seconds = RATE_LIMIT_SECONDS - (now - last_time)

    if wait_seconds > 0:
        return {
            "error": f"Подождите ещё {int(wait_seconds)} сек. перед следующим сообщением.",
            "wait": int(wait_seconds),
        }

    # Очищаем текст от лишних пробелов
    text = msg.text.strip()
    if len(text) < 2:
        return {"error": "Комментарий слишком короткий."}

    # Сохраняем в базу данных
    comment_id = uuid.uuid4().hex[:6]
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    query = comments.insert().values(
        id=comment_id,
        user_id=user_id,
        text=text,
        timestamp=timestamp,
        target_name=None  # Будет обновлено при получении
    )
    await database.execute(query)

    # Обновляем рейт-лимит
    rate_limiter[client_ip] = now
    
    # Отправляем уведомление в ЛС Discord асинхронно
    target_user = bot.get_user(int(user_id))
    if target_user:
        async def send_dm():
            try:
                await target_user.send(f"👀 **Вам оставили новый анонимный отзыв!**\n\n💬 *«{text}»*\n\n🌐 Посмотреть на сайте: {SITE_URL}")
            except Exception:
                pass
        asyncio.create_task(send_dm())

    return {"ok": True, "cooldown": RATE_LIMIT_SECONDS, "id": comment_id}

@app.delete("/api/comments/{user_id}/{comment_id}")
async def delete_comment(user_id: str, comment_id: str, request: Request):
    auth = request.headers.get("Authorization")
    if auth != f"Bearer {ADMIN_PASSWORD}":
        return {"error": "Неверный пароль администратора."}
        
    query = comments.delete().where(
        (comments.c.user_id == user_id) & (comments.c.id == comment_id)
    )
    result = await database.execute(query)
    
    if result:
        return {"ok": True}
    return {"error": "Комментарий не найден."}

@app.get("/api/latest_comments")
async def get_latest_comments():
    if not GUILD_ID: return {"comments": []}
    guild = bot.get_guild(GUILD_ID)
    if not guild: return {"comments": []}
    
    # Получаем последние 10 комментариев из базы данных
    query = comments.select().order_by(comments.c.timestamp.desc()).limit(10)
    rows = await database.fetch_all(query)
    
    comments_list = []
    for row in rows:
        member = guild.get_member(int(row["user_id"]))
        if member:
            comment_dict = dict(row)
            comment_dict["target_name"] = member.display_name
            comments_list.append(comment_dict)
    
    return {"comments": comments_list}

@app.get("/", response_class=HTMLResponse)
async def get_index_page():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Файл index.html не найден на сервере</h1>"


# ══════════════════════════════════════════════════════════════
#  ГРАЦИОЗНОЕ ЗАВЕРШЕНИЕ (Ctrl+C → бот сразу уходит в оффлайн)
# ══════════════════════════════════════════════════════════════

async def shutdown(server: uvicorn.Server):
    """Корректно завершает бота и веб-сервер."""
    print("\n🔻 Завершение работы…")
    # Отправляем Discord'у команду отключения — бот сразу станет оффлайн
    if not bot.is_closed():
        print("   ↳ Отключение от Discord…")
        await bot.close()
        print("   ✅ Бот отключен от Discord (статус: оффлайн)")
    # Останавливаем веб-сервер
    server.should_exit = True
    print("   ✅ Веб-сервер остановлен")


async def main():
    if not BOT_TOKEN or not GUILD_ID:
        print("❌ КРИТИЧЕСКАЯ ОШИБКА: Проверьте переменные окружения BOT_TOKEN и GUILD_ID!")
        return

    config = uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info")
    server = uvicorn.Server(config)

    # Перехватываем Ctrl+C (SIGINT) и SIGTERM для корректного завершения
    loop = asyncio.get_running_loop()

    def _handle_signal():
        asyncio.ensure_future(shutdown(server))

    # На Windows signal.SIGTERM может не работать, поэтому ловим оба если можем
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler — используем fallback
            signal.signal(sig, lambda s, f: asyncio.ensure_future(shutdown(server)))

    print(f"🚀 Запуск бота и веб-сервера на порту {PORT}…")
    print("   Для остановки нажмите Ctrl+C\n")

    try:
        await asyncio.gather(
            server.serve(),
            bot.start(BOT_TOKEN),
        )
    except asyncio.CancelledError:
        pass
    finally:
        # Страховка: если gather завершился, но бот ещё не закрыт
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run может выбросить KeyboardInterrupt до запуска loop
        print("\n👋 Бот остановлен.")
        sys.exit(0)
