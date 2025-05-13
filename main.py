from config import (
    BOT_TOKEN, MISTRAL_API_KEY, ADMINS, TECHCRUNCH_URL,
    COLLECTION_TIME, POSTING_TIME, CHANNEL_ID
)
from db import Database

from typing import Optional
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, InputMediaVideo
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from mistralai import Mistral
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, time
import asyncio
import pytz
import re
import json

# Инициализация клиента Mistral
mistral_client = Mistral(api_key=MISTRAL_API_KEY)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Состояния FSM
class PostStates(StatesGroup):
    waiting_for_time = State()
    waiting_for_approval = State()
    waiting_for_media = State()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Глобальные переменные
posting_enabled = True
post_time = time(20, 0) # По умолчанию 20:00 (8 PM)
pending_post = None
pending_media = []
MOSCOW_TZ = pytz.timezone('Europe/Moscow')
schedule_task = None
POST_NOTIFICATION_TEMPLATE = "📢 Пост опубликован в канале\n\nID сообщения: `{message_id}`\n\n{text}"
articles_data = []  # Будет хранить сырые данные статей
used_articles = []  # Будет хранить статьи, использованные в посте
HTTP_TIMEOUT = 10
MAX_RETRIES = 2

# Проверка прав администратора
def is_admin(user_id: str) -> bool:
    return str(user_id) in ADMINS

async def get_linked_chat_id():
    """Получает ID связанного чата комментариев"""
    try:
        chat = await bot.get_chat(CHANNEL_ID)
        if chat.linked_chat_id:
            logger.info(f"Найден linked_chat_id: {chat.linked_chat_id}")
            return chat.linked_chat_id
        logger.warning("У канала нет связанного чата комментариев")
        return None
    except Exception as e:
        logger.error(f"Ошибка получения linked_chat_id: {e}")
        return None

# Клавиатура для админа
def get_admin_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Статус"), KeyboardButton(text="⏰ Изменить время")],
            [KeyboardButton(text="✅ Вкл. автопост"), KeyboardButton(text="⛔ Выкл. автопост")],
            [KeyboardButton(text="📝 Создать пост"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите действие"
    )
    return keyboard

# Обновленная клавиатура для одобрения поста
def get_approval_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✅ Опубликовать"), KeyboardButton(text="🔄 Перегенерировать")],
            [KeyboardButton(text="✏️ Редактировать"), KeyboardButton(text="📷 Добавить медиа")],
            [KeyboardButton(text="🚫 Отменить"), KeyboardButton(text="⏱ Отложить")]
        ],
        resize_keyboard=True
    )
    return keyboard


async def send_error_to_admin(error_message: str):
    """Отправляет сообщение об ошибке админу"""
    try:
        for admin_id in ADMINS:
            await bot.send_message(
                chat_id=admin_id,
                text=f"🚨 Ошибка в боте:\n\n{error_message}",
                reply_markup=get_admin_keyboard()
            )
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке админу: {e}")

async def fetch_article_content(url: str) -> Optional[str]:
    """Загружает содержимое статьи с заданным таймаутом и повторными попытками"""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as response:
                    if response.status == 200:
                        return await response.text()
                    logger.warning(f"Статус ответа {response.status} для URL: {url}")
                    return None
        except asyncio.TimeoutError:
            logger.warning(f"Таймаут при попытке {attempt}/{MAX_RETRIES} получить статью: {url}")
            if attempt == MAX_RETRIES:
                logger.error(f"Не удалось получить статью после {MAX_RETRIES} попыток: {url}")
                return None
            await asyncio.sleep(1)  # Задержка перед повторной попыткой
        except Exception as e:
            logger.error(f"Ошибка при получении статьи {url}: {str(e)}")
            return None
    return None

async def get_articles():
    """Сбор статей с TechCrunch с обработкой таймаутов"""
    logger.info("Начало сбора статей с TechCrunch")
    try:
        # Получаем главную страницу
        main_page_content = await fetch_article_content(TECHCRUNCH_URL)
        if not main_page_content:
            logger.error("Не удалось получить главную страницу TechCrunch")
            return []
        
        soup = BeautifulSoup(main_page_content, 'html.parser')
        articles = []
        moscow_tz = pytz.timezone('Europe/Moscow')
        time_threshold = datetime.now(moscow_tz) - timedelta(hours=20)

        for card in soup.find_all('div', class_='loop-card__content'):
            try:
                title_link = card.find('h3', class_='loop-card__title').find('a', class_='loop-card__title-link')
                time_elem = card.find('time', class_='loop-card__time')
                
                if title_link and time_elem:
                    article_url = title_link['href']
                    article_time = datetime.fromisoformat(time_elem['datetime'].replace('Z', '+00:00'))
                    article_time = article_time.astimezone(moscow_tz)
                    
                    if article_time >= time_threshold:
                        article_html = await fetch_article_content(article_url)
                        if not article_html:
                            continue
                        
                        article_soup = BeautifulSoup(article_html, 'html.parser')
                        content = article_soup.find('div', class_='entry-content')
                        paragraphs = content.find_all('p', class_='wp-block-paragraph') if content else []
                        article_text = '\n'.join(p.get_text() for p in paragraphs)
                        
                        articles.append({
                            'url': article_url,
                            'title': title_link.get_text(),
                            'content': article_text
                        })
                        logger.info(f"Собрана статья: {title_link.get_text()}")
            except Exception as e:
                logger.error(f"Ошибка при обработке статьи: {e}")
                continue
        
        logger.info(f"Собрано {len(articles)} статей")
        return articles
    except Exception as e:
        logger.error(f"Критическая ошибка при сборе статей: {e}")
        await send_error_to_admin(f"Критическая ошибка при сборе статей: {e}")
        return []

async def compile_post(articles):
    """Компиляция поста с обработкой таймаутов"""
    logger.info("Начало компиляции поста")
    max_attempts = 3  # Максимальное количество попыток генерации
    attempt = 0
    global used_articles
    
    while attempt < max_attempts:
        if not articles:
            logger.warning("Нет статей для публикации")
            return None
    
        try:
            ### Этап 1: Выбор релевантных статей
            selection_prompt = (
                "Выбери 3 самые интересные статьи из списка ниже. "
                "Верни только JSON с ключами: selected (индексы выбранных статей 0-2), "
                "reason (краткое объяснение выбора).\n\n" +
                "\n".join(f"{i}. {a['title']}" for i, a in enumerate(articles[:5])))
            
            try:
                selection_response = mistral_client.chat.complete(
                    model="mistral-large-latest",
                    messages=[{"role": "user", "content": selection_prompt}],
                    response_format={"type": "json_object"}
                )
                logger.debug(f"Ответ от Mistral (выбор статей): {selection_response}")
            except asyncio.TimeoutError:
                logger.warning("Таймаут при генерации выбора статей")
                attempt += 1
                continue
                
            selection = json.loads(selection_response.choices[0].message.content)
            used_articles = [articles[i] for i in selection.get('selected', [0,1,2])]
            
            ### Этап 2: Генерация поста
            generation_prompt = (
                "Создай подробный пост для Telegram-канала, на основе следующих статей:" +
                "\n\n".join(f"{a['title']}\n{a['content'][:300]}..." for a in used_articles) +
                "\n\nСделай пост не длиннее 800 символов, добавь эмодзи и структурируй текст. "
                "Ни в коем случае не вставляй ссылки на статьи. "
                "Если пост получается слишком длинным, сократи его, оставив только самое важное."
            )
            
            try:
                generation_response = mistral_client.chat.complete(
                    model="mistral-large-latest",
                    messages=[{"role": "user", "content": generation_prompt}]
                )
                logger.debug(f"Ответ от Mistral (генерация поста): {generation_response}")
            except asyncio.TimeoutError:
                logger.warning("Таймаут при генерации поста")
                attempt += 1
                continue
                
            post = generation_response.choices[0].message.content
            
            if len(post) <= 1024:
                logger.info("Пост успешно скомпилирован")
                return post
            else:
                attempt += 1
                logger.warning(f"Пост слишком длинный ({len(post)} символов), попытка {attempt}/{max_attempts}")
                
                if attempt < max_attempts:
                    for admin_id in ADMINS:
                        try:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=f"⚠️ Пост слишком длинный ({len(post)} символов). Пытаюсь сгенерировать более короткий вариант (попытка {attempt}/{max_attempts})..."
                            )
                        except Exception as e:
                            logger.error(f"Не удалось уведомить админа {admin_id}: {e}")
                
        except Exception as e:
            logger.error(f"Ошибка при компиляции поста: {e}")
            await send_error_to_admin(f"Ошибка при компиляции поста: {e}")
            return None
    
    error_msg = f"Не удалось сгенерировать пост короче 1024 символов после {max_attempts} попыток"
    logger.error(error_msg)
    await send_error_to_admin(error_msg)
    return None
    
async def generate_daily_post():
    """Генерация ежедневного поста с сохранением данных статей"""
    global articles_data
    articles_data = await get_articles()  # Сохраняем сырые данные
    
    if not articles_data:
        return "Нет новых статей для публикации."
        
    return await compile_post(articles_data)

async def schedule_post():
    """Планирует ежедневную публикацию в заданное время по МСК"""
    global posting_enabled, post_time, pending_post, pending_media
    
    while True:
        if not posting_enabled:
            await asyncio.sleep(3600)
            continue
            
        now = datetime.now(pytz.timezone('Europe/Moscow'))
        today_post_time = now.replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)
        
        if now < today_post_time:
            next_post_time = today_post_time
        else:
            next_post_time = today_post_time + timedelta(days=1)
        
        sleep_seconds = (next_post_time - now).total_seconds()
        logger.info(f"Следующий пост в {next_post_time.strftime('%d.%m.%Y %H:%M')} МСК")
        await asyncio.sleep(sleep_seconds)
        
        if not posting_enabled:
            continue
            
        post_content = await generate_daily_post()
        if post_content:
            pending_post = post_content
            pending_media = []
            
            # Отправляем пост на одобрение всем админам
            for admin_id in ADMINS:
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"📝 Новый пост для одобрения:\n\n{post_content}",
                        reply_markup=get_approval_keyboard()
                    )
                except Exception as e:
                    logger.error(f"Не удалось отправить пост на одобрение админу {admin_id}: {e}")

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if is_admin(message.from_user.id):
        await message.answer(
            f"🤖 Бот для публикации постов\n"
            f"Текущее время публикации: {post_time.strftime('%H:%M')} МСК\n\n"
            f"Используйте кнопки ниже для управления ботом",
            reply_markup=get_admin_keyboard()
        )
    else:
        await message.answer("Доступ запрещен")

@dp.message(F.text == "ℹ️ Помощь", lambda message: is_admin(message.from_user.id))
async def cmd_help(message: types.Message):
    help_text = (
        "📚 Справка по командам:\n\n"
        "🔄 Статус - текущее состояние бота\n"
        "⏰ Изменить время - установить новое время публикации\n"
        "✅ Вкл. автопост - включить автоматическую публикацию\n"
        "⛔ Выкл. автопост - выключить автоматическую публикацию\n"
        "📝 Создать пост - сгенерировать и отправить пост сейчас\n\n"
        "При создании поста:\n"
        "✅ Опубликовать - отправить пост в канал\n"
        "🔄 Перегенерировать - создать новый вариант поста\n"
        "✏️ Редактировать - изменить текст вручную\n"
        "📷 Добавить медиа - прикрепить фото/видео\n"
        "🚫 Отменить - удалить текущий пост\n"
        "⏱ Отложить - сохранить для публикации позже"
    )
    await message.answer(help_text, reply_markup=get_admin_keyboard())

@dp.message(F.text == "📝 Создать пост")
async def manual_post(message: types.Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Доступ запрещён")
        return
        
    global pending_post, pending_media
    pending_media = []
    
    # Удаляем клавиатуру на время обработки
    await message.answer("🔄 Собираю статьи и генерирую пост...", 
                        reply_markup=types.ReplyKeyboardRemove())
    
    try:
        post_content = await generate_daily_post()
        if not post_content:
            await message.answer("❌ Не удалось создать пост. Попробуйте позже.",
                               reply_markup=get_admin_keyboard())
            return
            
        pending_post = post_content
        await message.answer(
            f"📝 Пост готов к публикации:\n\n{post_content}",
            reply_markup=get_approval_keyboard()
        )
        
        # Уведомление других админов
        for admin_id in ADMINS:
            if str(admin_id) != str(message.from_user.id):
                try:
                    await bot.send_message(
                        chat_id=admin_id,
                        text=f"📝 Новый пост от {message.from_user.full_name}:\n\n{post_content}",
                        reply_markup=get_approval_keyboard()
                    )
                except Exception as e:
                    logger.error(f"Ошибка уведомления админа {admin_id}: {e}")
                    
    except Exception as e:
        logger.error(f"Ошибка при создании поста: {str(e)}")
        await message.answer(f"❌ Произошла ошибка: {str(e)}",
                           reply_markup=get_admin_keyboard())

@dp.message(F.text == "✅ Опубликовать", lambda message: is_admin(message.from_user.id))
async def approve_post(message: types.Message):
    global pending_post, pending_media, used_articles
    
    if not pending_post:
        await message.answer("❌ Нет поста для публикации")
        return
    
    try:
        # 1. Получаем ID связанного чата комментариев
        linked_chat_id = await get_linked_chat_id()
        if not linked_chat_id:
            logger.warning("У канала нет связанного чата комментариев")
            await message.answer("⚠️ Чат комментариев не найден")
        
        # 2. Публикация поста в канал
        if pending_media:
            media = [
                InputMediaPhoto(media=m['file_id'], caption=pending_post if i == 0 else None)
                if m['type'] == 'photo' else
                InputMediaVideo(media=m['file_id'], caption=pending_post if i == 0 else None)
                for i, m in enumerate(pending_media)
            ]
            sent_messages = await bot.send_media_group(CHANNEL_ID, media=media)
            channel_message_id = sent_messages[0].message_id
            media_group_id = sent_messages[0].media_group_id
        else:
            sent_message = await bot.send_message(CHANNEL_ID, text=pending_post)
            channel_message_id = sent_message.message_id
            media_group_id = None
        
        # 3. Поиск сообщения в чате комментариев
        discussion_message_id = None
        if linked_chat_id:
            try:
                await asyncio.sleep(2)
                updates = await bot.get_updates(limit=10, timeout=3)
                
                for update in updates:
                    msg = update.message
                    if not msg:
                        continue
                        
                    if media_group_id and hasattr(msg, 'media_group_id'):
                        if (msg.chat.id == linked_chat_id and 
                            msg.media_group_id == media_group_id):
                            discussion_message_id = msg.message_id
                            break
                    elif (msg.chat.id == linked_chat_id and
                          msg.text == pending_post):
                        discussion_message_id = msg.message_id
                        break
                
                logger.info(f"Найден ID в чате: {discussion_message_id}")
                
            except Exception as e:
                logger.error(f"Ошибка поиска сообщения: {e}")
        
        # 4. Добавление записи в базу данных
        db = Database()
        urls = [a['url'] for a in used_articles] if used_articles else None
        url = json.dumps(urls) if urls else None  # Сериализуем список в JSON
        
        try:
            inserted_post = await db.insert_post(
                telegram_id=discussion_message_id,
                message_text=pending_post,
                url=url,
                user_id=message.from_user.id,
                username=message.from_user.full_name
            )
            
            if inserted_post:
                notification_text = f"✅ Пост успешно добавлен (ID: {inserted_post.get('id')})"
                logger.info(notification_text)
            else:
                notification_text = "❌ Не удалось сохранить пост в базу данных"
                logger.error(notification_text)
            
        except Exception as e:
            error_msg = str(e)
            if "violates foreign key constraint" in error_msg:
                # Извлекаем parent_id из сообщения об ошибке
                match = re.search(r'parent_id=(\d+)', error_msg)
                parent_id = match.group(1) if match else "unknown"
                notification_text = f"❌ Ошибка при добавлении поста: parent_id={parent_id} не существует"
            else:
                notification_text = f"❌ Ошибка при добавлении поста: {error_msg}"
            
            logger.error(notification_text)
        
        # 5. Уведомление администраторов
        for admin_id in ADMINS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=notification_text,
                    parse_mode="Markdown",
                    reply_markup=get_admin_keyboard()
                )
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
        
        # 6. Отправка источников (если есть)
        if used_articles:
            sources_text = "🔗 *Источники:*\n" + "\n".join(
                f"{i+1}. [{a['title']}]({a['url']})" for i, a in enumerate(used_articles)
            )
            await message.answer(
                sources_text,
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        
        # 7. Сброс состояния
        pending_post = None
        pending_media = []
        used_articles = []
        
    except Exception as e:
        error_msg = f"❌ Ошибка публикации: {str(e)}"
        logger.error(error_msg)
        await message.answer(error_msg)
        
        # Уведомляем всех админов об ошибке
        for admin_id in ADMINS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=error_msg,
                    reply_markup=get_admin_keyboard()
                )
            except Exception as send_error:
                logger.error(f"Не удалось отправить сообщение об ошибке админу {admin_id}: {send_error}")

@dp.message(F.text == "🔄 Перегенерировать", lambda message: is_admin(message.from_user.id))
async def regenerate_post(message: types.Message):
    global pending_post, pending_media
    pending_media = []
    
    await message.answer("🔄 Создаю новый вариант поста...", reply_markup=types.ReplyKeyboardRemove())
    
    post_content = await generate_daily_post()
    if post_content:
        pending_post = post_content
        await message.answer(
            f"📝 Новый вариант поста:\n\n{post_content}",
            reply_markup=get_approval_keyboard()
        )
    else:
        await message.answer("❌ Не удалось перегенерировать пост", reply_markup=get_admin_keyboard())

@dp.message(F.text == "✏️ Редактировать", lambda message: is_admin(message.from_user.id))
async def edit_post_manually(message: types.Message, state: FSMContext):
    await message.answer(
        "✏️ Введите новый текст поста:",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(PostStates.waiting_for_approval)

@dp.message(F.text == "📷 Добавить медиа", lambda message: is_admin(message.from_user.id))
async def add_media_to_post(message: types.Message, state: FSMContext):
    await message.answer(
        "📎 Прикрепите фото или видео (можно несколько):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(PostStates.waiting_for_media)

@dp.message(F.text == "🚫 Отменить", lambda message: is_admin(message.from_user.id))
async def cancel_post(message: types.Message):
    global pending_post, pending_media
    pending_post = None
    pending_media = []
    await message.answer(
        "❌ Публикация отменена",
        reply_markup=get_admin_keyboard()
    )   

@dp.message(F.text == "⏱ Отложить", lambda message: is_admin(message.from_user.id))
async def postpone_post(message: types.Message):
    await message.answer(
        "⏱ Пост сохранен для публикации позже",
        reply_markup=get_admin_keyboard()
    )

@dp.message(PostStates.waiting_for_media, F.photo | F.video, lambda message: is_admin(message.from_user.id))
async def process_media(message: types.Message, state: FSMContext):
    global pending_media
    
    if message.photo:
        file_id = message.photo[-1].file_id
        pending_media.append({'type': 'photo', 'file_id': file_id})
    elif message.video:
        file_id = message.video.file_id
        pending_media.append({'type': 'video', 'file_id': file_id})
    
    await message.answer(
        "📎 Медиа добавлено к посту. Отправьте еще или нажмите /done для завершения.",
        reply_markup=types.ReplyKeyboardRemove()
    )

@dp.message(PostStates.waiting_for_media, Command("done"), lambda message: is_admin(message.from_user.id))
async def finish_adding_media(message: types.Message, state: FSMContext):
    await message.answer(
        f"📝 Пост с медиа для одобрения:\n\n{pending_post}",
        reply_markup=get_approval_keyboard()
    )
    await state.clear()

@dp.message(F.text == "❌ Отменить публикацию", lambda message: is_admin(message.from_user.id))
async def cancel_post(message: types.Message):
    global pending_post, pending_media
    pending_post = None
    pending_media = []
    await message.answer(
        "❌ Публикация отменена",
        reply_markup=get_admin_keyboard()
    )

@dp.message(F.text == "⛔ Выкл. автопост", lambda message: is_admin(message.from_user.id))
async def disable_posting(message: types.Message):
    global posting_enabled
    if posting_enabled:
        posting_enabled = False
        await message.answer("⛔ Автопостинг выключен!", reply_markup=get_admin_keyboard())
    else:
        await message.answer("ℹ️ Автопостинг уже выключен", reply_markup=get_admin_keyboard())

@dp.message(F.text == "✅ Вкл. автопост", lambda message: is_admin(message.from_user.id))
async def enable_posting(message: types.Message):
    global posting_enabled
    if not posting_enabled:
        posting_enabled = True
        await message.answer("✅ Автопостинг включен!", reply_markup=get_admin_keyboard())
    else:
        await message.answer("ℹ️ Автопостинг уже включен", reply_markup=get_admin_keyboard())

@dp.message(F.text == "🔄 Статус", lambda message: is_admin(message.from_user.id))
async def post_status(message: types.Message):
    global post_time
    next_post_time = datetime.now(pytz.timezone('Europe/Moscow')).replace(hour=post_time.hour, minute=post_time.minute, second=0, microsecond=0)
    
    if datetime.now(pytz.timezone('Europe/Moscow')) > next_post_time:
        next_post_time += timedelta(days=1)
    
    await message.answer(
        f"Статус: {'🟢 Включен' if posting_enabled else '🔴 Выключен'}\n"
        f"Следующая публикация: {next_post_time.strftime('%d.%m.%Y в %H:%M')} МСК",
        reply_markup=get_admin_keyboard()
    )

@dp.message(F.text == "⏰ Изменить время", lambda message: is_admin(message.from_user.id))
async def cmd_set_time(message: types.Message, state: FSMContext):
    await message.answer(
        "⏰ Введите новое время публикации в формате ЧЧ:ММ (например, 20:00):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(PostStates.waiting_for_time)

@dp.message(PostStates.waiting_for_time, lambda message: is_admin(message.from_user.id))
async def process_set_time(message: types.Message, state: FSMContext):
    global post_time, schedule_task
    
    time_pattern = re.compile(r'^([0-1]?[0-9]|2[0-3]):([0-5][0-9])$')
    
    if not time_pattern.match(message.text):
        await message.answer("❌ Неверный формат времени. Используйте ЧЧ:ММ (например, 20:00)")
        return
    
    try:
        hours, minutes = map(int, message.text.split(':'))
        post_time = time(hours, minutes)
        
        # Отменяем старую задачу и запускаем новую
        if schedule_task:
            schedule_task.cancel()
            try:
                await schedule_task
            except asyncio.CancelledError:
                pass
                
        schedule_task = asyncio.create_task(schedule_post())
        
        await message.answer(
            f"✅ Время публикации изменено на {post_time.strftime('%H:%M')} МСК",
            reply_markup=get_admin_keyboard()
        )
        await state.clear()
    except ValueError:
        await message.answer("❌ Неверное время. Используйте формат ЧЧ:ММ (например, 20:00)")

async def on_startup():
    for admin_id in ADMINS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=f"🤖 Бот запущен и готов к работе!\n"
                     f"Текущее время публикации: {post_time.strftime('%H:%M')} МСК",
                reply_markup=get_admin_keyboard()
            )
        except Exception as e:
            logger.error(f"Не удалось отправить сообщение админу {admin_id}: {e}")

@dp.message()
async def unhandled_message(message: types.Message):
    logger.warning(f"Необработанное сообщение: {message.text}")

async def main():
    global schedule_task
    logger.info("Запуск бота...")
    await on_startup()
    schedule_task = asyncio.create_task(schedule_post())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())