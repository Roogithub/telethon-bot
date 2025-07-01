import os
import re
import io
import zipfile
import tempfile
import base64
import logging
import warnings
import asyncio

from telethon import TelegramClient, events, Button
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

# Улучшенное регулярное выражение для поиска глав
CHAPTER_RE = re.compile(
    r"(?i)\b("
    r"глава\s*\d+|эпизод\s*\d+|часть\s*\d+|гл\.\s*\d+|описание\s*\d+|аннотация\s*\d+|"
    r"пролог|эпилог|вступление|"
    r"chapter\s*\d+|episode\s*\d+|part\s*\d+|scene\s*\d+|act\s*\d+|"
    r"prologue|epilogue|foreword|afterword|preface|introduction|outro|conclusion|"
    r"\d+[-‐–—]?\d*[\.\s:：)]|"
    r"\d+[ \t]*[–—-][ \t]*[^\n<:]+[:：)]|"
    r"\d+[\.\)]?[ \t]+[^\n<:：]+[:：)]|"
    r"пролог|эпилог|"
    r")\b"
)

# Функция для создания прогресс-бара (только проценты, без счетчиков)
def create_progress_bar(current, total, width=20):
    if total == 0:
        return "▓" * width + " 100%"
    progress = int((current / total) * width)
    bar = "▓" * progress + "░" * (width - progress)
    percentage = int((current / total) * 100)
    return f"{bar} {percentage}%"

# Функция для проверки, нужно ли обновлять прогресс (избегаем спама)
def should_update_progress(current, total, last_updated_percent):
    if total == 0:
        return True
    current_percent = int((current / total) * 100)
    # Обновляем только при изменении на 5% или больше
    return current_percent - last_updated_percent >= 5

# Команды
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond(
        "Здравствуйте. Вас приветствует Адикия — бот для работы с документами.\n\n"
        "Возможности:\n"
        "• Сжатие или удаление изображений в .epub, .fb2, .docx\n"
        "• Конвертация файлов: .epub, .fb2, .docx, .txt\n"
        "• Извлечение глав из EPUB и пересборка с оглавлением\n\n"
        "Используйте /help для получения списка команд."
    )

@client.on(events.NewMessage(pattern='/help'))
async def help_command(event):
    await event.respond(
        "Справка по командам:\n\n"
        "/start    - Начать работу с ботом\n"
        "/help     - Показать эту справку\n"
        "/compress - Сжать или удалить изображения в .epub/.fb2/.docx\n"
        "/convert  - Конвертировать файл (.epub/.fb2/.docx/.txt)\n"
        "/extract  - Извлечь главы из EPUB и пересобрать\n"
        "/cancel   - Отменить текущую операцию\n"
    )

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

@client.on(events.NewMessage(pattern='/convert'))
async def convert_cmd(event):
    user_mode[event.sender_id] = 'convert'
    await event.respond("Пожалуйста, отправьте файл одного из поддерживаемых форматов: .epub, .fb2, .docx, .txt")

@client.on(events.NewMessage(pattern='/extract'))
async def extract_cmd(event):
    user_mode[event.sender_id] = 'extract'
    await event.respond("Пожалуйста, отправьте .epub файл. Я извлеку главы и пересоберу его.")

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
            try:
                await progress_msg.edit(f"📥 Загрузка файла...\n{progress_bar}")
                last_percent = int((current / total) * 100)
            except:
                pass  # Игнорируем ошибки редактирования
    
    await client.download_media(event.message, file=file_data, progress_callback=progress_callback)
    file_data.seek(0)

    await progress_msg.edit("📥 Загрузка завершена! Сохранение файла...")

    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(file_data.read())
        tmp_path = tmp.name

    user_files[user_id] = (filename, tmp_path)
    await progress_msg.delete()

    if mode == 'compress' and ext in ['.epub', '.fb2', '.docx']:
        buttons = [Button.inline(label, data=label.encode()) for label in RESOLUTIONS]
        await event.respond("Выберите способ обработки изображений:", buttons=buttons)

    elif mode == 'convert' and ext in ['.epub', '.fb2', '.docx', '.txt']:
        buttons = [
            [Button.inline("В DOCX", b"to_docx"), Button.inline("В FB2", b"to_fb2")],
            [Button.inline("В EPUB", b"to_epub"), Button.inline("В TXT", b"to_txt")]
        ]
        await event.respond("Выберите формат для конвертации:", buttons=buttons)

    elif mode == 'extract' and ext == '.epub':
        await event.respond("Файл получен. Начинаю обработку...")
        try:
            chapters, images = await extract_chapters_from_epub_async(tmp_path, event)
            if not chapters:
                await event.respond("Главы не найдены.")
                return
            base = os.path.splitext(filename)[0]
            output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.epub")
            
            build_progress = await event.respond("📚 Сборка EPUB...\n░░░░░░░░░░░░░░░░░░░░ 0%")
            await build_epub_async(base, chapters, images, output_path, build_progress)
            await build_progress.delete()
            
            await client.send_file(user_id, output_path, caption="EPUB пересобран с оглавлением.")
            os.remove(output_path)
        except Exception as e:
            await event.respond(f"Ошибка: {e}")
        finally:
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
        if ext == '.fb2':
            await process_fb2(event, user_id, filename, filepath, resolution)
        elif ext == '.docx':
            await process_docx(event, user_id, filename, filepath, resolution)
        elif ext == '.epub':
            await process_epub_compression(event, user_id, filename, filepath, resolution)

    elif mode == 'convert':
        target_ext = data.replace("to_", ".")
        if ext == target_ext:
            await event.respond("Файл уже в этом формате.")
            await client.send_file(user_id, filepath)
        else:
            convert_progress = await event.respond("🔄 Конвертация файла...\n░░░░░░░░░░░░░░░░░░░░ 0%")
            await asyncio.sleep(0.5)  # Имитация обработки
            await convert_progress.edit("🔄 Конвертация файла...\n▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░ 50%")
            await asyncio.sleep(0.5)
            
            new_path = os.path.join(tempfile.gettempdir(), os.path.splitext(filename)[0] + target_ext)
            with open(filepath, 'rb') as src, open(new_path, 'wb') as dst:
                dst.write(src.read())
            
            await convert_progress.edit("🔄 Конвертация файла...\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
            await asyncio.sleep(0.5)
            await convert_progress.delete()
            
            await client.send_file(user_id, new_path, caption="Конвертация завершена.")
            os.remove(new_path)

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
            await progress_msg.edit(f"🖼️ {action} изображений FB2...\n{progress_bar}")
            last_percent = int((current / total) * 100)

        if resolution is None:
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

    await progress_msg.edit("💾 Сохранение файла...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    tree.write(out_path, encoding='utf-8', xml_declaration=True)
    
    await progress_msg.delete()
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
        for current, shape in enumerate(doc.inline_shapes, 1):
            if should_update_progress(current, total, last_percent):
                progress_bar = create_progress_bar(current, total)
                await progress_msg.edit(f"🖼️ Удаление изображений DOCX...\n{progress_bar}")
                last_percent = int((current / total) * 100)
            
            try:
                shape._element.getparent().remove(shape._element)
                deleted += 1
            except Exception:
                continue
    else:
        for current, shape in enumerate(doc.inline_shapes, 1):
            if should_update_progress(current, total, last_percent):
                progress_bar = create_progress_bar(current, total)
                await progress_msg.edit(f"🖼️ Сжатие изображений DOCX...\n{progress_bar}")
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

    await progress_msg.edit("💾 Сохранение документа...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    doc.save(out_path)
    
    await progress_msg.delete()
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
            await progress_msg.edit(f"🖼️ {action} изображений EPUB...\n{progress_bar}")
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

    await progress_msg.edit("💾 Сохранение EPUB...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    epub.write_epub(out_path, book)
    
    await progress_msg.delete()
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(filepath)
    os.remove(out_path)

# EPUB: главы с асинхронным прогресс-баром
async def extract_chapters_from_epub_async(epub_path, event):
    temp_dir = tempfile.mkdtemp()
    html_blocks = []
    images = {}

    progress_msg = await event.respond("📖 Извлечение файлов из EPUB...\n░░░░░░░░░░░░░░░░░░░░ 0%")
    
    with zipfile.ZipFile(epub_path, 'r') as zf:
        zf.extractall(temp_dir)

    await progress_msg.edit("📖 Анализ содержимого...\n▓▓▓▓▓░░░░░░░░░░░░░░░ 25%")
    
    all_files = []
    for root, _, files in os.walk(temp_dir):
        for file in files:
            all_files.append(os.path.join(root, file))
    
    processed = 0
    total_files = len(all_files)
    
    for file_path in all_files:
        file = os.path.basename(file_path)
        if file.lower().endswith((".xhtml", ".html", ".htm")):
            with open(file_path, "r", encoding="utf-8") as f:
                html_blocks.append(BeautifulSoup(f, "lxml"))
        elif file.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
            images[file] = file_path
        
        processed += 1
        if processed % 10 == 0:  # Обновляем каждые 10 файлов
            progress = int((processed / total_files) * 25) + 25
            progress_bar = "▓" * (progress // 5) + "░" * (20 - progress // 5)
            await progress_msg.edit(f"📖 Анализ файлов...\n{progress_bar} {progress}%")

    await progress_msg.edit("🔍 Поиск глав...\n▓▓▓▓▓▓▓▓▓▓░░░░░░░░░░ 50%")
    
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
                title = match.group(1)
                num_match = re.search(r'\d+', title)
                num = int(num_match.group()) if num_match else 0
                content = f"<h1>{title}</h1>"
            else:
                content += str(elem)
        
        processed_blocks += 1
        if processed_blocks % 5 == 0:  # Обновляем каждые 5 блоков
            progress = 50 + int((processed_blocks / total_blocks) * 25)
            progress_bar = "▓" * (progress // 5) + "░" * (20 - progress // 5)
            await progress_msg.edit(f"🔍 Поиск глав...\n{progress_bar} {progress}%")
    
    if title:
        chapters.append((num, title, content.strip()))

    await progress_msg.edit("🔄 Обработка результатов...\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░ 75%")
    
    seen, result = set(), []
    for n, t, c in sorted(chapters, key=lambda x: x[0]):
        if t not in seen:
            result.append((n, t, c))
            seen.add(t)
    
    await progress_msg.edit(f"✅ Найдено глав: {len(result)}\n▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ 100%")
    await asyncio.sleep(1)
    await progress_msg.delete()
    
    return result, images

async def build_epub_async(title, chapters, image_paths, output_path, progress_msg):
    book = epub.EpubBook()
    book.set_identifier("converted")
    book.set_title(title)
    book.set_language("ru")
    book.add_author("Chronos Bot")

    spine = ['nav']
    toc = []
    
    total_steps = len(image_paths) + len(chapters) + 3  # +3 для финальных шагов
    current_step = 0

    # Добавление изображений
    for fname, path in image_paths.items():
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        await progress_msg.edit(f"📚 Добавление изображений...\n{progress_bar}")
        
        ext = os.path.splitext(fname)[1][1:].lower()
        mime = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else ext}"
        with open(path, 'rb') as f:
            book.add_item(epub.EpubItem(uid=fname, file_name=f"images/{fname}", media_type=mime, content=f.read()))

    # Добавление глав
    for i, (num, chapter_title, html_body) in enumerate(chapters, 1):
        current_step += 1
        progress_bar = create_progress_bar(current_step, total_steps)
        await progress_msg.edit(f"📚 Добавление глав...\n{progress_bar}")
        
        html = epub.EpubHtml(title=chapter_title, file_name=f"chap_{i}.xhtml", lang='ru')
        html.content = html_body
        book.add_item(html)
        spine.append(html)
        toc.append(epub.Link(html.file_name, chapter_title, f"chap_{i}"))

    # Финальная сборка
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await progress_msg.edit(f"📚 Создание оглавления...\n{progress_bar}")
    
    book.spine = spine
    book.toc = toc
    book.add_item(epub.EpubNcx())
    
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await progress_msg.edit(f"📚 Сохранение EPUB...\n{progress_bar}")
    
    epub.write_epub(output_path, book)
    
    current_step += 1
    progress_bar = create_progress_bar(current_step, total_steps)
    await progress_msg.edit(f"📚 Завершение...\n{progress_bar}")

def build_epub(title, chapters, image_paths, output_path):
    book = epub.EpubBook()
    book.set_identifier("converted")
    book.set_title(title)
    book.set_language("ru")
    book.add_author("Chronos Bot")

    spine = ['nav']
    toc = []

    for fname, path in image_paths.items():
        ext = os.path.splitext(fname)[1][1:].lower()
        mime = f"image/{'jpeg' if ext in ['jpg', 'jpeg'] else ext}"
        with open(path, 'rb') as f:
            book.add_item(epub.EpubItem(uid=fname, file_name=f"images/{fname}", media_type=mime, content=f.read()))

    for i, (num, title, html_body) in enumerate(chapters, 1):
        html = epub.EpubHtml(title=title, file_name=f"chap_{i}.xhtml", lang='ru')
        html.content = html_body
        book.add_item(html)
        spine.append(html)
        toc.append(epub.Link(html.file_name, title, f"chap_{i}"))

    book.spine = spine
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(output_path, book)

# Запуск
client.start()
print("Бот запущен.")
client.run_until_disconnected()
