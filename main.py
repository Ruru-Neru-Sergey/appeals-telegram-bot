"""
Бот для приёма обращений жителей посёлка.

Житель выбирает тему, прикладывает фото или видео, описывает проблему и указывает
место. Готовое обращение уходит администраторам, а они отвечают жителю прямо из бота.
"""

import logging
import sqlite3
import threading
from dataclasses import dataclass
from typing import Optional

import requests
import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("appeals-bot")

bot = telebot.TeleBot(config.TOKEN)

DB_PATH = "users.db"
GEOCODER_URL = "https://geocode-maps.yandex.ru/1.x/"

# Подпись к фото в Telegram ограничена 1024 символами, поэтому текст режем заранее
MAX_TEXT_LENGTH = 800
MAX_ADDRESS_LENGTH = 200
MAX_TOPIC_LENGTH = 60

# Тема обращения -> код, который добавляется к номеру обращения
TOPICS = {"ЖКХ": 111, "Мусор": 222, "Транспорт": 333}
CUSTOM_TOPIC_CODE = 444  # код для темы, которую житель написал сам


# --------------------------------------------------------------------------
# Тексты и кнопки
# --------------------------------------------------------------------------

CB_NEW_APPEAL = "new_appeal"

BTN_MAIN_MENU = "Назад в главное меню"
BTN_BACK = "Назад к выбору темы обращения"
BTN_RESTART = "Заново написать обращение"
BTN_CUSTOM_TOPIC = "Своя тема"
BTN_CONTINUE = "Продолжить"
BTN_MEDIA_OK = "Да, это"
BTN_MEDIA_AGAIN = "Отправить заново"
BTN_MANUAL_ADDRESS = "Написать местоположение вручную"
BTN_ADDRESS_OK = "Да, верно"
BTN_ADDRESS_AGAIN = "Указать местоположение заново"
BTN_SEND = "Отправить обращение"
BTN_REPLY_TO_USER = "Ввести айди пользователя"

GREETING = (
    "Привет, {name}!\n\n"
    "Я бот, в котором Вы можете описать проблему, связанную с посёлком, "
    "а я отошлю ее администрации.\n"
    "Для использования бота необходимо соблюдать чёткую последовательность действий.\n\n"
    "Ниже представлено главное меню:"
)

TOPICS_TEXT = (
    "В данный момент мы принимаем обращения, связанные с посёлком (Романовка), "
    "по следующим темам:\n"
    "1) ЖКХ (к нему относится: водоснабжение и электроснабжение)\n"
    "2) Мусор (к нему относится: несанкционированные свалки)\n"
    "3) Транспорт (например, отсутствие автобусов, которые следуют чётко "
    "расписанию, указанному на сайте их организации)\n\n"
    "Если Ваш вопрос сюда не подходит, нажмите «Своя тема» и напишите её сами.\n\n"
    "Выберите тему обращения"
)

RULES_TEXT = (
    "Перед описанием проблемы, ознакомьтесь с правилами оформления заявки:\n\n"
    "1) Пришлите фото или видео\n"
    "2) Опишите саму проблему\n"
    "3) Нажмите кнопку «Отправить местоположение» или «Написать местоположение вручную»\n\n"
    "Если Вы готовы, нажмите кнопку «Продолжить»"
)


# --------------------------------------------------------------------------
# Состояние диалога
# --------------------------------------------------------------------------

class Step:
    """Шаги диалога. Для каждого шага ниже есть свой обработчик."""
    IDLE = "idle"
    CHOOSING_TOPIC = "choosing_topic"
    WAITING_CUSTOM_TOPIC = "waiting_custom_topic"
    READING_RULES = "reading_rules"
    WAITING_MEDIA = "waiting_media"
    CONFIRMING_MEDIA = "confirming_media"
    WAITING_TEXT = "waiting_text"
    WAITING_LOCATION = "waiting_location"
    WAITING_ADDRESS = "waiting_address"
    CONFIRMING_ADDRESS = "confirming_address"
    CONFIRMING_SEND = "confirming_send"
    ADMIN_USER_ID = "admin_user_id"
    ADMIN_APPEAL_ID = "admin_appeal_id"
    ADMIN_REPLY = "admin_reply"


@dataclass
class Session:
    """Черновик обращения одного пользователя (хранится в памяти)."""
    step: str = Step.IDLE
    topic: Optional[str] = None
    topic_code: Optional[int] = None
    media_kind: Optional[str] = None  # "photo" или "video"
    media_id: Optional[str] = None
    text: Optional[str] = None
    address: Optional[str] = None
    # для ответа администратора
    reply_user_id: Optional[int] = None
    reply_appeal_id: Optional[str] = None

    def reset(self):
        self.step = Step.IDLE
        self.topic = self.topic_code = self.media_kind = self.media_id = None
        self.text = self.address = None
        self.reply_user_id = self.reply_appeal_id = None


sessions: dict = {}


def get_session(chat_id: int) -> Session:
    return sessions.setdefault(chat_id, Session())


# --------------------------------------------------------------------------
# База данных
# --------------------------------------------------------------------------

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()


def init_db():
    with db_lock, db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS users(
                chatid TEXT,
                username TEXT,
                id INTEGER PRIMARY KEY AUTOINCREMENT
            )"""
        )


def get_user_number(chat_id: int, username: Optional[str]) -> int:
    """Порядковый номер пользователя. Если его ещё нет в базе, добавляем."""
    with db_lock, db:
        row = db.execute(
            "SELECT rowid FROM users WHERE chatid = ?", (str(chat_id),)
        ).fetchone()
        if row:
            return row[0]
        cur = db.execute(
            "INSERT INTO users(chatid, username) VALUES (?, ?)",
            (str(chat_id), username),
        )
        return cur.lastrowid


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------

def keyboard(*labels: str, row_width: int = 2) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(row_width=row_width, resize_keyboard=True)
    kb.add(*[types.KeyboardButton(label) for label in labels])
    return kb


def location_keyboard() -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    kb.add(
        types.KeyboardButton("Отправить местоположение", request_location=True),
        types.KeyboardButton(BTN_MANUAL_ADDRESS),
    )
    return kb


def main_menu_markup() -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("Хочу описать проблему", callback_data=CB_NEW_APPEAL),
        types.InlineKeyboardButton(
            "Связаться с председателем Молодёжного Совета", url=config.CHAIRMAN_URL
        ),
        types.InlineKeyboardButton(
            "Связаться с секретарём Молодёжного совета", url=config.SECRETARY_URL
        ),
        types.InlineKeyboardButton("Связаться с тех поддержкой", url=config.SUPPORT_URL),
    )
    return markup


def is_admin(chat_id: int) -> bool:
    return chat_id in config.ADMIN_IDS


def send_media(chat_id, kind, file_id, caption, markup=None):
    if kind == "video":
        return bot.send_video(chat_id, file_id, caption=caption, reply_markup=markup)
    return bot.send_photo(chat_id, file_id, caption=caption, reply_markup=markup)


def reverse_geocode(latitude: float, longitude: float) -> Optional[str]:
    """Адрес по координатам через Яндекс Геокодер. None, если адрес не нашёлся."""
    params = {
        "apikey": config.YANDEX_GEOCODER_KEY,
        "format": "json",
        "lang": "ru_RU",
        "geocode": f"{longitude},{latitude}",  # Яндекс ждёт сначала долготу
    }
    try:
        response = requests.get(GEOCODER_URL, params=params, timeout=10)
        response.raise_for_status()
        found = response.json()["response"]["GeoObjectCollection"]["featureMember"]
    except (requests.RequestException, KeyError, ValueError) as err:
        log.warning("Геокодер не ответил: %s", err)
        return None

    if not found:
        return None
    return found[0]["GeoObject"]["metaDataProperty"]["GeocoderMetaData"]["text"]


def appeal_caption(session: Session, title: str) -> str:
    return (
        f"{title}"
        f"\nВаша тема: {session.topic}"
        f"\nВаш текст: {session.text}"
        f"\nМестоположение: {session.address}"
    )


# --------------------------------------------------------------------------
# Экраны: каждая функция показывает сообщение и переключает шаг
# --------------------------------------------------------------------------

def show_main_menu(chat_id: int, text: str):
    get_session(chat_id).reset()
    bot.send_message(chat_id, text, reply_markup=main_menu_markup())


def show_topics(chat_id: int):
    session = get_session(chat_id)
    session.reset()
    session.step = Step.CHOOSING_TOPIC
    bot.send_message(
        chat_id,
        TOPICS_TEXT,
        reply_markup=keyboard(*TOPICS, BTN_CUSTOM_TOPIC, BTN_MAIN_MENU, row_width=1),
    )


def ask_location(chat_id: int, session: Session):
    session.step = Step.WAITING_LOCATION
    bot.send_message(
        chat_id,
        "Теперь отправьте местоположение или напишите его вручную",
        reply_markup=location_keyboard(),
    )


def ask_address_confirm(chat_id: int, session: Session):
    session.step = Step.CONFIRMING_ADDRESS
    bot.send_message(
        chat_id,
        "Верно?",
        reply_markup=keyboard(BTN_ADDRESS_OK, BTN_ADDRESS_AGAIN, BTN_RESTART),
    )


# --------------------------------------------------------------------------
# Команды и кнопки главного меню
# --------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def cmd_start(message):
    get_user_number(message.chat.id, message.from_user.username)
    show_main_menu(message.chat.id, GREETING.format(name=message.from_user.first_name))


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    chat_id = message.chat.id
    if not is_admin(chat_id):
        bot.send_message(chat_id, "Вы не являетесь администратором")
        return

    get_session(chat_id).reset()
    bot.send_message(
        chat_id,
        "Если хотите ответить пользователю, то нажмите на кнопку и введите айди пользователя",
        reply_markup=keyboard(BTN_REPLY_TO_USER, row_width=1),
    )


@bot.callback_query_handler(func=lambda call: call.data == CB_NEW_APPEAL)
def cb_new_appeal(call):
    bot.answer_callback_query(call.id)
    show_topics(call.message.chat.id)


# --------------------------------------------------------------------------
# Обработчики шагов
# --------------------------------------------------------------------------

def handle_idle(message, session):
    if message.text == BTN_REPLY_TO_USER and is_admin(message.chat.id):
        session.step = Step.ADMIN_USER_ID
        bot.send_message(
            message.chat.id, "Введите айди пользователя", reply_markup=types.ReplyKeyboardRemove()
        )
        return
    bot.send_message(message.chat.id, "Чтобы начать, нажмите /start")


def show_rules(chat_id: int, session: Session):
    session.step = Step.READING_RULES
    bot.send_message(chat_id, RULES_TEXT, reply_markup=keyboard(BTN_CONTINUE, BTN_BACK))


def handle_topic(message, session):
    if message.text == BTN_CUSTOM_TOPIC:
        session.step = Step.WAITING_CUSTOM_TOPIC
        bot.send_message(
            message.chat.id,
            "Напишите тему обращения (коротко, одной фразой)",
            reply_markup=types.ReplyKeyboardRemove(),
        )
        return

    if message.text not in TOPICS:
        bot.send_message(message.chat.id, "Выберите тему кнопкой ниже")
        return

    session.topic = message.text
    session.topic_code = TOPICS[message.text]
    show_rules(message.chat.id, session)


def handle_custom_topic(message, session):
    topic = (message.text or "").strip()
    if not topic or len(topic) > MAX_TOPIC_LENGTH:
        bot.send_message(
            message.chat.id, f"Напишите тему текстом, не длиннее {MAX_TOPIC_LENGTH} символов"
        )
        return

    session.topic = topic
    session.topic_code = CUSTOM_TOPIC_CODE
    show_rules(message.chat.id, session)


def handle_rules(message, session):
    if message.text != BTN_CONTINUE:
        bot.send_message(message.chat.id, "Нажмите «Продолжить», когда будете готовы")
        return

    session.step = Step.WAITING_MEDIA
    bot.send_message(
        message.chat.id, "Пришлите фото/видео", reply_markup=types.ReplyKeyboardRemove()
    )


def handle_media(message, session):
    if message.content_type == "photo":
        session.media_kind = "photo"
        session.media_id = message.photo[-1].file_id  # последнее фото — самое большое
        what = "фото"
    elif message.content_type == "video":
        session.media_kind = "video"
        session.media_id = message.video.file_id
        what = "видео"
    else:
        bot.send_message(message.chat.id, "Пришлите фото или видео")
        return

    session.step = Step.CONFIRMING_MEDIA
    caption = (
        f"Уверены ли вы, что хотите отправить именно это {what}?\n"
        f"Если нет, нажмите «{BTN_MEDIA_AGAIN}»"
    )
    send_media(
        message.chat.id,
        session.media_kind,
        session.media_id,
        caption,
        keyboard(BTN_MEDIA_OK, BTN_MEDIA_AGAIN, BTN_BACK),
    )


def handle_media_confirm(message, session):
    if message.text == BTN_MEDIA_OK:
        session.step = Step.WAITING_TEXT
        bot.send_message(
            message.chat.id, "Напишите текст обращения", reply_markup=types.ReplyKeyboardRemove()
        )
    elif message.text == BTN_MEDIA_AGAIN:
        session.step = Step.WAITING_MEDIA
        bot.send_message(
            message.chat.id, "Пришлите фото или видео ещё раз", reply_markup=types.ReplyKeyboardRemove()
        )
    else:
        bot.send_message(message.chat.id, "Выберите вариант кнопкой ниже")


def handle_text(message, session):
    text = (message.text or "").strip()
    if not text:
        bot.send_message(message.chat.id, "Напишите текст обращения сообщением")
        return
    if len(text) > MAX_TEXT_LENGTH:
        bot.send_message(
            message.chat.id,
            f"Текст слишком длинный ({len(text)} симв.). Сократите до {MAX_TEXT_LENGTH}.",
        )
        return

    session.text = text
    ask_location(message.chat.id, session)


def handle_location(message, session):
    chat_id = message.chat.id

    if message.location is not None:
        address = reverse_geocode(message.location.latitude, message.location.longitude)
        if address is None:
            bot.send_message(
                chat_id, "Не удалось получить Ваш адрес. Попробуйте ещё раз или напишите его вручную"
            )
            return
        session.address = address
        bot.send_message(chat_id, f"Адрес:\n{address}")
        ask_address_confirm(chat_id, session)

    elif message.text == BTN_MANUAL_ADDRESS:
        session.step = Step.WAITING_ADDRESS
        bot.send_message(
            chat_id, "Напишите местоположение", reply_markup=types.ReplyKeyboardRemove()
        )

    else:
        bot.send_message(chat_id, "Отправьте геопозицию кнопкой или выберите ввод вручную")


def handle_address(message, session):
    address = (message.text or "").strip()
    if not address or len(address) > MAX_ADDRESS_LENGTH:
        bot.send_message(message.chat.id, "Напишите адрес текстом, покороче")
        return

    session.address = address
    bot.send_message(message.chat.id, address)
    ask_address_confirm(message.chat.id, session)


def handle_address_confirm(message, session):
    chat_id = message.chat.id

    if message.text == BTN_ADDRESS_OK:
        session.step = Step.CONFIRMING_SEND
        send_media(
            chat_id,
            session.media_kind,
            session.media_id,
            appeal_caption(session, "Отправить обращение?"),
            keyboard(BTN_SEND, BTN_RESTART),
        )
    elif message.text == BTN_ADDRESS_AGAIN:
        ask_location(chat_id, session)
    else:
        bot.send_message(chat_id, "Выберите вариант кнопкой ниже")


def handle_send_confirm(message, session):
    if message.text != BTN_SEND:
        bot.send_message(message.chat.id, "Выберите вариант кнопкой ниже")
        return
    submit_appeal(message, session)


def submit_appeal(message, session):
    chat_id = message.chat.id
    user_number = get_user_number(chat_id, message.from_user.username)
    appeal_id = f"{user_number}{session.topic_code}"

    # подтверждение жителю
    bot.send_message(chat_id, f"Айди обращения: {appeal_id}", reply_markup=types.ReplyKeyboardRemove())
    send_media(
        chat_id, session.media_kind, session.media_id, appeal_caption(session, "Ваше обращение:")
    )
    bot.send_message(chat_id, "Готово! Обращение принято. Ожидайте ответа.")

    # уведомление администраторов
    admin_caption = (
        f"Тема: {session.topic}"
        f"\nОбращение: {session.text}"
        f"\nМестоположение: {session.address}"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            bot.send_message(
                admin_id,
                f"Айди обращения: {appeal_id}\nАйди пользователя: {message.from_user.id}",
            )
            send_media(admin_id, session.media_kind, session.media_id, admin_caption)
        except ApiTelegramException as err:
            log.warning("Не удалось отправить обращение администратору %s: %s", admin_id, err)

    log.info("Обращение %s принято (тема: %s)", appeal_id, session.topic)
    session.reset()


# --- ответ администратора жителю ---

def handle_admin_user_id(message, session):
    if not (message.text or "").strip().isdigit():
        bot.send_message(message.chat.id, "Айди пользователя — это число, попробуйте ещё раз")
        return

    session.reply_user_id = int(message.text)
    session.step = Step.ADMIN_APPEAL_ID
    bot.send_message(message.chat.id, "Введите айди обращения")


def handle_admin_appeal_id(message, session):
    if not (message.text or "").strip().isdigit():
        bot.send_message(message.chat.id, "Айди обращения — это число, попробуйте ещё раз")
        return

    session.reply_appeal_id = message.text.strip()
    session.step = Step.ADMIN_REPLY
    bot.send_message(message.chat.id, "Введите ответ на обращение")


def handle_admin_reply(message, session):
    if not message.text:
        bot.send_message(message.chat.id, "Ответ нужно написать текстом")
        return

    try:
        bot.send_message(session.reply_user_id, f"Айди обращения: {session.reply_appeal_id}")
        bot.send_message(session.reply_user_id, message.text)
    except ApiTelegramException as err:
        log.warning("Ответ не доставлен пользователю %s: %s", session.reply_user_id, err)
        bot.send_message(
            message.chat.id,
            "Не удалось отправить ответ: пользователь мог заблокировать бота или айди указан неверно",
        )
    else:
        bot.send_message(message.chat.id, "Ответ отправлен")
    session.reset()


STEP_HANDLERS = {
    Step.IDLE: handle_idle,
    Step.CHOOSING_TOPIC: handle_topic,
    Step.WAITING_CUSTOM_TOPIC: handle_custom_topic,
    Step.READING_RULES: handle_rules,
    Step.WAITING_MEDIA: handle_media,
    Step.CONFIRMING_MEDIA: handle_media_confirm,
    Step.WAITING_TEXT: handle_text,
    Step.WAITING_LOCATION: handle_location,
    Step.WAITING_ADDRESS: handle_address,
    Step.CONFIRMING_ADDRESS: handle_address_confirm,
    Step.CONFIRMING_SEND: handle_send_confirm,
    Step.ADMIN_USER_ID: handle_admin_user_id,
    Step.ADMIN_APPEAL_ID: handle_admin_appeal_id,
    Step.ADMIN_REPLY: handle_admin_reply,
}


@bot.message_handler(content_types=["text", "photo", "video", "location"])
def on_message(message):
    chat_id = message.chat.id
    session = get_session(chat_id)

    # эти кнопки работают на любом шаге
    if message.text == BTN_MAIN_MENU:
        show_main_menu(chat_id, "Вы вернулись в главное меню!")
        return
    if message.text in (BTN_RESTART, BTN_BACK):
        show_topics(chat_id)
        return

    STEP_HANDLERS.get(session.step, handle_idle)(message, session)


if __name__ == "__main__":
    init_db()
    log.info("Бот запущен")
    bot.infinity_polling(skip_pending=True)
