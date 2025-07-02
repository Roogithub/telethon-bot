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

# Новое, более простое регулярное выражение для поиска глав по ключевым словам
CHAPTER_RE = re.compile(
    r"(?i)^\s*("
    r"Глава|chapter|часть|пролог|аннотация|annotation|описание|предисловие от автора"
    r")", re.IGNORECASE
)


def create_progress_bar(current, total, width=20):
    if total == 0:
        return "▓" * width + " 100%"
    progress = int((current / total) * width)
    bar = "▓" * progress + "░" * (width - progress)
    percentage = int((current / total) * 100)
    return f"{bar} {percentage}%"

def should_update_progress(current, total, last_updated_percent):
    if total == 0:
        return True
    current_percent = int((current / total) * 100)
    return current_percent - last_updated_percent >= 5

async def safe_edit_message(message, new_text):
    try:
        message_id = message.id
        if message_id not in last_message_text or last_message_text[message_id] != new_text:
            await message.edit(new_text)
            last_message_text[message_id] = new_text
    except MessageNotModifiedError:
        pass
    except Exception as e:
        logging.error(f"Error editing message: {e}")

@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    await event.respond(
        "Здравствуйте. Вас приветствует Адикия — бот для работы с документами.\n\n"
        "Возможности:\n"
        "• Сжатие или удаление изображений в .epub, .fb2, .docx\n"
        "• Извлечение глав из EPUB, FB2, DOCX и пересборка с оглавлением\n\n"
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

@client.on(events.NewMessage(pattern='/extract'))
async def extract_cmd(event):
    user_mode[event.sender_id] = 'extract'
    await event.respond("Пожалуйста, отправьте файл .epub, .fb2 или .docx. Я извлеку главы и пересоберу его с оглавлением.")

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

    progress_msg = await event.respond("📥 Загрузка файла...")
    file_data = io.BytesIO()
    await client.download_media(event.message, file=file_data)
    file_data.seek(0)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
        tmp.write(file_data.read())
        tmp_path = tmp.name
    
    user_files[user_id] = (filename, tmp_path)
    await progress_msg.delete()

    if mode == 'compress' and ext in ['.epub', '.fb2', '.docx']:
        buttons = [Button.inline(label, data=label.encode()) for label in RESOLUTIONS]
        await event.respond("Выберите способ обработки изображений:", buttons=buttons)

    elif mode == 'extract' and ext in ['.epub', '.fb2', '.docx']:
        await event.respond("Файл получен. Начинаю обработку...")
        try:
            output_path = None
            caption = "Файл обработан."
            base = os.path.splitext(filename)[0]

            if ext == '.epub':
                chapters, images = await extract_chapters_from_epub_async(tmp_path, event)
                if not chapters:
                    await event.respond("Главы не найдены.")
                    return
                output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.epub")
                build_progress = await event.respond("📚 Сборка EPUB...")
                await build_epub_async(base, chapters, images, output_path, build_progress)
                caption = "✅ EPUB пересобран с оглавлением."

            elif ext == '.fb2':
                chapters = await extract_chapters_from_fb2_async(tmp_path, event)
                if not chapters:
                    await event.respond("Главы не найдены в FB2. Убедитесь, что в файле есть теги <section> с <title>.")
                    return
                output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.fb2")
                build_progress = await event.respond("📚 Сборка FB2...")
                await build_fb2_with_toc_async(base, chapters, output_path, build_progress)
                caption = "✅ FB2 пересобран с оглавлением."

            elif ext == '.docx':
                chapters = await extract_chapters_from_docx_async(tmp_path, event)
                if not chapters:
                    await event.respond("Главы не найдены в DOCX. Поиск ведется по ключевым словам в начале параграфа.")
                    return
                output_path = os.path.join(tempfile.gettempdir(), f"{base}_converted.docx")
                build_progress = await event.respond("📚 Сборка DOCX...")
                await build_docx_with_toc_async(base, chapters, output_path, build_progress)
                caption = "✅ DOCX пересобран с оглавлением."
            
            if output_path and os.path.exists(output_path):
                await build_progress.delete()
                await client.send_file(user_id, output_path, caption=caption)
                os.remove(output_path)
                
        except Exception as e:
            logging.error(f"Error processing file for extraction: {e}", exc_info=True)
            await event.respond(f"Произошла ошибка при обработке файла: {e}")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            user_mode.pop(user_id, None)
            user_files.pop(user_id, None)

@client.on(events.CallbackQuery)
async def handle_button(event):
    user_id = event.sender_id
    mode = user_mode.get(user_id)
    if not mode: return

    data = event.data.decode()
    filename, filepath = user_files.get(user_id, (None, None))
    if not filename or not os.path.exists(filepath):
        await event.answer("Файл не найден. Начните заново.", alert=True)
        return

    ext = os.path.splitext(filename)[1].lower()

    if mode == 'compress':
        resolution = RESOLUTIONS.get(data)
        await event.edit(f"⚙️ Обработка файла {filename}...")
        try:
            if ext == '.fb2':
                await process_fb2(event, user_id, filename, filepath, resolution)
            elif ext == '.docx':
                await process_docx(event, user_id, filename, filepath, resolution)
            elif ext == '.epub':
                await process_epub_compression(event, user_id, filename, filepath, resolution)
        except Exception as e:
            logging.error(f"Error during compression: {e}", exc_info=True)
            await event.edit(f"Ошибка при сжатии: {e}")
        finally:
            if os.path.exists(filepath):
                os.remove(filepath)
            user_files.pop(user_id, None)
            user_mode.pop(user_id, None)

# ... (Код функций сжатия process_fb2, process_docx, process_epub_compression остается без изменений) ...

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
    progress_msg = await event.respond(f"🖼️ Обработка изображений FB2...")
    for current, binary in enumerate(image_binaries, 1):
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
    await safe_edit_message(progress_msg, "💾 Сохранение файла...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    tree.write(out_path, encoding='utf-8', xml_declaration=True)
    await progress_msg.delete()
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(out_path)

async def process_docx(event, user_id, filename, filepath, resolution):
    doc = Document(filepath)
    changed = deleted = 0
    total = len(doc.inline_shapes)
    if total == 0:
        await event.edit("Изображения не найдены в документе.")
        return
    progress_msg = await event.respond(f"🖼️ Обработка изображений DOCX...")
    if resolution is None:
        for shape in doc.inline_shapes:
            try:
                shape._element.getparent().remove(shape._element)
                deleted += 1
            except Exception: continue
    else:
        for shape in doc.inline_shapes:
            try:
                r_id = shape._inline.graphic.graphicData.pic.blipFill.blip.embed
                img_part = doc.part.related_parts[r_id]
                img = Image.open(io.BytesIO(img_part.blob)).convert('RGB')
                img.thumbnail(resolution, Image.Resampling.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=30)
                img_part._blob = buf.getvalue()
                changed += 1
            except Exception: continue
    await safe_edit_message(progress_msg, "💾 Сохранение документа...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    doc.save(out_path)
    await progress_msg.delete()
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(out_path)

async def process_epub_compression(event, user_id, filename, filepath, resolution):
    book = epub.read_epub(filepath)
    changed = deleted = 0
    images = [item for item in list(book.get_items()) if item.media_type and item.media_type.startswith("image/")]
    total = len(images)
    if total == 0:
        await event.edit("Изображения не найдены в EPUB.")
        return
    progress_msg = await event.respond(f"🖼️ Обработка изображений EPUB...")
    for item in images:
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
            except Exception: continue
    await safe_edit_message(progress_msg, "💾 Сохранение EPUB...")
    base, ext = os.path.splitext(filename)
    out_path = os.path.join(tempfile.gettempdir(), f"{base}_compressed{ext}")
    epub.write_epub(out_path, book)
    await progress_msg.delete()
    await client.send_file(user_id, out_path, caption=f"✅ Готово: сжато {changed}, удалено {deleted}")
    os.remove(out_path)
    
# ... (Функция extract_chapters_from_epub_async остается без изменений) ...
async def extract_chapters_from_epub_async(epub_path, event):
    # ... (старый код без изменений)
    return result, images

async def build_epub_async(title, chapters, image_paths, output_path, progress_msg):
    # ... (старый код без изменений)
    pass

# ---- ИСПРАВЛЕННЫЕ ФУНКЦИИ ----

async def extract_chapters_from_fb2_async(fb2_path, event):
    """Извлекает главы из FB2, считая каждую секцию с заголовком главой."""
    await safe_edit_message(event, "📖 Анализ структуры FB2...")
    ns = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    tree = etree.parse(fb2_path)
    root = tree.getroot()
    
    chapters = []
    # Ищем все секции, у которых есть заголовок
    sections = root.xpath('//fb2:section[fb2:title]', namespaces=ns)
    
    for idx, section in enumerate(sections):
        title_elem = section.find('fb2:title', namespaces=ns)
        if title_elem is not None:
            # Получаем полный текст заголовка
            title_text = ' '.join(title_elem.itertext()).strip()
            # Получаем полное содержимое секции как строку
            content = etree.tostring(section, encoding='unicode', method='xml')
            chapters.append((idx, title_text, content))
            
    await event.edit(f"✅ Найдено глав в FB2: {len(chapters)}")
    await asyncio.sleep(1)
    return chapters

async def build_fb2_with_toc_async(title, chapters, output_path, progress_msg):
    """Собирает FB2 из готовых секций-глав."""
    await safe_edit_message(progress_msg, "📚 Создание структуры FB2...")
    nsmap = {'fb2': 'http://www.gribuser.ru/xml/fictionbook/2.0'}
    root = etree.Element('FictionBook', nsmap=nsmap)
    
    description = etree.SubElement(root, 'description')
    title_info = etree.SubElement(description, 'title-info')
    etree.SubElement(title_info, 'book-title').text = title
    etree.SubElement(title_info, 'lang').text = 'ru'
    
    body = etree.SubElement(root, 'body')
    
    for idx, (num, chapter_title, content_xml) in enumerate(chapters):
        try:
            # Парсим XML-строку главы и добавляем ее в тело
            section_node = etree.fromstring(content_xml)
            body.append(section_node)
        except Exception as e:
            logging.error(f"Could not parse chapter '{chapter_title}': {e}")
            continue # Пропускаем главу, если не удалось ее распарсить
            
    await safe_edit_message(progress_msg, "💾 Сохранение FB2...")
    tree = etree.ElementTree(root)
    tree.write(output_path, encoding='utf-8', xml_declaration=True, pretty_print=True)

async def extract_chapters_from_docx_async(docx_path, event):
    """Извлекает главы из DOCX файла по ключевым словам в начале параграфа."""
    await safe_edit_message(event, "📖 Анализ структуры DOCX...")
    doc = Document(docx_path)
    chapters = []
    current_chapter_title = None
    current_content = []

    for para in doc.paragraphs:
        # Проверяем, является ли параграф заголовком главы
        if CHAPTER_RE.match(para.text.strip()):
            # Если это новый заголовок, сохраняем предыдущую главу
            if current_chapter_title is not None:
                chapters.append((len(chapters), current_chapter_title, "\n".join(current_content)))
                current_content = []
            
            # Начинаем новую главу
            current_chapter_title = para.text.strip()
        
        # Если мы внутри главы, добавляем текст
        if current_chapter_title is not None:
            current_content.append(para.text)
    
    # Добавляем последнюю найденную главу
    if current_chapter_title is not None:
        chapters.append((len(chapters), current_chapter_title, "\n".join(current_content)))
        
    await event.edit(f"✅ Найдено глав в DOCX: {len(chapters)}")
    await asyncio.sleep(1)
    return chapters

async def build_docx_with_toc_async(title, chapters, output_path, progress_msg):
    """Создает DOCX файл с оглавлением без лишних разрывов страниц."""
    doc = Document()
    doc.add_heading(title, 0)
    
    await safe_edit_message(progress_msg, "📚 Создание оглавления DOCX...")
    doc.add_heading('Оглавление', 1)
    for num, chapter_title, _ in sorted(chapters, key=lambda x: x[0]):
        # Добавляем пункт оглавления как обычный параграф
        doc.add_paragraph(f"{chapter_title}")
    
    await safe_edit_message(progress_msg, "📚 Добавление глав DOCX...")
    for num, chapter_title, content in sorted(chapters, key=lambda x: x[0]):
        doc.add_heading(chapter_title, 1)
        # Добавляем параграфы главы
        for paragraph_text in content.split('\n'):
             if paragraph_text.strip(): # Добавляем только непустые параграфы
                doc.add_paragraph(paragraph_text)
                
    await safe_edit_message(progress_msg, "💾 Сохранение DOCX...")
    doc.save(output_path)

# Запуск
client.start()
print("Бот запущен.")
client.run_until_disconnected()
                
