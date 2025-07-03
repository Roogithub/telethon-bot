import asyncio
from telethon import TelegramClient, events
from telethon.tl.functions.channels import CreateForumTopicRequest
from telethon.tl.types import DocumentAttributeImageSize
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Ваши данные API
API_ID = 24519852
API_HASH = '2186f59fdf9c2ad4e7ddf0deb250ff0c'

# ID группы (суперgroup)
GROUP_ID = -1002756641199

# Создание клиента
client = TelegramClient('topic_bot_session', API_ID, API_HASH)

# СПИСОК ТЕЗИСОВ - ВСТАВЬТЕ СЮДА ВАШ СПИСОК
TOPIC_LIST = [
    # Пример формата:
    # "2 Название первой темы",
    # "3 Название второй темы",
    # "4 Название третьей темы",
    # ... добавьте здесь ваш список
]

def load_topics_from_file(filename):
    """Загружает список тем из файла"""
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            topics = [line.strip() for line in f if line.strip()]
        logger.info(f"Загружено {len(topics)} тем из файла {filename}")
        return topics
    except FileNotFoundError:
        logger.error(f"Файл {filename} не найден")
        return []
    except Exception as e:
        logger.error(f"Ошибка при чтении файла: {e}")
        return []

async def create_topic_from_text(topic_text):
    """Создает тему по тексту из списка"""
    try:
        # Получаем сущность группы
        group = await client.get_entity(GROUP_ID)
        
        # Используем весь текст как название темы
        title = topic_text.strip()
        
        # Ограничиваем длину названия (Telegram лимит ~300 символов)
        if len(title) > 200:
            title = title[:197] + "..."
        
        # Создаем тему
        result = await client(CreateForumTopicRequest(
            channel=group,
            title=title,
            icon_color=None,
            icon_emoji_id=None
        ))
        
        logger.info(f"Тема '{title}' создана успешно!")
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при создании темы '{topic_text}': {e}")
        return None

async def create_topics_from_list(topics_list=None, start_index=0, end_index=None):
    """Создает темы из списка"""
    if topics_list is None:
        topics_list = TOPIC_LIST
    
    if not topics_list:
        logger.error("Список тем пуст!")
        return 0, 0
    
    # Определяем диапазон
    if end_index is None:
        end_index = len(topics_list)
    
    topics_to_create = topics_list[start_index:end_index]
    total_count = len(topics_to_create)
    
    successful = 0
    failed = 0
    
    logger.info(f"Начинаю создание {total_count} тем (с {start_index+1} по {end_index})...")
    
    for i, topic_text in enumerate(topics_to_create, 1):
        try:
            result = await create_topic_from_text(topic_text)
            
            if result:
                successful += 1
                logger.info(f"✅ Создана тема {i}/{total_count}: {topic_text[:50]}...")
            else:
                failed += 1
                logger.warning(f"❌ Не удалось создать тему {i}/{total_count}: {topic_text[:50]}...")
            
            # Задержка между созданием тем (12 секунд)
            if i < total_count:
                logger.info(f"⏳ Ждем 12 секунд перед созданием следующей темы...")
                await asyncio.sleep(12)
                
        except Exception as e:
            failed += 1
            logger.error(f"❌ Критическая ошибка при создании темы {i}: {e}")
            
            # При критической ошибке ждем дольше
            if "FLOOD_WAIT" in str(e):
                wait_time = 60
                logger.warning(f"⚠️ Флуд-контроль! Ждем {wait_time} секунд...")
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(12)
    
    logger.info(f"🏁 Завершено! Успешно создано: {successful}, Ошибок: {failed}")
    return successful, failed

@client.on(events.NewMessage(pattern='/create_from_list'))
async def handler_create_from_list(event):
    """Создает все темы из списка"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    if not TOPIC_LIST:
        await event.respond("❌ Список тем пуст! Добавьте темы в переменную TOPIC_LIST")
        return
    
    total_topics = len(TOPIC_LIST)
    estimated_time = (total_topics * 12) // 60
    
    await event.respond(
        f"🚀 Начинаю создание {total_topics} тем из списка!\n"
        f"⏱️ Примерное время: {estimated_time} минут"
    )
    
    successful, failed = await create_topics_from_list()
    
    result_message = (
        f"🏁 **Создание тем из списка завершено!**\n\n"
        f"✅ Успешно создано: {successful} тем\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Общий процент успеха: {(successful/(successful+failed)*100):.1f}%"
    )
    
    await event.respond(result_message)

@client.on(events.NewMessage(pattern='/create_from_file'))
async def handler_create_from_file(event):
    """Создает темы из файла topics.txt"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    # Загружаем темы из файла
    topics = load_topics_from_file('/storage/emulated/0/Bots/topics.txt')
    
    if not topics:
        await event.respond("❌ Не удалось загрузить темы из файла topics.txt")
        return
    
    total_topics = len(topics)
    estimated_time = (total_topics * 12) // 60
    
    await event.respond(
        f"🚀 Начинаю создание {total_topics} тем из файла!\n"
        f"⏱️ Примерное время: {estimated_time} минут"
    )
    
    successful, failed = await create_topics_from_list(topics)
    
    result_message = (
        f"🏁 **Создание тем из файла завершено!**\n\n"
        f"✅ Успешно создано: {successful} тем\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Общий процент успеха: {(successful/(successful+failed)*100):.1f}%"
    )
    
    await event.respond(result_message)

@client.on(events.NewMessage(pattern=r'/create_range_(\d+)_(\d+)'))
async def handler_create_range(event):
    """Создает темы в диапазоне от N до M"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    start_num = int(event.pattern_match.group(1)) - 1  # -1 для индекса массива
    end_num = int(event.pattern_match.group(2))
    
    if not TOPIC_LIST:
        await event.respond("❌ Список тем пуст!")
        return
    
    if start_num < 0 or end_num > len(TOPIC_LIST) or start_num >= end_num:
        await event.respond(f"❌ Некорректный диапазон. Доступно тем: {len(TOPIC_LIST)}")
        return
    
    count = end_num - start_num
    estimated_time = (count * 12) // 60
    
    await event.respond(
        f"🚀 Создаю темы с {start_num+1} по {end_num} ({count} тем)\n"
        f"⏱️ Примерное время: {estimated_time} минут"
    )
    
    successful, failed = await create_topics_from_list(TOPIC_LIST, start_num, end_num)
    
    result_message = (
        f"🏁 **Создание диапазона тем завершено!**\n\n"
        f"✅ Успешно создано: {successful} тем\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Общий процент успеха: {(successful/(successful+failed)*100):.1f}%"
    )
    
    await event.respond(result_message)

@client.on(events.NewMessage(pattern='/info'))
async def handler_info(event):
    """Показывает информацию о загруженных темах"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    info_text = f"📋 **Информация о темах:**\n\n"
    
    if TOPIC_LIST:
        info_text += f"✅ Загружено тем из кода: {len(TOPIC_LIST)}\n"
        info_text += f"📝 Первые 3 темы из списка:\n"
        for i, topic in enumerate(TOPIC_LIST[:3]):
            info_text += f"   {i+1}. {topic[:50]}...\n"
    else:
        info_text += "❌ Список тем в коде пуст\n"
    
    # Проверяем файл
    file_topics = load_topics_from_file('topics.txt')
    if file_topics:
        info_text += f"\n📁 Найден файл topics.txt: {len(file_topics)} тем"
    else:
        info_text += f"\n❌ Файл topics.txt не найден"
    
    await event.respond(info_text)

# Старые команды остаются без изменений
async def create_experiment_topic(topic_number=None):
    """Создает тему с названием 'Эксперимент'"""
    try:
        group = await client.get_entity(GROUP_ID)
        
        if topic_number:
            title = f"Эксперимент #{topic_number}"
        else:
            title = "Эксперимент"
        
        result = await client(CreateForumTopicRequest(
            channel=group,
            title=title,
            icon_color=None,
            icon_emoji_id=None
        ))
        
        logger.info(f"Тема '{title}' создана успешно!")
        return result
        
    except Exception as e:
        logger.error(f"Ошибка при создании темы: {e}")
        return None

async def create_multiple_topics(count=150):
    """Создает несколько тем с задержками"""
    successful = 0
    failed = 0
    
    logger.info(f"Начинаю создание {count} тем...")
    
    for i in range(1, count + 1):
        try:
            result = await create_experiment_topic(i)
            
            if result:
                successful += 1
                logger.info(f"✅ Создана тема {i}/{count} (успешно: {successful})")
            else:
                failed += 1
                logger.warning(f"❌ Не удалось создать тему {i}/{count} (ошибок: {failed})")
            
            if i < count:
                logger.info(f"⏳ Ждем 12 секунд перед созданием следующей темы...")
                await asyncio.sleep(12)
                
        except Exception as e:
            failed += 1
            logger.error(f"❌ Критическая ошибка при создании темы {i}: {e}")
            
            if "FLOOD_WAIT" in str(e):
                wait_time = 60
                logger.warning(f"⚠️ Флуд-контроль! Ждем {wait_time} секунд...")
                await asyncio.sleep(wait_time)
            else:
                await asyncio.sleep(12)
    
    logger.info(f"🏁 Завершено! Успешно создано: {successful}, Ошибок: {failed}")
    return successful, failed

@client.on(events.NewMessage(pattern='/create_topic'))
async def handler_create_topic(event):
    """Обработчик команды создания темы"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    result = await create_experiment_topic()
    
    if result:
        await event.respond("✅ Тема 'Эксперимент' создана успешно!")
    else:
        await event.respond("❌ Ошибка при создании темы. Проверьте права бота.")

@client.on(events.NewMessage(pattern='/create_150'))
async def handler_create_150(event):
    """Обработчик команды создания 150 тем"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    await event.respond("🚀 Начинаю создание 150 тем! Это займет около 30 минут...")
    
    successful, failed = await create_multiple_topics(150)
    
    result_message = (
        f"🏁 **Создание тем завершено!**\n\n"
        f"✅ Успешно создано: {successful} тем\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Общий процент успеха: {(successful/(successful+failed)*100):.1f}%"
    )
    
    await event.respond(result_message)

@client.on(events.NewMessage(pattern=r'/create_(\d+)'))
async def handler_create_custom(event):
    """Обработчик команды создания N тем"""
    if event.chat_id != GROUP_ID:
        await event.respond("Эта команда работает только в указанной группе.")
        return
    
    count = int(event.pattern_match.group(1))
    
    if count > 200:
        await event.respond("⚠️ Максимальное количество тем: 200")
        return
    
    if count < 1:
        await event.respond("⚠️ Количество тем должно быть больше 0")
        return
    
    estimated_time = (count * 12) // 60
    
    await event.respond(
        f"🚀 Начинаю создание {count} тем!\n"
        f"⏱️ Примерное время: {estimated_time} минут"
    )
    
    successful, failed = await create_multiple_topics(count)
    
    result_message = (
        f"🏁 **Создание {count} тем завершено!**\n\n"
        f"✅ Успешно создано: {successful} тем\n"
        f"❌ Ошибок: {failed}\n"
        f"📊 Общий процент успеха: {(successful/(successful+failed)*100):.1f}%"
    )
    
    await event.respond(result_message)

@client.on(events.NewMessage(pattern='/start'))
async def handler_start(event):
    """Обработчик команды /start"""
    await event.respond(
        "🤖 Бот для создания тем готов к работе!\n\n"
        "Команды:\n"
        "/create_topic - создать тему 'Эксперимент'\n"
        "/help - показать помощь"
    )

@client.on(events.NewMessage(pattern='/help'))
async def handler_help(event):
    """Обработчик команды помощи"""
    help_text = """
🔧 **Справка по боту**

**Старые команды:**
• `/create_topic` - создать одну тему "Эксперимент" 
• `/create_150` - создать 150 тем "Эксперимент #1-150"
• `/create_N` - создать N тем (например: /create_50)

**Новые команды для списка тем:**
• `/create_from_list` - создать все темы из списка в коде
• `/create_from_file` - создать темы из файла topics.txt
• `/create_range_N_M` - создать темы с N по M (например: /create_range_1_100)
• `/info` - информация о загруженных темах

**Справка:**
• `/start` - начать работу с ботом
• `/help` - показать эту справку

**⚠️ Как добавить ваш список:**
1. Вставьте темы в переменную TOPIC_LIST в коде
2. Или создайте файл topics.txt с темами (по одной на строку)

**Требования:**
• Бот должен быть администратором в группе
• Группа должна поддерживать темы (Topics/Forum)
• Бот должен иметь права на создание тем

**Примечание:** Команды работают только в настроенной группе.
    """
    await event.respond(help_text)

async def main():
    """Основная функция запуска бота"""
    try:
        await client.start()
        
        me = await client.get_me()
        logger.info(f"Бот запущен: @{me.username}")
        
        try:
            group = await client.get_entity(GROUP_ID)
            logger.info(f"Подключен к группе: {group.title}")
        except Exception as e:
            logger.error(f"Не удалось подключиться к группе: {e}")
        
        print("Бот запущен и готов к работе!")
        print("Нажмите Ctrl+C для остановки")
        
        await client.run_until_disconnected()
        
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {e}")

if __name__ == '__main__':
    asyncio.run(main())
