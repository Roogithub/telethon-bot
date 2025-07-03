import os
import re
import io
import zipfile
import tempfile
import base64
import logging
import warnings
import asyncio
import shutil  # Добавлено для удаления временных директорий
from collections import deque

from telethon import TelegramClient, events, Button
from telethon.errors import MessageNotModifiedError
from ebooklib import epub
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from PIL import Image
from lxml import etree
from docx import Document

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
logging.basicConfig(level=logging.INFO)

api_id = 24519852
api_hash = '2186f59fdf9c2ad4e7ddf0deb250ff0c'
client = TelegramClient('unified_bot', api_id, api_hash)

RESOLUTIONS = {
    'Удалить изображения': None,
    '64p': (64, 64),
    '144p': (256, 144),
    '360p': (640, 360),
    '480p': (854, 480),
    '720p': (1280, 720),
    '1080p': (1920, 1080)
}

user_files = {}
user_mode = {}
last_message_text = {}

# Система очереди - ИЗМЕНЕНО на 1
MAX_CONCURRENT_TASKS = 1  # Только один файл обрабатывается одновременно
active_tasks = 0
task_queue = deque()
queue_lock = asyncio.Lock()

# Новое регулярное выражение для поиска глав
CHAPTER_RE = re.compile(
    r"(?i)^\s*("
    r"Глава|chapter|часть|пролог|аннотация|annotation|описание|предисловие от автора"
    r")", re.IGNORECASE
)

# Декоратор для управления очередью
def queue_manager(func):
    """Декоратор для управления очередью обработки"""
    async def wrapper(*args, **kwargs):
        global active_tasks
        
        async with queue_lock:
            # Если слот занят, добавляем в очередь
            if active_tasks >= MAX_CONCURRENT_TASKS:
                # Создаем Future для ожидания
                future = asyncio.Future()
                task_queue.append(future)
                
                # Уведомляем пользователя о позиции в очереди
                event = args[0]
                queue_position = len(task_queue)
                await event.respond(
                    f"⏳ Ваш запрос добавлен в очередь.\n"
                    f"Позиция в очереди: {queue_position}\n"
                    f"Ожидайте обработки..."
                )
                
                # Освобождаем блокировку перед ожиданием
                queue_lock.release()
                try:
                    await future
                finally:
                    await queue_lock.acquire()
            
            active_tasks += 1
        
        try:
            # Выполняем основную функцию
            result = await func(*args, **kwargs)
            return result
        finally:
            async with queue_lock:
                active_tasks -= 1
                
                # Если есть задачи в очереди, запускаем следующую
                if task_queue:
                    next_task = task_queue.popleft()
                    next_task.set_result(True)
    
    return wrapper  # Возвращаем wrapper, а не вызываем его 
            

# Функция для создания прогресс-бара
def create_progress_bar(current, total, width=20):
    if total == 0:
        return "▓" * width + " 100%"
    progress = int((current / total) * width)
    bar = "▓" * progress + "░" * (width - progress)
    percentage = int((current / total) * 100)
    return f"{bar} {percentage}%"

# Функция для проверки, нужно ли обновлять прогресс
def should_update_progress(current, total, last_updated_percent):
    if total == 0:
        return True
    current_percent = int((current / total) * 100)
    return current_percent - last_updated_percent >= 5

# Безопасное редактирование сообщения
async def safe_edit_message(message, new_text):
    """Редактирует сообщение только если текст изменился"""
    try:
        message_id = message.id
        if message_id not in last_message_text or last_message_text[message_id] != new_text:
            await message.edit(new_text)
            last_message_text[message_id] = new_text
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logging.error(f"Error editing message: {e}")

# Команды
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond(
        "Здравствуйте. Вас приветствует Адикия — бот для работы с документами.\n\n"
        "Возможности:\n"
        "• Сжатие или удаление изображений в .epub, .fb2, .docx\n"
        "• Извлечение глав из EPUB, FB2, DOCX и пересборка с оглавлением\n\n"
        "⚠️ Бот работает на бесплатном сервере.\n"
        "Файлы обрабатываются строго по очереди.\n\n"
        "Используйте /help для получения списка команд."
    )

@client.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    await event.respond(
        "Справка по командам:\n\n"
        "/start    - Начать работу с ботом\n"
        "/help     - Показать эту справку\n"
        "/compress - Сжать или удалить изображения в .epub/.fb2/.docx\n"
        "/extract  - Извлечь главы и пересобрать с оглавлением\n"
        "/cancel   - Отменить текущую операцию\n"
        "/status   - Проверить статус очереди\n"
    )

@client.on(events.NewMessage(pattern='/status'))
async def status_command(event):
    """Показывает текущий статус очереди"""
    status_text = "📊 Статус системы:\n\n"
    
    if active_tasks > 0:
        status_text += "🔄 Сейчас обрабатывается файл\n"
    else:
        status_text += "✅ Бот свободен\n"
    
    status_text += f"⏳ В очереди: {len(task_queue)} файл(ов)"
    
    await event.respond(status_text)

@client.on(events.NewMessage(pattern='/cancel'))
async def cancel(event):
    user_id = event.sender_id
    user_files.pop(user_id, None)
    user_mode.pop(user_id, None)
    await event.respond("Операция отменена.")

@client.on(events.NewMessage(pattern='/compress'))
async def compress_cmd(event):
    user_mode[event.sender_id] = 'compress'
    await event.respond("Пожалуйста, отправьте файл .epub, .fb2 или .docx для обработки изображений.")

@client.on(events.NewMessage(pattern='/extract'))
async def extract_cmd(event):
    user_mode[event.sender_id] = 'extract'
    await event.respond("Пожалуйста, отправьте файл .epub, .fb2 или .docx. Я извлеку главы и пересоберу его с оглавлением.")

# Приём файлов с прогресс-баром
@client.on(events.NewMessage(incoming=True))
async def handle_file(event):
    if not event.file:
        return
    user_id = event.sender_id
    mode = user_mode.get(user_id)
    if not mode:
        return

    filename = event.file.name or ''
    ext = os.path.splitext(filename)[1].lower()
    file_size = event.file.size

    # Индикатор прогресса загрузки файла
    progress_msg = await event.respond("📥 Загрузка файла...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    
    file_data = io.BytesIO()
    last_percent = 0
    
    async def progress_callback(current, total):
        nonlocal last_percent
        if total > 0 and should_update_progress(current, total, last_percent):
            progress_bar = create_progress_bar(current, total)
            new_text = f"📥 Загрузка файла...\n{progress_bar}"
            await safe_edit_message(progress_msg, new_text)
            last_percent = int((current / total) * 100)
    
    await client.download_media(event.message, file=file_data, progress_callback=progress_callback)
    file_data.seek(0)

    await safe_edit_message(progress_msg, "📥 Загрузка завершена! Сохранение файла...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(file_data.read())
        tmp_path = tmp.name

    user_files[user_id] = (filename, tmp_path)
    await progress_msg.delete()
    last_message_text.pop(progress_msg.id, None)

    if mode == 'compress' and ext in ['.epub', '.fb2', '.docx']:
        buttons = [Button.inline(label, data=label.encode()) for label in RESOLUTIONS]
        await event.respond("Выберите способ обработки изображений:", buttons=buttons)

    elif mode == 'extract' and ext in ['.epub', '.fb2', '.docx']:
        await process_extract_with_queue(event, user_id, filename, tmp_path, ext)

# Обработка извлечения с очередью
@queue_manager
async def process_extract_with_queue(event, user_id, filename, tmp_path, ext):
    """Обработка извлечения глав с учетом очереди"""
    await event.respond("✅ Файл принят в обработку. Начинаю...")
    try:
        base = os.path.splitext(filename)[0]
        output_path = None
        
        if ext == '.epub':
            chapters, images = await extract_chapters_from_epub_async(tmp_path, event)
            if not chapters:
                await event.respond("Главы не найдены в EPUB.")
                return
            output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.epub")
            build_progress = await event.respond("📚 Сборка EPUB...\n░░░░░░░░░░░░░░░░░░░░ 0%")
            await build_epub_async(base, chapters, images, output_path, build_progress)
            await build_progress.delete()
            last_message_text.pop(build_progress.id, None)
            await client.send_file(user_id, output_path, caption="✅ EPUB пересобран с оглавлением.")
            
        elif ext == '.fb2':
            chapters = await extract_chapters_from_fb2_async(tmp_path, event)
            if not chapters:
                await event.respond("Главы не найдены в FB2.")
                return
            output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.fb2")
            build_progress = await event.respond("📚 Сборка FB2...\n░░░░░░░░░░░░░░░░░░░░ 0%")
            await build_fb2_with_toc_async(base, chapters, output_path, build_progress)
            await build_progress.delete()
            last_message_text.pop(build_progress.id, None)
            await client.send_file(user_id, output_path, caption="✅ FB2 пересобран с оглавлением.")
            
        elif ext == '.docx':
            chapters = await extract_chapters_from_docx_async(tmp_path, event)
            if not chapters:
                await event.respond("Главы не найдены в DOCX.")
                return
            output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.docx")
            build_progress = await event.respond("📚 Сборка DOCX...\n░░░░░░░░░░░░░░░░░░░░ 0%")
            await build_docx_with_toc_async(base, chapters, output_path, build_progress)
            await build_progress.delete()
            last_message_text.pop(build_progress.id, None)
            await client.send_file(user_id, output_path, caption="✅ DOCX пересобран с оглавлением.")
        
        if output_path and os.path.exists(output_path):
            os.remove(output_path)
            
    except Exception as e:
        logging.error(f"Error processing file: {e}", exc_info=True)
        await event.respond(f"Ошибка: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        user_mode.pop(user_id, None)
        user_files.pop(user_id, None)

# Inline-кнопки
@client.on(events.CallbackQuery)
async def handle_button(event):
    user_id = event.sender_id
    mode = user_mode.get(user_id)
    if not mode:
        return

    data = event.data.decode()
    filename, filepath = user_files.get(user_id, (None, None))
    if not filename or not os.path.exists(filepath):
        await event.answer("Файл не найден. Начните заново.", alert=True)
        return

    ext = os.path.splitext(filename)[1].lower()

    if mode == 'compress':
        resolution = RESOLUTIONS.get(data)
        await event.edit(f"⚙️ Подготовка к обработке файла {filename}...")
        await process_compression_with_queue(event, user_id, filename, filepath, resolution, ext)

# Обработка сжатия с очередью
@queue_manager
async def process_compression_with_queue(event, user_id, filename, filepath, resolution, ext):
    """Обработка сжатия изображений с учетом очереди"""
    await event.edit("✅ Файл принят в обработку. Начинаю...")
    
    if ext == '.fb2':
        await process_fb2(event, user_id, filename, filepath, resolution)
    elif ext == '.docx':
        await process_docx(event, user_id, filename, filepath, resolution)
    elif ext == '.epub':
        await process_epub_compression(event, user_id, filename, filepath, resolution)
    
    if os.path.exists(filepath):
        os.remove(filepath)
    user_files.pop(user_id, None)
    user_mode.pop(user_id, None)

# Обработка изображений FB2 с улучшенным прогресс-баром
async def process_fb2(event, user_id, filename, filepath, resolution):
    ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    tree = etree.parse(filepath)
    root = tree.getroot()
    binaries = root.xpath('//fb2:binary', namespaces=ns)

    changed = deleted = 0
    image_binaries = [b for b in binaries if 'image' in (b.get('content-type') or '')]
    total = len(image_binaries)
    
    if total == 0:
        await event.edit("Изображения не найдены в файле.")
        return

    progress_msg = await event.respond(f"🖼️ Обработка изображений FB2...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    last_percent = 0

    for current, binary in enumerate(image_binaries, 1):
        if should_update_progress(current, total, last_percent):
            progress_bar = create_progress_bar(current, total)
            action = "Удаление" if resolution is None else "Сжатие"
            new_text = f"🖼️ {action} изображений FB2...\n{progress_bar}"
            await safe_edit_message(progress_msg, new_text)
            last_percent = int((current / total) * 100)

        if resolution is None:
            # Удаляем ссылки на изображение из текста
            binary_id = binary.get('id')
            if binary_id:
                # Удаляем все ссылки на это изображение
                for img_ref in root.xpath(f'//fb2:image[@l:href="#{binary_id}"]', 
                                        namespaces={'fb2': ns['fb2'], 'l': 'http://www.w3.org/1999/xlink'}):
                    img_ref.getparent().remove(img_ref)
            root.remove(binary)
            deleted += 1
        else:
            try:
                img = Image.open(io.BytesIO(base64.b64decode(binary.text))).convert('RGB')
                img.thumbnail(resolution, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=30)
                binary.text = base64.b64encode(buf.getvalue()).decode()
                binary.set('content-type', 'image/jpeg')
                changed += 1
            except Exception:
                continue

    await safe_edit_message(progress_msg, "💾 Сохранение файла...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    tree.write(out_path, encoding='utf-8', xml_declaration=True)
    
    await progress_msg.delete()
    last_message_text.pop(progress_msg.id, None)
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(filepath)
    os.remove(out_path)

# DOCX с улучшенным прогресс-баром
async def process_docx(event, user_id, filename, filepath, resolution):
    doc = Document(filepath)
    changed = deleted = 0
    total = len(doc.inline_shapes)
    
    if total == 0:
        await event.edit("Изображения не найдены в документе.")
        return

    progress_msg = await event.respond(f"🖼️ Обработка изображений DOCX...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    last_percent = 0

    if resolution is None:
        # Создаем список для удаления в обратном порядке
        shapes_to_remove = []
        for current, shape in enumerate(doc.inline_shapes, 1):
            if should_update_progress(current, total, last_percent):
                progress_bar = create_progress_bar(current, total)
                new_text = f"🖼️ Удаление изображений DOCX...\n{progress_bar}"
                await safe_edit_message(progress_msg, new_text)
                last_percent = int((current / total) * 100)
            shapes_to_remove.append(shape)
        
        # Удаляем в обратном порядке чтобы не нарушить индексацию
        for shape in reversed(shapes_to_remove):
            try:
                shape._element.getparent().remove(shape._element)
                deleted += 1
            except Exception:
                continue
    else:
        for current, shape in enumerate(doc.inline_shapes, 1):
            if should_update_progress(current, total, last_percent):
                progress_bar = create_progress_bar(current, total)
                new_text = f"🖼️ Сжатие изображений DOCX...\n{progress_bar}"
                await safe_edit_message(progress_msg, new_text)
                last_percent = int((current / total) * 100)
            
            try:
                r_id = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
                img_part = doc.part.related_parts[r_id]
                img = Image.open(io.BytesIO(img_part.blob)).convert('RGB')
                img.thumbnail(resolution, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=30)
                img_part._blob = buf.getvalue()
                changed += 1
            except Exception:
                continue

    await safe_edit_message(progress_msg, "💾 Сохранение документа...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    doc.save(out_path)
    
    await progress_msg.delete()
    last_message_text.pop(progress_msg.id, None)
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(filepath)
    os.remove(out_path)

# EPUB с улучшенным прогресс-баром
async def process_epub_compression(event, user_id, filename, filepath, resolution):
    book = epub.read_epub(filepath)
    changed = deleted = 0
    images = [item for item in list(book.get_items()) if item.media_type and item.media_type.startswith("image/")]
    total = len(images)
    
    if total == 0:
        await event.edit("Изображения не найдены в EPUB.")
        return

    progress_msg = await event.respond(f"🖼️ Обработка изображений EPUB...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    last_percent = 0

    for current, item in enumerate(images, 1):
        if should_update_progress(current, total, last_percent):
            progress_bar = create_progress_bar(current, total)
            action = "Удаление" if resolution is None else "Сжатие"
            new_text = f"🖼️ {action} изображений EPUB...\n{progress_bar}"
            await safe_edit_message(progress_msg, new_text)
            last_percent = int((current / total) * 100)

        if resolution is None:
            book.items.remove(item)
            deleted += 1
        else:
            try:
                img = Image.open(io.BytesIO(item.content)).convert("RGB")
                img.thumbnail(resolution, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=30)
                item.content = buf.getvalue()
                item.media_type = "image/jpeg"
                changed += 1
            except Exception:
                continue

    await safe_edit_message(progress_msg, "💾 Сохранение EPUB...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    epub.write_epub(out_path, book)
    
    await progress_msg.delete()
    last_message_text.pop(progress_msg.id, None)
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(filepath)
    os.remove(out_path)

# EPUB: главы с асинхронным прогресс-баром
async def extract_chapters_from_epub_async(epub_path, event):
    temp_dir = tempfile.mkdtemp()
    html_blocks = []
    images = {}

    progress_msg = await event.respond("📖 Извлечение файлов из EPUB...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    
    try:
        with zipfile.ZipFile(epub_path, 'r') as zf:
            zf.extractall(temp_dir)

        await safe_edit_message(progress_msg, "📖 Анализ содержимого...\n▓▓▓▓▓░░░░░░░░░░░░░░░ 25%")
        
        all_files = []
        for root, _, files in os.walk(temp_dir):
            for file in files:
                all_files.append(os.path.join(root, file))
        
        processed = 0
        total_files = len(all_files)
        
        for file_path in all_files:
            file = os.path.basename(file_path)
            if file.lower().endswith((".xhtml", ".html", ".htm")):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        html_blocks.append(BeautifulSoup(f, "lxml"))
                except Exception as e:
                    logging.warning(f"Could not parse HTML file {file}: {e}")
            elif file.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
                images[file] = file_path
            
            processed += 1
            if processed % 10 == 0:
                progress = int((processed / total_files) * 25) + 25
                progress_bar = "▓" * (progress // 5) + "░" * (20 - progress // 5)
                new_text = f"📖 Анализ файлов...\n{progress_bar} {progress}%"
                await safe_edit_message(progress_msg, new_text)

        await safe_edit_message(progress_msg, "🔍 Поиск глав...\n▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░ 50%")
        
        chapters = []
        title, content, num = None, "", 0
        
        processed_blocks = 0
        total_blocks = len(html_blocks)
        
        for soup in html_blocks:
            if not soup.body:
                continue
            for elem in soup.body.find_all(recursive=False):
                text = elem.get_text(strip=True)
                match = CHAPTER_RE.match(text or "")
                if match:
                    if title:
                        chapters.append((num, title, content.strip()))
                    title = text  # Используем полный текст, не только match.group(1)
                    num_match = re.search(r'\d+', title)
                    num = int(num_match.group()) if num_match else len(chapters)
                    content = f"<h1>{title}</h1>"
                else:
                    content += str(elem)
            
            processed_blocks += 1
            if processed_blocks % 5 == 0:
                progress = 50 + int((processed_blocks / total_blocks) * 25)
                progress_bar = "▓" * (progress // 5) + "░" * (20 - progress // 5)
                new_text = f"🔍 Поиск глав...\n{progress_bar} {progress}%"
                await safe_edit_message(progress_msg, new_text)
        
        if title:
            chapters.append((num, title, content.strip()))

        await safe_edit_message(progress_msg, "🔄 Обработка результатов...\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░ 75%")
        
        # Фильтруем пустые главы и дубликаты
        seen, result = set(), []
        for n, t, c in sorted(chapters, key=lambda x: x[0]):
            if t not in seen and len(c.strip()) > len(f"<h1>{t}</h1>"):  # Проверяем что есть контент кроме заголовка
                result.append((n, t, c))
                seen.add(t)
        
        await safe_edit_message(progress_msg, f"✅ Найдено глав: {len(result)}\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
        await asyncio.sleep(1)
        await progress_msg.delete()
        last_message_text.pop(progress_msg.id, None)
        
        return result, images
        
    finally:
        # Очищаем временную директорию
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

async def build_epub_async(title, chapters, image_paths, output_path, progress_msg):
    book = epub.EpubBook()
    book.set_identifier("converted")
    book.set_title(title)
    book.set_language("ru")
    book.add_author("Adikia Bot")  # Изменено с Chronos Bot

    spine = ['nav']
    toc = []
    
    total_steps = len(image_paths) + len(chapters) + 3
    current_step = 0

    # Добавление изображений
    for fname, path in image_paths.items():
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        new_text = f"📚 Добавление изображений...\n{progress_bar}"
        await safe_edit_message(progress_msg, new_text)
        
        try:
            ext = os.path.splitext(fname)[1][1:].lower()
            mime = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else ext}"
            with open(path, 'rb') as f:
                book.add_item(epub.EpubItem(uid=fname, file_name=f"images/{fname}", media_type=mime, content=f.read()))
        except Exception as e:
            logging.warning(f"Could not add image {fname}: {e}")

    # Добавление глав
    for i, (num, chapter_title, html_body) in enumerate(chapters, 1):
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        new_text = f"📚 Добавление глав...\n{progress_bar}"
        await safe_edit_message(progress_msg, new_text)
        
        html = epub.EpubHtml(title=chapter_title, file_name=f"chap_{i}.xhtml", lang='ru')
        html.content = html_body
        book.add_item(html)
        spine.append(html)
        toc.append(epub.Link(html.file_name, chapter_title, f"chap_{i}"))

    # Финальная сборка
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await safe_edit_message(progress_msg, f"📚 Создание оглавления...\n{progress_bar}")
    
    book.spine = spine
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await safe_edit_message(progress_msg, f"📚 Сохранение EPUB...\n{progress_bar}")
    
    epub.write_epub(output_path, book)
    
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await safe_edit_message(progress_msg, f"📚 Завершение...\n{progress_bar}")

# Функция для извлечения глав из FB2
async def extract_chapters_from_fb2_async(fb2_path, event):
    """Извлекает главы из FB2 файла"""
    progress_msg = await event.respond("📖 Анализ структуры FB2...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    
    try:
        ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
        tree = etree.parse(fb2_path)
        root = tree.getroot()
        
        await safe_edit_message(progress_msg, "📖 Поиск глав в FB2...\n▓▓▓▓▓░░░░░░░░░░░░░░░ 25%")
        
        chapters = []
        # Ищем все секции с заголовками
        sections = root.xpath('//fb2:section[fb2:title]', namespaces=ns)
        total = len(sections)
        
        if total == 0:
            await safe_edit_message(progress_msg, "❌ Секции с заголовками не найдены")
            await asyncio.sleep(2)
            await progress_msg.delete()
            return []
        
        for idx, section in enumerate(sections):
            if idx % 5 == 0:
                progress = 25 + int((idx / total) * 50)
                progress_bar = "▓" * (progress // 5) + "░" * (20 - progress // 5)
                await safe_edit_message(progress_msg, f"📖 Обработка секций...\n{progress_bar} {progress}%")
            
            # Получаем заголовок
            title_elem = section.find('.//fb2:title', namespaces=ns)
            if title_elem is not None:
                title_text = ' '.join(title_elem.itertext()).strip()
                
                # Извлекаем содержимое секции
                content = etree.tostring(section, encoding='unicode', pretty_print=True)
                
                # Извлекаем номер главы если есть
                num_match = re.search(r'\d+', title_text)
                num = int(num_match.group()) if num_match else idx
                
                chapters.append((num, title_text, content))
        
        await safe_edit_message(progress_msg, f"✅ Найдено глав: {len(chapters)}\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
        await asyncio.sleep(1)
        await progress_msg.delete()
        last_message_text.pop(progress_msg.id, None)
        
        return chapters
        
    except Exception as e:
        logging.error(f"Error parsing FB2: {e}")
        await safe_edit_message(progress_msg, f"❌ Ошибка при обработке FB2: {e}")
        await asyncio.sleep(3)
        await progress_msg.delete()
        return []

# Функция для создания FB2 с оглавлением
async def build_fb2_with_toc_async(title, chapters, output_path, progress_msg):
    """Создает FB2 файл с правильным оглавлением"""
    ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    
    # Создаем базовую структуру FB2
    root = etree.Element('{http://www.gribuser.ru/xml/fictionbook/2.0}FictionBook', 
                        nsmap={None: ns['fb2']})
    root.set('{http://www.w3.org/2001/XMLSchema-instance}schemaLocation', 
             'http://www.gribuser.ru/xml/fictionbook/2.0 http://www.gribuser.ru/xml/fictionbook/2.0/FictionBook2.xsd')
    
    # Описание документа
    description = etree.SubElement(root, 'description')
    title_info = etree.SubElement(description, 'title-info')
    etree.SubElement(title_info, 'book-title').text = title
    etree.SubElement(title_info, 'lang').text = 'ru'
    
    # Тело документа
    body = etree.SubElement(root, 'body')
    
    total = len(chapters)
    
    # Добавляем главы
    for idx, (num, chapter_title, content) in enumerate(chapters):
        progress = int((idx / total) * 100)
        progress_bar = create_progress_bar(idx, total)
        await safe_edit_message(progress_msg, f"📚 Сборка FB2...\n{progress_bar}")
        
        # Парсим содержимое главы
        try:
            # Удаляем объявление XML если есть
            content = content.replace('<?xml version="1.0" encoding="utf-8"?>', '')
            section = etree.fromstring(content)
            # Убираем namespace из элементов
            for elem in section.iter():
                elem.tag = elem.tag.split('}')[-1] if '}' in elem.tag else elem.tag
            body.append(section)
        except Exception as e:
            logging.error(f"Could not parse chapter content: {e}")
            # Если не удалось распарсить, создаем новую секцию
            section = etree.SubElement(body, 'section')
            section_title = etree.SubElement(section, 'title')
            p = etree.SubElement(section_title, 'p')
            p.text = chapter_title
    
    # Сохраняем файл
    await safe_edit_message(progress_msg, "💾 Сохранение FB2...\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
    tree = etree.ElementTree(root)
    tree.write(output_path, encoding='utf-8', xml_declaration=True, pretty_print=True)

# Функция для извлечения глав из DOCX
async def extract_chapters_from_docx_async(docx_path, event):
    """Извлекает главы из DOCX файла по заголовкам"""
    progress_msg = await event.respond("📖 Анализ структуры DOCX...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    
    try:
        doc = Document(docx_path)
        chapters = []
        current_chapter = None
        current_content = []
        
        total = len(doc.paragraphs)
        
        for idx, para in enumerate(doc.paragraphs):
            if idx % 20 == 0:
                progress = int((idx / total) * 75)
                progress_bar = create_progress_bar(idx, total)
                await safe_edit_message(progress_msg, f"📖 Анализ параграфов...\n{progress_bar}")
            
            # Проверяем, является ли параграф заголовком главы
            text = para.text.strip()
            if text and (para.style.name.startswith('Heading') or CHAPTER_RE.match(text)):
                # Сохраняем предыдущую главу
                if current_chapter and current_content:
                    num_match = re.search(r'\d+', current_chapter)
                    num = int(num_match.group()) if num_match else len(chapters)
                    chapters.append((num, current_chapter, '\n'.join(current_content)))
                
                # Начинаем новую главу
                current_chapter = text
                current_content = []  # Не включаем заголовок в контент
            elif current_chapter and text:  # Добавляем только непустые параграфы
                current_content.append(text)
        
        # Добавляем последнюю главу
        if current_chapter and current_content:
            num_match = re.search(r'\d+', current_chapter)
            num = int(num_match.group()) if num_match else len(chapters)
            chapters.append((num, current_chapter, '\n'.join(current_content)))
        
        await safe_edit_message(progress_msg, f"✅ Найдено глав: {len(chapters)}\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
        await asyncio.sleep(1)
        await progress_msg.delete()
        last_message_text.pop(progress_msg.id, None)
        
        return chapters
        
    except Exception as e:
        logging.error(f"Error parsing DOCX: {e}")
        await safe_edit_message(progress_msg, f"❌ Ошибка при обработке DOCX: {e}")
        await asyncio.sleep(3)
        await progress_msg.delete()
        return []

# Функция для создания DOCX с оглавлением
async def build_docx_with_toc_async(title, chapters, output_path, progress_msg):
    """Создает DOCX файл с оглавлением"""
    doc = Document()
    
    # Добавляем заголовок документа
    doc.add_heading(title, 0)
    
    # Добавляем оглавление
    doc.add_heading('Оглавление', 1)
    
    total_steps = len(chapters) * 2 + 2
    current_step = 0
    
    # Создаем список глав для оглавления
    for num, chapter_title, _ in sorted(chapters, key=lambda x: x[0]):
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        await safe_edit_message(progress_msg, f"📚 Создание оглавления...\n{progress_bar}")
        
        # Добавляем пункт оглавления
        doc.add_paragraph(f"{num + 1}. {chapter_title}", style='List Number')
    
    # Добавляем разрыв страницы после оглавления
    doc.add_page_break()
    
    # Добавляем главы
    for num, chapter_title, content in sorted(chapters, key=lambda x: x[0]):
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        await safe_edit_message(progress_msg, f"📚 Добавление глав...\n{progress_bar}")
        
        # Добавляем заголовок главы
        doc.add_heading(chapter_title, 1)
        
        # Добавляем содержимое главы
        paragraphs = content.split('\n')
        for paragraph in paragraphs:
            if paragraph.strip():
                doc.add_paragraph(paragraph)
        
        # НЕ добавляем разрыв страницы после каждой главы для экономии места
    
    # Сохраняем документ
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await safe_edit_message(progress_msg, f"💾 Сохранение DOCX...\n{progress_bar}")
    
    doc.save(output_path)

# Запуск
client.start()
print("Бот запущен.")
print("Обработка файлов: строго по очереди")
client.run_until_disconnected()
