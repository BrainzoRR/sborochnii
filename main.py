import os
import logging
import json
import time
import shutil
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict
import hashlib

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler
)
from telegram.constants import ParseMode
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Конфигурация
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHANNEL_ID = os.getenv("CHANNEL_ID")
MAX_SEARCH_RESULTS = 10

# Пути к файлам
QUEUE_FILE = "queue.json"
POSTED_PACKS_FILE = "posted_packs.txt"
IMAGES_DIR = "images"

# Создаём папку для изображений
os.makedirs(IMAGES_DIR, exist_ok=True)

# Создаём пустой queue.json, если его нет
if not os.path.exists(QUEUE_FILE) or os.path.getsize(QUEUE_FILE) == 0:
    with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
        json.dump([], f)

# Состояния для ConversationHandler
EDITING_TEXT = 1

# Модель данных для сборки
@dataclass
class Modpack:
    title: str
    description: str
    minecraft_version: str
    image_url: Optional[str]          # иконка (запасной вариант)
    gallery_urls: List[str]            # скриншоты из галереи
    download_url: str
    platform: str
    categories: List[str]
    loaders: List[str]
    slug: str
    project_id: str = ""
    versions_info: str = ""

    def get_id(self) -> str:
        return f"{self.platform}:{self.slug}"

# Модель для элемента очереди
@dataclass
class QueuedPost:
    text: str
    image_path: Optional[str]
    download_url: str
    scheduled_time: float
    pack_id: str
    title: str

class ModpackFinder:
    """Поиск сборок на Modrinth"""
    
    def __init__(self):
        self.modrinth_api = "https://api.modrinth.com/v2"
        self.headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        self.posted_packs = self.load_posted_packs()
    
    def load_posted_packs(self) -> set:
        try:
            with open(POSTED_PACKS_FILE, "r") as f:
                return set(line.strip() for line in f)
        except FileNotFoundError:
            return set()
    
    def save_posted_pack(self, pack_id: str):
        with open(POSTED_PACKS_FILE, "a") as f:
            f.write(f"{pack_id}\n")
        self.posted_packs.add(pack_id)
    
    def is_pack_posted(self, pack_id: str) -> bool:
        return pack_id in self.posted_packs
    
    def get_project_gallery(self, project_id: str) -> List[str]:
        """Получает список URL скриншотов из галереи"""
        try:
            r = requests.get(
                f"{self.modrinth_api}/project/{project_id}/gallery",
                headers=self.headers,
                timeout=30
            )
            r.raise_for_status()
            data = r.json()
            # Берём первые 3 скриншота
            return [item['url'] for item in data[:3]]
        except Exception as e:
            logger.debug(f"Не удалось получить галерею для {project_id}: {e}")
            return []
    
    async def search_new_modpacks(self) -> List[Modpack]:
        new_packs = []
        # Используем упрощённый синтаксис facets
        facets = '[["project_type:modpack"]]'
        params = {
            "query": "",
            "facets": facets,
            "sort": "updated",
            "limit": 50
        }
        
        try:
            response = requests.get(
                f"{self.modrinth_api}/search",
                params=params,
                headers=self.headers,
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            
            for hit in data.get("hits", []):
                pack_id = hit["project_id"]
                slug = hit["slug"]
                unique_id = f"modrinth:{slug}"
                
                if self.is_pack_posted(unique_id):
                    continue
                
                # Получаем детальную информацию
                project = self.get_modrinth_project(pack_id)
                if not project:
                    continue
                
                # Получаем версии
                versions = self.get_modrinth_versions(pack_id)
                mc_versions = set()
                loaders = set()
                for ver in versions[:5]:
                    for gv in ver.get("game_versions", []):
                        mc_versions.add(gv)
                    for loader in ver.get("loaders", []):
                        loaders.add(loader)
                
                # Получаем галерею
                gallery = self.get_project_gallery(pack_id)
                
                modpack = Modpack(
                    title=hit["title"],
                    description=hit.get("description", ""),
                    minecraft_version=", ".join(sorted(mc_versions, reverse=True)[:3]),
                    image_url=hit.get("icon_url"),
                    gallery_urls=gallery,
                    download_url=f"https://modrinth.com/modpack/{slug}",
                    platform="modrinth",
                    categories=hit.get("categories", []),
                    loaders=list(loaders),
                    slug=slug,
                    project_id=pack_id,
                    versions_info=f"Версии: {', '.join(list(mc_versions)[:3])}"
                )
                
                new_packs.append(modpack)
                if len(new_packs) >= MAX_SEARCH_RESULTS:
                    break
            
            logger.info(f"Найдено {len(new_packs)} новых сборок на Modrinth")
            
        except Exception as e:
            logger.error(f"Ошибка при поиске на Modrinth: {e}")
        
        return new_packs
    
    def get_modrinth_project(self, project_id: str) -> Optional[Dict]:
        try:
            r = requests.get(f"{self.modrinth_api}/project/{project_id}", headers=self.headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except:
            return None
    
    def get_modrinth_versions(self, project_id: str) -> List[Dict]:
        try:
            r = requests.get(f"{self.modrinth_api}/project/{project_id}/version", headers=self.headers, timeout=30)
            r.raise_for_status()
            return r.json()
        except:
            return []

class MessageStyler:
    """Стилизация сообщений в уникальном формате"""
    
    @staticmethod
    def style_message(modpack: Modpack) -> str:
        # Определяем эмодзи для заголовка по категориям
        cat = modpack.categories
        desc_lower = modpack.description.lower()
        
        # Эмодзи для заголовка
        title_emoji = "📦"
        if "magic" in cat or "магия" in desc_lower:
            title_emoji = "🔮"
        elif "adventure" in cat or "приключ" in desc_lower:
            title_emoji = "⚔️"
        elif "technology" in cat or "техн" in desc_lower:
            title_emoji = "⚙️"
        elif "exploration" in cat or "исслед" in desc_lower:
            title_emoji = "🌍"
        elif "dragon" in desc_lower or "дракон" in desc_lower:
            title_emoji = "🐉"
        elif "viking" in desc_lower or "викинг" in desc_lower:
            title_emoji = "🛡️"
        
        # Основное описание (первые 300 символов, обрезаем по предложению)
        short_desc = modpack.description[:300].rsplit('.', 1)[0] + "."
        
        # Формируем особенности
        features = []
        if "magic" in cat:
            features.append("🔮 Магия и заклинания")
        if "adventure" in cat:
            features.append("⚔️ Приключения и данжи")
        if "technology" in cat:
            features.append("⚙️ Технологии и механизмы")
        if "exploration" in cat:
            features.append("🌍 Исследование миров")
        if "quests" in cat:
            features.append("📜 Квесты")
        if "building" in cat:
            features.append("🏗️ Строительство")
        
        # Если особенностей мало, добавляем из описания
        if len(features) < 3:
            if "dragon" in desc_lower:
                features.append("🐉 Драконы")
            if "viking" in desc_lower:
                features.append("🛡️ Викинги")
            if "optimiz" in desc_lower:
                features.append("⚡ Оптимизация")
        
        # Добиваем до 3-4 пунктов общими фразами
        while len(features) < 3:
            features.append("✨ Уникальные механики")
        
        # Хештеги
        tags = ["#майнкрафт", "#сборка"]
        if modpack.platform == "modrinth":
            tags.append("#modrinth")
        
        # Добавляем хештеги из категорий
        cat_map = {
            "adventure": "#приключение",
            "magic": "#магия",
            "technology": "#техно",
            "exploration": "#исследование",
            "quests": "#квесты",
            "building": "#строительство"
        }
        for c in cat:
            if c in cat_map and cat_map[c] not in tags:
                tags.append(cat_map[c])
        
        # Хештег с версией (без точек)
        ver = modpack.minecraft_version.split(',')[0].strip().replace('.', '')
        tags.append(f"#mc{ver}")
        
        # Собираем пост
        lines = [
            f"**{modpack.title} ({modpack.minecraft_version})** {title_emoji}",
            "",
            short_desc,
            "",
            "✨ **Особенности:**"
        ]
        lines.extend([f"• {f}" for f in features[:4]])
        lines.append("")
        lines.append(" ".join(tags))
        lines.append("")
        lines.append("❤️ - Заходит")
        lines.append("👎 - Не моё")
        
        return "\n".join(lines)

# Работа с очередью
class PostQueue:
    @staticmethod
    def load() -> List[QueuedPost]:
        if not os.path.exists(QUEUE_FILE) or os.path.getsize(QUEUE_FILE) == 0:
            return []
        try:
            with open(QUEUE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return [QueuedPost(**item) for item in data]
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Ошибка загрузки очереди: {e}")
            return []
    
    @staticmethod
    def save(queue: List[QueuedPost]):
        try:
            with open(QUEUE_FILE, 'w', encoding='utf-8') as f:
                json.dump([asdict(q) for q in queue], f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения очереди: {e}")
    
    @staticmethod
    def add_post(post: QueuedPost):
        queue = PostQueue.load()
        queue.append(post)
        PostQueue.save(queue)
    
    @staticmethod
    def remove_post(index: int) -> Optional[QueuedPost]:
        queue = PostQueue.load()
        if 0 <= index < len(queue):
            removed = queue.pop(index)
            PostQueue.save(queue)
            return removed
        return None
    
    @staticmethod
    def get_due_posts(now: float) -> List[QueuedPost]:
        queue = PostQueue.load()
        due = []
        remaining = []
        for post in queue:
            if post.scheduled_time <= now:
                due.append(post)
            else:
                remaining.append(post)
        if due:
            PostQueue.save(remaining)
        return due

def get_next_schedule_time() -> float:
    """Возвращает timestamp ближайшего слота (12:00 или 18:00)"""
    now = datetime.now()
    slot12 = now.replace(hour=12, minute=0, second=0, microsecond=0)
    slot18 = now.replace(hour=18, minute=0, second=0, microsecond=0)
    
    if now < slot12:
        return slot12.timestamp()
    elif now < slot18:
        return slot18.timestamp()
    else:
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()

def download_image(url: str, pack_id: str) -> Optional[str]:
    """Скачивает изображение и возвращает локальный путь"""
    if not url:
        return None
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            ext = os.path.splitext(url.split('?')[0])[1]
            if not ext or len(ext) > 5:
                ext = '.png'
            filename = hashlib.md5(pack_id.encode()).hexdigest() + ext
            filepath = os.path.join(IMAGES_DIR, filename)
            with open(filepath, 'wb') as f:
                f.write(response.content)
            return filepath
    except Exception as e:
        logger.error(f"Ошибка скачивания изображения {url}: {e}")
    return None

# Управление сессиями пользователей
class UserSession:
    def __init__(self):
        self.modpacks: List[Modpack] = []
        self.current_index: int = 0
        self.current_pack: Optional[Modpack] = None
    
    def set_results(self, packs: List[Modpack]):
        self.modpacks = packs
        self.current_index = 0
        self._update_current()
    
    def next(self) -> Optional[Modpack]:
        if self.current_index < len(self.modpacks) - 1:
            self.current_index += 1
            self._update_current()
            return self.current_pack
        return None
    
    def _update_current(self):
        if self.modpacks and self.current_index < len(self.modpacks):
            self.current_pack = self.modpacks[self.current_index]
        else:
            self.current_pack = None
    
    def has_next(self) -> bool:
        return self.current_index < len(self.modpacks) - 1

# Глобальные объекты
finder = ModpackFinder()
styler = MessageStyler()
user_sessions: Dict[int, UserSession] = {}

def get_user_session(user_id: int) -> UserSession:
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession()
    return user_sessions[user_id]

async def send_modpack_preview(update: Update, context: ContextTypes.DEFAULT_TYPE, modpack: Modpack):
    """Отправляет предпросмотр сборки с кнопками"""
    text = styler.style_message(modpack)
    
    keyboard = [
        [
            InlineKeyboardButton("📦 В очередь", callback_data="publish"),
            InlineKeyboardButton("🚀 Опубликовать сейчас", callback_data="publish_now")
        ],
        [
            InlineKeyboardButton("✏️ Редактировать", callback_data="edit"),
            InlineKeyboardButton("🔄 Перегенерировать", callback_data="regenerate"),
            InlineKeyboardButton("❌ Отклонить", callback_data="reject")
        ],
        [InlineKeyboardButton("📥 Скачать сборку", url=modpack.download_url)]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # Пытаемся взять первый скриншот из галереи, если есть
    image_url = modpack.gallery_urls[0] if modpack.gallery_urls else modpack.image_url
    
    if image_url:
        try:
            img_response = requests.get(image_url, timeout=30)
            if img_response.status_code == 200:
                await update.effective_chat.send_photo(
                    photo=img_response.content,
                    caption=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
                return
        except Exception as e:
            logger.error(f"Ошибка загрузки изображения: {e}")
    
    # Если нет картинки или ошибка, отправляем текстом
    await update.effective_chat.send_message(
        text=text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=reply_markup
    )

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я бот для поиска модпаков.\n"
        "/search — начать поиск новых сборок\n"
        "/queue — показать очередь на публикацию"
    )

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    msg = await update.message.reply_text("🔍 Ищу новые сборки на Modrinth...")
    
    new_packs = await finder.search_new_modpacks()
    
    if not new_packs:
        await msg.edit_text("😕 Новых сборок не найдено. Попробуй позже.")
        return
    
    session.set_results(new_packs)
    await msg.delete()
    await update.message.reply_text(f"✅ Найдено {len(new_packs)} новых сборок. Показываю первую:")
    await send_modpack_preview(update, context, session.current_pack)

async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    queue = PostQueue.load()
    if not queue:
        await update.message.reply_text("📭 Очередь пуста.")
        return
    
    lines = ["📋 **Очередь публикаций:**\n"]
    for i, post in enumerate(queue, 1):
        dt = datetime.fromtimestamp(post.scheduled_time).strftime("%d.%m %H:%M")
        lines.append(f"{i}. {post.title} — {dt}")
    
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# Обработчики кнопок
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    
    if not session.current_pack:
        await query.edit_message_text("Сессия истекла. Начни заново с /search")
        return
    
    pack = session.current_pack
    action = query.data
    
    if action == "publish":
        # Добавляем в очередь
        text = styler.style_message(pack)
        scheduled_time = get_next_schedule_time()
        dt_str = datetime.fromtimestamp(scheduled_time).strftime("%d.%m %H:%M")
        
        # Скачиваем картинку (первый скриншот или иконку)
        image_url = pack.gallery_urls[0] if pack.gallery_urls else pack.image_url
        image_path = download_image(image_url, pack.get_id()) if image_url else None
        
        queued = QueuedPost(
            text=text,
            image_path=image_path,
            download_url=pack.download_url,
            scheduled_time=scheduled_time,
            pack_id=pack.get_id(),
            title=pack.title
        )
        PostQueue.add_post(queued)
        
        # Помечаем как обработанную
        finder.save_posted_pack(pack.get_id())
        
        # Удаляем сообщение с предпросмотром и отправляем подтверждение новым сообщением
        await query.message.delete()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"✅ Сборка добавлена в очередь на {dt_str}"
        )
        
        # Переходим к следующей
        if session.has_next():
            session.next()
            await send_modpack_preview(update, context, session.current_pack)
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Все новые сборки закончились. Используй /search снова."
            )
    
    elif action == "publish_now":
        # Мгновенная публикация в канал (для теста)
        text = styler.style_message(pack)
        image_url = pack.gallery_urls[0] if pack.gallery_urls else pack.image_url
        
        keyboard = [[InlineKeyboardButton("📥 Скачать сборку", url=pack.download_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            if image_url:
                img_response = requests.get(image_url, timeout=30)
                if img_response.status_code == 200:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=img_response.content,
                        caption=text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup
                    )
                else:
                    await context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup
                    )
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
            
            finder.save_posted_pack(pack.get_id())
            
            await query.message.delete()
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="🚀 Сборка опубликована в канал!"
            )
            
            if session.has_next():
                session.next()
                await send_modpack_preview(update, context, session.current_pack)
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="Все новые сборки закончились. Используй /search снова."
                )
        except Exception as e:
            logger.error(f"Ошибка публикации: {e}")
            await query.edit_message_text(f"❌ Ошибка при публикации: {e}")
    
    elif action == "reject":
        finder.save_posted_pack(pack.get_id())
        await query.message.delete()
        if session.has_next():
            session.next()
            await send_modpack_preview(update, context, session.current_pack)
        else:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Сборка отклонена. Новых больше нет."
            )
    
    elif action == "regenerate":
        # Удаляем старое и отправляем новое (текст может измениться при следующей стилизации)
        await query.message.delete()
        await send_modpack_preview(update, context, pack)
    
    elif action == "edit":
        await query.edit_message_text(
            "✍️ Отправь свой текст для этого поста (можно Markdown). "
            "После отправки он будет добавлен в очередь.\n"
            "Отправь /cancel для отмены."
        )
        context.user_data['editing_pack'] = pack
        return EDITING_TEXT

async def edit_text_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    pack = context.user_data.get('editing_pack')
    
    if not pack:
        await update.message.reply_text("Ошибка. Начни заново.")
        return ConversationHandler.END
    
    # Добавляем в очередь с пользовательским текстом
    scheduled_time = get_next_schedule_time()
    dt_str = datetime.fromtimestamp(scheduled_time).strftime("%d.%m %H:%M")
    
    image_url = pack.gallery_urls[0] if pack.gallery_urls else pack.image_url
    image_path = download_image(image_url, pack.get_id()) if image_url else None
    
    queued = QueuedPost(
        text=user_text,
        image_path=image_path,
        download_url=pack.download_url,
        scheduled_time=scheduled_time,
        pack_id=pack.get_id(),
        title=pack.title
    )
    PostQueue.add_post(queued)
    finder.save_posted_pack(pack.get_id())
    
    await update.message.reply_text(f"✅ Сборка добавлена в очередь на {dt_str}")
    
    # Переходим к следующей
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    if session.has_next():
        session.next()
        await send_modpack_preview(update, context, session.current_pack)
    else:
        await update.message.reply_text("Все новые сборки закончились. Используй /search снова.")
    
    return ConversationHandler.END

async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Редактирование отменено.")
    user_id = update.effective_user.id
    session = get_user_session(user_id)
    if session.current_pack:
        await send_modpack_preview(update, context, session.current_pack)
    return ConversationHandler.END

# Периодическая проверка очереди
async def check_queue_callback(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    due_posts = PostQueue.get_due_posts(now)
    
    if not due_posts:
        return
    
    for post in due_posts:
        try:
            keyboard = [[InlineKeyboardButton("📥 Скачать сборку", url=post.download_url)]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            if post.image_path and os.path.exists(post.image_path):
                with open(post.image_path, 'rb') as f:
                    await context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=f,
                        caption=post.text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=reply_markup
                    )
                # Удаляем файл после отправки
                os.remove(post.image_path)
            else:
                await context.bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=post.text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
            
            logger.info(f"Опубликована сборка из очереди: {post.title}")
                
        except Exception as e:
            logger.error(f"Ошибка публикации из очереди: {e}")

# Обработка ошибок
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

def main():
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN не задан")
        return
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID не задан")
        return
    
    # Создаём приложение
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    
    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("queue", queue_command))
    
    # ConversationHandler для редактирования
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(button_callback, pattern="^edit$")],
        states={
            EDITING_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_text_received)]
        },
        fallbacks=[CommandHandler("cancel", cancel_edit)]
    )
    app.add_handler(conv_handler)
    
    # Обработчик всех остальных callback
    app.add_handler(CallbackQueryHandler(button_callback))
    
    # Периодическая проверка очереди (раз в минуту)
    job_queue = app.job_queue
    job_queue.run_repeating(check_queue_callback, interval=60, first=10)
    
    app.add_error_handler(error_handler)
    
    logger.info("Бот запущен...")
    app.run_polling()

if __name__ == "__main__":
    main()
