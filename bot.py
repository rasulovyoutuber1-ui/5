"""
AniUz Telegram Anime Bot - To'liq Full Versiya (Post + Admin + Owner + User + DB + Middleware)
"""

import asyncio
import logging
import random
import uuid
import sys
from typing import List, Set
from dotenv import load_dotenv
import os

from aiogram import Bot, Dispatcher, Router, F, BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton, 
    InlineKeyboardMarkup, InlineKeyboardButton, TelegramObject
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from pydantic_settings import BaseSettings
from pydantic import Field, field_validator

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import String, Integer, Float, Text, BigInteger, select

# --- 1. CONFIGURATION ---
load_dotenv()

class Settings(BaseSettings):
    BOT_TOKEN: str = Field(..., env="BOT_TOKEN")
    BOT_USERNAME: str = Field(default="@AniUzAnimeBot", env="BOT_USERNAME")
    OWNER_ID: int = Field(..., env="OWNER_ID")
    ADMIN_IDS_RAW: str = Field(default="", env="ADMIN_IDS")
    MANDATORY_CHANNELS_RAW: str = Field(default="", env="MANDATORY_CHANNELS")
    POST_CHANNEL: str = Field(default="", env="POST_CHANNEL")
    DATABASE_URL: str = Field(default="sqlite+aiosqlite:///aniuz_bot.db", env="DATABASE_URL")
    LOG_LEVEL: str = Field(default="INFO", env="LOG_LEVEL")

    @field_validator("BOT_TOKEN")
    def validate_token(cls, v: str) -> str:
        if not v or ":" not in v:
            raise ValueError("BOT_TOKEN noto'g'ri!")
        return v.strip()

    @property
    def admin_ids(self) -> Set[int]:
        ids = {self.OWNER_ID}
        if self.ADMIN_IDS_RAW:
            for item in self.ADMIN_IDS_RAW.split(","):
                if item.strip().isdigit():
                    ids.add(int(item.strip()))
        return ids

    @property
    def mandatory_channels(self) -> List[str]:
        if not self.MANDATORY_CHANNELS_RAW:
            return []
        return [ch.strip() for ch in self.MANDATORY_CHANNELS_RAW.split(",") if ch.strip()]

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

try:
    config = Settings()
except Exception as e:
    print(f"❌ Xatolik: {e}")
    sys.exit(1)

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d - %(message)s"
)
logger = logging.getLogger("aniuz_bot")


# --- 2. DATABASE ---
engine = create_async_engine(config.DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

class Base(AsyncAttrs, DeclarativeBase):
    pass

class Anime(Base):
    __tablename__ = "animes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, nullable=True)
    year: Mapped[int] = mapped_column(Integer, nullable=True)
    rating: Mapped[float] = mapped_column(Float, default=0.0)
    genres_str: Mapped[str] = mapped_column(String(255), default="")
    poster_url: Mapped[str] = mapped_column(String(500), nullable=True)

    @property
    def genres(self) -> list[str]:
        return [g.strip() for g in self.genres_str.split(",") if g.strip()]

class VipCode(Base):
    __tablename__ = "vip_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    duration_days: Mapped[int] = mapped_column(Integer, default=30)
    is_used: Mapped[int] = mapped_column(Integer, default=0)

class DatabaseManager:
    async def init_db(self):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def get_anime_by_code(self, code: str) -> Anime | None:
        async with async_session() as session:
            result = await session.execute(select(Anime).where(Anime.code == code))
            return result.scalar_one_or_none()

    async def get_random_anime(self) -> Anime | None:
        async with async_session() as session:
            result = await session.execute(select(Anime))
            rows = result.scalars().all()
            if not rows:
                return None
            return random.choice(rows)

    async def add_anime(self, code: str, title: str, description: str, year: int, rating: float, genres: str, poster: str):
        async with async_session() as session:
            anime = Anime(code=code, title=title, description=description, year=year, rating=rating, genres_str=genres, poster_url=poster)
            session.add(anime)
            await session.commit()

    async def delete_anime(self, code: str) -> bool:
        async with async_session() as session:
            anime = await self.get_anime_by_code(code)
            if anime:
                await session.delete(anime)
                await session.commit()
                return True
            return False

    async def create_vip_code(self, code: str, duration_days: int):
        async with async_session() as session:
            db_code = VipCode(code=code, duration_days=duration_days, is_used=0)
            session.add(db_code)
            await session.commit()

db_session = DatabaseManager()


# --- 3. MIDDLEWARE (Check Subscription) ---
class CheckSubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: callable,
        event: TelegramObject,
        data: dict
    ) -> any:
        bot: Bot = data.get("bot")
        user = data.get("event_from_user")

        if not user or not bot or user.id in config.admin_ids:
            return await handler(event, data)

        if isinstance(event, CallbackQuery) and event.data == "check_subscription":
            return await handler(event, data)

        unsubscribed_channels = []
        for channel in config.mandatory_channels:
            try:
                member = await bot.get_chat_member(chat_id=channel, user_id=user.id)
                if member.status in ["left", "kicked"]:
                    chat_info = await bot.get_chat(channel)
                    invite_link = chat_info.invite_link or f"https://t.me/{channel.replace('@', '')}"
                    unsubscribed_channels.append({
                        "title": chat_info.title or channel,
                        "url": invite_link
                    })
            except Exception as e:
                logger.warning(f"Kanal {channel} tekshirishda xatolik: {e}")

        if unsubscribed_channels:
            keyboard_buttons = []
            for ch in unsubscribed_channels:
                keyboard_buttons.append([InlineKeyboardButton(text=f"🔗 {ch['title']} (A'zo bo'lish)", url=ch['url'])])
            keyboard_buttons.append([InlineKeyboardButton(text="✅ Tekshirish", callback_data="check_subscription")])
            markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            warning_text = (
                "👋 <b>Assalomu alaykum!</b>\n\n"
                "Botimizdan foydalanish uchun homiy kanallarimizga a'zo bo'ling va <b>«✅ Tekshirish»</b> tugmasini bosing:"
            )

            if isinstance(event, Message):
                await event.answer(warning_text, reply_markup=markup, parse_mode="HTML")
            elif isinstance(event, CallbackQuery):
                await event.answer("⚠️ Avval barcha kanallarga a'zo bo'ling!", show_alert=True)
                await event.message.edit_text(warning_text, reply_markup=markup, parse_mode="HTML")
            return

        return await handler(event, data)


# --- 4. HANDLERS (Routers) ---
user_router = Router()
admin_router = Router()
owner_router = Router()

class SearchState(StatesGroup):
    waiting_for_code = State()
    waiting_for_vip_code = State()

class AdminAnimeState(StatesGroup):
    code = State()
    title = State()
    description = State()
    year = State()
    rating = State()
    genres = State()
    poster = State()

class AdminDeleteState(StatesGroup):
    code = State()

class AdminPostState(StatesGroup):
    content = State()

def get_main_menu_keyboard(user_id: int):
    kb = [
        [KeyboardButton(text="🔍 Anime qidirish"), KeyboardButton(text="📂 Janrlar")],
        [KeyboardButton(text="🎲 Tasodifiy anime"), KeyboardButton(text="⭐ Sevimlilar")],
        [KeyboardButton(text="👑 VIP obuna"), KeyboardButton(text="✍️ Zayavka yuborish")]
    ]
    if user_id in config.admin_ids:
        kb.append([KeyboardButton(text="🛠 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

# --- USER HANDLERS ---
@user_router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"👋 Salom, <b>{message.from_user.full_name}</b>!\n\n🎬 <b>AniUz Anime Bot</b>iga xush kelibsiz!",
        reply_markup=get_main_menu_keyboard(message.from_user.id),
        parse_mode="HTML"
    )

@user_router.callback_query(F.data == "check_subscription")
async def verify_sub(callback: CallbackQuery):
    await callback.answer("🎉 Tabriklaymiz! Obuna tasdiqlandi.", show_alert=True)
    await callback.message.delete()
    await callback.message.answer("Asosiy menyu:", reply_markup=get_main_menu_keyboard(callback.from_user.id))

@user_router.message(F.text == "🔍 Anime qidirish")
async def search_choice(message: Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔢 Kod orqali", callback_data="search_by_code")]
    ])
    await message.answer("🔍 Qidiruv usulini tanlang:", reply_markup=markup)

@user_router.callback_query(F.data == "search_by_code")
async def ask_code(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.waiting_for_code)
    await callback.message.answer("🔢 Anime kodini kiriting (Masalan: <b>101</b>):", parse_mode="HTML")
    await callback.answer()

@user_router.message(SearchState.waiting_for_code)
async def process_code_search(message: Message, state: FSMContext):
    code = message.text.strip().replace("#", "")
    anime = await db_session.get_anime_by_code(code)
    await state.clear()

    if not anime:
        await message.answer(f"❌ <b>#{code}</b> kodli anime topilmadi.", parse_mode="HTML")
        return

    caption = f"🎬 <b>{anime.title}</b>\n🔢 Kod: <code>#{anime.code}</code>\n⭐ Reyting: {anime.rating}/10\n📅 Yili: {anime.year}\n\n{anime.description}"
    if anime.poster_url:
        await message.answer_photo(photo=anime.poster_url, caption=caption, parse_mode="HTML")
    else:
        await message.answer(caption, parse_mode="HTML")

@user_router.message(F.text == "🎲 Tasodifiy anime")
async def random_anime(message: Message):
    anime = await db_session.get_random_anime()
    if not anime:
        await message.answer("Bazada hali animelar mavjud emas.")
        return
    caption = f"🎲 <b>{anime.title}</b> (Kod: #{anime.code})\n⭐ Reyting: {anime.rating}/10"
    if anime.poster_url:
        await message.answer_photo(photo=anime.poster_url, caption=caption, parse_mode="HTML")
    else:
        await message.answer(caption, parse_mode="HTML")

@user_router.message(F.text == "👑 VIP obuna")
async def vip_info(message: Message):
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 VIP Kodni kiritish", callback_data="enter_vip_code")]
    ])
    await message.answer("👑 VIP obuna bo'limi. Kodni kiriting:", reply_markup=markup)


# --- ADMIN HANDLERS (Anime + Post) ---
@admin_router.message(F.text == "🛠 Admin Panel")
async def admin_panel(message: Message):
    if message.from_user.id not in config.admin_ids:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Anime qo'shish", callback_data="admin_add_anime")],
        [InlineKeyboardButton(text="❌ Anime o'chirish", callback_data="admin_del_anime")],
        [InlineKeyboardButton(text="📢 Post yuborish", callback_data="admin_send_post")]
    ])
    await message.answer("🛠 <b>Admin Boshqaruv Paneli:</b>", reply_markup=markup, parse_mode="HTML")

@admin_router.callback_query(F.data == "admin_add_anime")
async def start_add_anime(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.admin_ids:
        return
    await state.set_state(AdminAnimeState.code)
    await callback.message.answer("🔢 Anime uchun <b>unikal kod</b> kiriting (masalan: <code>101</code>):", parse_mode="HTML")
    await callback.answer()

@admin_router.message(AdminAnimeState.code)
async def state_anime_code(message: Message, state: FSMContext):
    await state.update_data(code=message.text.strip().replace("#", ""))
    await state.set_state(AdminAnimeState.title)
    await message.answer("📝 Anime <b>nomini</b> kiriting:")

@admin_router.message(AdminAnimeState.title)
async def state_anime_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text.strip())
    await state.set_state(AdminAnimeState.description)
    await message.answer("📖 Anime haqida <b>tavsif (opisaniya)</b> kiriting:")

@admin_router.message(AdminAnimeState.description)
async def state_anime_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text.strip())
    await state.set_state(AdminAnimeState.year)
    await message.answer("📅 Chiqqan <b>yilini</b> kiriting (masalan: 2024):")

@admin_router.message(AdminAnimeState.year)
async def state_anime_year(message: Message, state: FSMContext):
    try:
        year = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Faqat raqam kiriting! Qaytadan yozing:")
        return
    await state.update_data(year=year)
    await state.set_state(AdminAnimeState.rating)
    await message.answer("⭐ Reytingini kiriting (masalan: 8.5):")

@admin_router.message(AdminAnimeState.rating)
async def state_anime_rating(message: Message, state: FSMContext):
    try:
        rating = float(message.text.strip())
    except ValueError:
        await message.answer("❌ Faqat raqam kiriting (masalan: 8.5):")
        return
    await state.update_data(rating=rating)
    await state.set_state(AdminAnimeState.genres)
    await message.answer("📂 Janrlarini vergul bilan kiriting (masalan: action, fantasy):")

@admin_router.message(AdminAnimeState.genres)
async def state_anime_genres(message: Message, state: FSMContext):
    await state.update_data(genres=message.text.strip())
    await state.set_state(AdminAnimeState.poster)
    await message.answer("🖼 Poster rasmining <b>URL manzili</b>ni yuboring (yoki 'yo'q' deb yozing):")

@admin_router.message(AdminAnimeState.poster)
async def state_anime_save(message: Message, state: FSMContext):
    poster = message.text.strip()
    if poster.lower() in ["yo'q", "yoq", "no", "-"]:
        poster = None
    
    data = await state.get_data()
    await state.clear()

    await db_session.add_anime(
        code=data["code"],
        title=data["title"],
        description=data["description"],
        year=data["year"],
        rating=data["rating"],
        genres=data["genres"],
        poster=poster
    )
    await message.answer(f"✅ <b>{data['title']}</b> animkasi bazaga muvaffaqiyatli qo'shildi!", parse_mode="HTML", reply_markup=get_main_menu_keyboard(message.from_user.id))

@admin_router.callback_query(F.data == "admin_del_anime")
async def start_del_anime(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.admin_ids:
        return
    await state.set_state(AdminDeleteState.code)
    await callback.message.answer("❌ O'chirmoqchi bo'lgan anime <b>kodini</b> kiriting:", parse_mode="HTML")
    await callback.answer()

@admin_router.message(AdminDeleteState.code)
async def process_del_anime(message: Message, state: FSMContext):
    code = message.text.strip().replace("#", "")
    await state.clear()
    success = await db_session.delete_anime(code)
    if success:
        await message.answer(f"✅ #{code} kodli anime o'chirildi.", reply_markup=get_main_menu_keyboard(message.from_user.id))
    else:
        await message.answer(f"❌ #{code} kodli anime topilmadi.")

# POST TARQATISH HANDLERLARI
@admin_router.callback_query(F.data == "admin_send_post")
async def start_send_post(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in config.admin_ids:
        return
    await state.set_state(AdminPostState.content)
    await callback.message.answer("📢 Kanalga yuborilishi kerak bo'lgan xabarni (matn, rasm yoki video) yuboring:")
    await callback.answer()

@admin_router.message(AdminPostState.content)
async def process_send_post(message: Message, state: FSMContext):
    await state.clear()
    if not config.POST_CHANNEL:
        await message.answer("❌ .env faylida POST_CHANNEL ko'rsatilmagan!")
        return
    
    try:
        await message.send_copy(chat_id=config.POST_CHANNEL)
        await message.answer("✅ Post muvaffaqiyatli kanalga yuborildi!", reply_markup=get_main_menu_keyboard(message.from_user.id))
    except Exception as e:
        await message.answer(f"❌ Postni yuborishda xatolik yuz berdi: {e}")


# --- OWNER HANDLERS ---
@owner_router.message(Command("owner"))
async def owner_dashboard(message: Message):
    if message.from_user.id != config.OWNER_ID:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 VIP Kod Generator", callback_data="owner_vip_gen")]
    ])
    await message.answer("👑 <b>BOT EGASI PANELI</b>", reply_markup=markup, parse_mode="HTML")

@owner_router.callback_query(F.data == "owner_vip_gen")
async def choose_vip_plan(callback: CallbackQuery):
    if callback.from_user.id != config.OWNER_ID:
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 30 kunlik (1 ta)", callback_data="gen_vip_30_1")],
        [InlineKeyboardButton(text="⬅️ Orqaga", callback_data="owner_back")]
    ])
    await callback.message.edit_text("🔑 Qancha muddatli VIP kod generatsiya qilmoqchisiz?", reply_markup=markup)

@owner_router.callback_query(F.data.startswith("gen_vip_"))
async def generate_codes(callback: CallbackQuery):
    _, _, days_str, count_str = callback.data.split("_")
    days, count = int(days_str), int(count_str)

    generated = []
    for _ in range(count):
        random_part = uuid.uuid4().hex[:8].upper()
        code = f"VIP{days}-{random_part[:4]}-{random_part[4:]}"
        await db_session.create_vip_code(code=code, duration_days=days)
        generated.append(f"<code>{code}</code> ({days} kunlik)")

    await callback.message.answer(f"✅ <b>Yangi VIP kodlar:</b>\n\n" + "\n".join(generated), parse_mode="HTML")
    await callback.answer("Yaratildi!")


# --- 5. MAIN ENTRY POINT ---
async def main():
    logger.info("Baza ishga tushirilmoqda...")
    await db_session.init_db()

    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    dp.message.outer_middleware(CheckSubscriptionMiddleware())
    dp.callback_query.outer_middleware(CheckSubscriptionMiddleware())

    dp.include_router(owner_router)
    dp.include_router(admin_router)
    dp.include_router(user_router)

    logger.info("🚀 Bot ishga tushdi!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot to'xtatildi.")
