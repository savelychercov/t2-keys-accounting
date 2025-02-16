import json
from aiogram.enums import ContentType, ChatAction
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, Message, ErrorEvent, Poll)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.filters import Command
from aiogram.types import CallbackQuery
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from datetime import datetime, timedelta
from requests.exceptions import ConnectionError
import asyncio
import sheets
import logger
import os
import sys


def resource_path(relative_path):
    """ Get absolute path to resource, works for dev and for PyInstaller """
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


# region Connection

logger = logger.Logger()
print("Setting bot token")
with open(resource_path(os.path.join("credentials", "telegram_bot.json")), "r") as f:
    API_TOKEN = json.load(f)["telegram_apikey"]
dp = Dispatcher(storage=MemoryStorage())
bot: Bot = Bot(API_TOKEN)
print("Bot connected")


print("Connecting to worksheets")
keys_accounting_table = sheets.KeysAccountingTable()
keys_table = sheets.KeysTable()
emp_table = sheets.EmployeesTable()
print("Worksheets connected")


async def main():
    await dp.start_polling(bot)


# endregion


# region Utils


def make_serializable(data):
    if isinstance(data, list):
        return [make_serializable(item) for item in data]
    elif isinstance(data, dict):
        return {key: make_serializable(value) for key, value in data.items()}
    elif hasattr(data, 'to_dict'):
        return data.to_dict()
    else:
        # Если тип неизвестен и не имеет to_dict(), оставляем его как есть.
        # Это может вызвать ошибку при попытке сериализации.
        return str(data)


async def remove_key_after_delay(key, dictionary, delay=600):
    await asyncio.sleep(delay)
    if key in dictionary:
        try:
            await bot.send_message(chat_id=dictionary[key], text=f"Время запроса на ключ {key} истекло.")
        except Exception as e:
            print("Не удалось отправить сообщение пользователю:\n", e)
        del dictionary[key]


def escape_markdown(text: str):
    escape_chars = ['_', '*', '[', '`']
    for char in escape_chars:
        text = text.replace(char, f'\\{char}')
    return text


def phone_format(phone: str | int):
    phone = str(phone)
    digits = ''.join(filter(str.isdigit, phone))
    if digits.startswith('8'):
        digits = '7' + digits[1:]
    elif digits.startswith('7'):
        pass
    else:
        digits = '7' + digits
    digits = digits[:11]
    return f'+{digits}'


async def has_role(role: str, user_id: str):
    user_id = str(user_id)
    employees = await emp_table.get_all_employees()
    emp = next((emp for emp in employees if emp.telegram == user_id), None)
    if emp is not None and role in emp.roles:  # emp found and has enough roles
        return True
    elif emp is None:  # emp not found
        return None
    elif role not in emp.roles:  # emp found, but not enough roles
        return False


async def check_registration(user_id: str) -> bool:
    user_id = str(user_id)
    employees = await emp_table.get_all_employees()
    return any(emp.telegram == user_id for emp in employees)


# endregion


# region Backend

requested_keys = {}
request_delay = 60*60  # 10 minutes
reminder_delay = 60*60*24  # 24 hours


async def time_reminder():
    while True:
        try:
            print("Checking for time reminders...")
            not_returned_entries = await keys_accounting_table.get_not_returned_keys()

            for entry in not_returned_entries:
                print(f"Checking {entry.emp_firstname} {entry.emp_lastname} for key {entry.key_name}")
                if entry.time_received + timedelta(days=3) < datetime.now():
                    emp = await emp_table.get_by_name(entry.emp_firstname, entry.emp_lastname)
                    if emp is None:
                        print(f"Employee {entry.emp_firstname} {entry.emp_lastname} not returned key {entry.key_name} but not found in database for sending notification message")
                        continue
                    print("Sending notification message")
                    await bot.send_message(chat_id=emp.telegram, text=f"Вы взяли ключ {entry.key_name} 3+ дня назад, но не вернули его. Пожалуйста, верните его в ближайшее время.")
        except ConnectionError as e:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERR: Connection error (Remote end closed connection without response)")
        except Exception as e:
            logger.err(e, "Error in time_reminder")
        await asyncio.sleep(reminder_delay)


@dp.error()
async def error_handler(event: ErrorEvent):
    if isinstance(event.exception, ConnectionError):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERR: Connection error (Remote end closed connection without response)")
        return
    logger.err(event.exception, additional_text="Error while handling command")
    if hasattr(event, "message"):
        await event.message.answer("Произошла ошибка, попробуйте еще раз")
    # await bot.send_message(event, "Произошла неизвестная ошибка, попробуйте еще раз позже.")


@dp.startup()
async def on_startup(dispatcher: Dispatcher):
    asyncio.create_task(time_reminder())
    print(f"Bot \'{(await bot.get_me()).username}\' started")


@dp.shutdown()
async def on_shutdown(*args, **kwargs):
    print(f"Bot \'{(await bot.get_me()).username}\' stopped")


class LogCommandsMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: dict):
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: Message from {event.from_user.username} ({event.from_user.id}): {event.text}")
        return await handler(event, data)


dp.message.middleware.register(LogCommandsMiddleware())


# endregion


# region General Commands


class FeedbackState(StatesGroup):
    waiting_for_feedback = State()


@dp.message(Command("feedback"))
async def send_feedback(message: types.Message, state: FSMContext):
    await message.answer("Напишите ваш отзыв или предложение (будет отправлен только текст)\n\n/cancel - отменить")
    await state.set_state(FeedbackState.waiting_for_feedback)


@dp.message(FeedbackState.waiting_for_feedback)
async def get_feedback(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await message.answer("Отменено.")
        await state.clear()
        return
    logger.log(f"New feedback:\nFrom: {message.from_user.first_name} {message.from_user.last_name} (@{message.from_user.username})\n\n```\n{message.text}```")
    await message.answer("Отправлено.")
    await state.clear()


# endregion


# region Registration


def needs_registration(user_tag: str) -> bool:
    print(f"User {user_tag} tries to register")
    return True


class RegistrationState(StatesGroup):
    waiting_for_name = State()
    waiting_for_surname = State()
    waiting_for_phone = State()


# Команда /start
@dp.message(Command("start"))
async def send_welcome(message: types.Message, state: FSMContext):
    user_id = message.from_user.id

    keyboard = types.ReplyKeyboardRemove()

    if await check_registration(user_id):
        await message.answer("Вы уже зарегистрированы и можете пользоваться ботом!", reply_markup=keyboard)
        return

    if not user_id: return
    if not needs_registration(user_id): return

    await message.answer("Чтобы зарегистрироваться в системе, введите ваше имя:", reply_markup=keyboard)
    await state.set_state(RegistrationState.waiting_for_name)


@dp.message(RegistrationState.waiting_for_name)
async def get_name(message: types.Message, state: FSMContext):
    name = message.text.replace(" ", "")
    await state.update_data(name=name)
    await message.answer("Теперь введите вашу фамилию:")
    await state.set_state(RegistrationState.waiting_for_surname)


@dp.message(RegistrationState.waiting_for_surname)
async def get_surname(message: types.Message, state: FSMContext):
    surname = message.text.replace(" ", "")
    await state.update_data(surname=surname)
    kb = [
        [KeyboardButton(text="Отправить номер телефона", request_contact=True)],
        [KeyboardButton(text="Ввести номер телефона вручную")]
    ]
    markup = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True, keyboard=kb)
    await message.answer("Теперь отправьте ваш номер телефона:", reply_markup=markup)
    await state.set_state(RegistrationState.waiting_for_phone)


@dp.message(RegistrationState.waiting_for_phone, F.content_type == ContentType.CONTACT)
async def get_phone_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact

    if contact is None or message.from_user.id != contact.user_id:
        await message.reply("Пожалуйста, используйте кнопку для отправки вашего номера телефона.")
        return

    await state.update_data(phone=contact.phone_number)
    await finalize_data(message, state)


@dp.message(RegistrationState.waiting_for_phone, F.content_type == ContentType.TEXT)
async def get_phone_text(message: Message, state: FSMContext) -> None:
    phone = message.text

    if not phone.isdigit() or len(phone) < 10 or phone[0] != "7":
        await message.reply("Пожалуйста, введите корректный номер телефона (только цифры) Например: 79008006050.")
        return

    await state.update_data(phone=phone)
    await finalize_data(message, state)


async def finalize_data(message: types.Message, state: FSMContext):
    user_data = await state.get_data()

    await message.answer(text="Все данные собраны", reply_markup=types.ReplyKeyboardRemove())

    kb = [[InlineKeyboardButton(text="Подтвердить", callback_data="confirm")]]
    inline_markup = InlineKeyboardMarkup(inline_keyboard=kb)

    await message.reply(
        f"Вот ваши данные:\n"
        f"Имя: {user_data['name']}\n"
        f"Фамилия: {user_data['surname']}\n"
        f"Телефон: {user_data['phone']}\n\n"
        "Если данные не совпадают, начните заново - команда /start.",
        reply_markup=inline_markup,
    )


@dp.callback_query(F.data == "confirm")
async def confirm_data(callback_query: CallbackQuery, state: FSMContext):
    try:
        user_data = await state.get_data()
        await emp_table.new_employee(
            user_data["name"],
            user_data["surname"],
            phone_format(user_data["phone"]),
            callback_query.from_user.id,
            # "user",
        )
    except Exception as e:
        print(e)
        logger.err(e, "Error in confirm registration data")
        await callback_query.answer("Произошла ошибка при сохранении данных.")
        return
    await callback_query.message.edit_reply_markup(reply_markup=None)
    await callback_query.answer("Данные сохранены, свяжитесь с администратором чтобы получить роли для доступа к командам бота")
    await state.clear()


# endregion


# region User Commands


class GetKeyState(StatesGroup):
    waiting_for_key = State()
    waiting_for_comment = State()
    waiting_for_confirmation = State()


@dp.message(Command("get_key"))
async def get_key(message: types.Message, state: FSMContext):
    if not await has_role("user", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    emp = await emp_table.get_by_telegram(message.from_user.id)
    await state.update_data(emp=emp)
    await message.answer("Введите название ключа или номер базовой станции\n\n(/cancel для отмены)")
    await state.set_state(GetKeyState.waiting_for_key)


@dp.message(GetKeyState.waiting_for_key)
async def get_key_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=types.ReplyKeyboardRemove())
        return
    msg = await message.answer("Поиск ключа...", reply_markup=types.ReplyKeyboardRemove())
    key_names = {key.key_name for key in await keys_table.get_all_keys()}
    not_returned_keys = {key.key_name for key in await keys_accounting_table.get_not_returned_keys()}
    similarities = await sheets.find_similar(message.text, key_names)

    if message.text in key_names or len(similarities) == 1:
        if similarities:
            key_name = similarities[0]
        else:
            key_name = message.text
        if key_name in not_returned_keys:
            await msg.delete()
            await message.answer("Этот ключ уже взят:")
            await message.answer(await get_key_state_str(key_name), reply_markup=types.ReplyKeyboardRemove(), parse_mode="Markdown")
            await state.clear()
            return
        if key_name in requested_keys:
            await msg.delete()
            await message.answer("Этот ключ уже запрошен.")
            await state.clear()
            return
        await state.update_data(key=key_name)
        await msg.delete()
        await message.answer(
            f"Ключ: {key_name}\n"
            f"Теперь введите комментарий\n\n(/empty - без комментария)\n\n(/cancel для отмены)")
        await state.set_state(GetKeyState.waiting_for_comment)
        return
    else:
        if similarities:
            kb = []
            for sim in similarities:
                kb.append([KeyboardButton(text=sim)])
            await msg.delete()
            await message.answer("Выберите ключ из найденных:",
                                 reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True,
                                                                  one_time_keyboard=True))
        else:
            await msg.delete()
            await message.answer("Ключ не найден")
            await state.clear()


@dp.message(GetKeyState.waiting_for_comment)
async def get_key_comment(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=types.ReplyKeyboardRemove())
        return
    if message.text == "/empty":
        await state.update_data(comment="")
    else:
        await state.update_data(comment=message.text)

    msg = await message.answer("Обработка...")

    security_emp = await emp_table.get_security_employee()
    if not security_emp:
        await message.reply("Охранник не зарегистрирован.")
        await state.clear()
        return

    security_id = security_emp.telegram
    key_name = (await state.get_data())["key"]
    comment = (await state.get_data())["comment"]
    emp_from = (await state.get_data())["emp"]

    callback_data_approve = f"approve_key:{message.from_user.id}:{key_name}:{comment}"
    callback_data_deny = f"deny_key:{message.from_user.id}:{key_name}"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Подтвердить выдачу ключей", callback_data=callback_data_approve)],
            [InlineKeyboardButton(text="Отклонить", callback_data=callback_data_deny)]
        ]
    )

    await bot.send_message(
        chat_id=security_id,
        text=(
            f"{f"Запрос на выдачу ключей от пользователя @{message.from_user.username}\n" if message.from_user.username else "Запрос на выдачу ключей\n"}"
            f"Ключ: {key_name}\n"
            f"Имя: {emp_from.first_name} {emp_from.last_name}\n"
            f"{f"Комментарий: {comment}\n\n" if comment else ""}"
            "Подтвердите действие:"
        ),
        reply_markup=keyboard,
    )

    await msg.edit_text("Запрос отправлен охраннику. Ожидайте подтверждения.")
    await state.clear()
    requested_keys[key_name] = message.from_user.id
    asyncio.create_task(remove_key_after_delay(key_name, requested_keys, request_delay))


@dp.callback_query(F.data.startswith("approve_key"))
async def approve_key(callback: CallbackQuery) -> None:
    _, user_id, key_name, comment = callback.data.split(":")
    if key_name not in requested_keys:
        await callback.message.edit_text(callback.message.text+"\n\nВремя запроса истекло")
        return

    emp = await emp_table.get_by_telegram(int(user_id))

    await bot.send_message(
        chat_id=user_id,
        text="✔ Охранник подтвердил ваш запрос на выдачу ключей",
    )

    await callback.message.edit_text(callback.message.text+"\n\n✔ Выдача ключа подтверждена")
    await keys_accounting_table.new_entry(
        key_name,
        emp.first_name,
        emp.last_name,
        emp.phone_number,
        comment=comment,
    )
    if key_name in requested_keys:
        del requested_keys[key_name]


@dp.callback_query(F.data.startswith("deny_key"))
async def deny_key(callback: CallbackQuery) -> None:
    _, user_id, key_name = callback.data.split(":")
    if key_name not in requested_keys:
        await callback.message.edit_text(callback.message.text+"\n\nВремя запроса истекло")
        return

    await bot.send_message(
        chat_id=user_id,
        text="❌ Охранник отклонил ваш запрос на выдачу ключей.",
    )
    await callback.message.edit_text(callback.message.text+"\n\n❌ Вы отклонили запрос на выдачу ключей.")
    if key_name in requested_keys:
        del requested_keys[key_name]


async def state_format(entry: sheets.Entry, key_info: bool = True) -> str:
    if key_info:
        key = await keys_table.get_by_name(entry.key_name)
        if key is None:
            return await state_format(entry, key_info=False)
        if entry.time_returned is None:
            return (
                f"*Ключ*: `{entry.key_name}`\n"
                f"*  Состояние*: Не на месте\n"
                f"*  Количество ключей*: `{key.count}`\n"
                f"*  Тип ключа*: `{key.key_type}`\n"
                f"*  Тип аппаратный*: `{key.hardware_type}`\n\n"
                f"*Ключ выдан:*\n"
                f"  *Имя*: `{entry.emp_firstname} {entry.emp_lastname}`\n"
                f"  *Выдан в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"{f"  *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
                f"  *Контакт*: {phone_format(entry.emp_phone)}\n"
            )
        else:
            return (
                f"*Ключ*: `{entry.key_name}`\n"
                f"*  Состояние*: Этот ключ сейчас на месте\n"
                f"*  Количество ключей*: `{key.count}`\n"
                f"*  Тип ключа*: `{key.key_type}`\n"
                f"*  Тип аппаратный*: `{key.hardware_type}`\n\n"
                f"*Последний пользователь:*\n"
                f"  *Имя*: `{entry.emp_firstname} {entry.emp_lastname}`\n"
                f"  *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"  *Вернул в*: `{entry.time_returned.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"{f"  *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
                f"  *Контакт*: {phone_format(entry.emp_phone)}\n"
            )
    else:
        if entry.time_returned is None:
            return (
                f"*Ключ*: `{entry.key_name}`\n"
                f"*  Состояние*: Не на месте\n"
                f"*Ключ выдан:*\n"
                f"  *Имя*: `{entry.emp_firstname} {entry.emp_lastname}`\n"
                f"  *Выдан в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"{f"  *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
                f"  *Контакт*: {phone_format(entry.emp_phone)}\n"
            )
        else:
            return (
                f"*Ключ*: `{entry.key_name}`\n"
                f"*  Состояние*: Этот ключ сейчас на месте\n"
                f"*Последний пользователь:*\n"
                f"  *Имя*: `{entry.emp_firstname} {entry.emp_lastname}`\n"
                f"  *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"  *Вернул в*: `{entry.time_returned.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"{f"  *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
                f"  *Контакт*: {phone_format(entry.emp_phone)}\n"
            )


async def get_key_state_str(key_name: str) -> str:
    entries = await keys_accounting_table.get_all_entries()
    key_entries = [entry for entry in entries if entry.key_name == key_name]
    if not key_entries:
        key = await keys_table.get_by_name(key_name)
        if key is None:
            return "По этому ключу нет записей в истории и в таблице ключей"
        else:
            return (
                f"*Ключ*: `{key_name}`\n"
                f"*  Состояние*: На месте\n"
                f"*  Количество ключей*: `{key.count}`\n"
                f"*  Тип ключа*: `{key.key_type}`\n"
                f"*  Тип аппаратный*: `{key.hardware_type}`\n\n"
                f"Нет информации по последнему пользователю\n"
            )
    last_entry = key_entries[-1]
    return await state_format(last_entry)


class FindKeyState(StatesGroup):
    waiting_for_key = State()


@dp.message(Command("find_key"))
async def find_key(message: types.Message, state: FSMContext):
    if not await has_role("user", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    await message.answer("Введите название ключа или номер базовой станции\n\n(/cancel для отмены)")
    await state.set_state(FindKeyState.waiting_for_key)


@dp.message(FindKeyState.waiting_for_key)
async def waiting_for_key_name(message: types.Message, state: FSMContext):
    msg = await message.answer("Поиск ключа...")
    await state.update_data(key=message.text)
    entries = await keys_accounting_table.get_all_entries()
    keys_obj = await keys_table.get_all_keys()
    key_names = {entry.key_name for entry in entries} | {key.key_name for key in keys_obj}
    similarities = await sheets.find_similar(message.text, key_names)

    if not similarities:
        await msg.edit_text("Ключ не найден")
        await state.clear()
        return

    if len(similarities) > 1:
        kb = []
        for sim in similarities:
            kb.append([KeyboardButton(text=sim)])
        await msg.delete()
        await message.answer("Выберите ключ из найденных:",
                             reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True,
                                                              one_time_keyboard=True))
        # await state.clear()
        return

    await msg.delete(),
    await message.answer(
        await get_key_state_str(similarities[0]),
        parse_mode="Markdown",
        reply_markup=types.ReplyKeyboardRemove()),
    await state.clear()


class GetKeyHistoryState(StatesGroup):
    waiting_for_key = State()


@dp.message(Command("key_history"))
async def get_key_history(message: types.Message, state: FSMContext):
    if not await has_role("user", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    await message.answer("Введите название ключа или номер базовой станции\n\n(/cancel для отмены)")
    await state.set_state(GetKeyHistoryState.waiting_for_key)


@dp.message(GetKeyHistoryState.waiting_for_key)
async def waiting_for_key_name(message: types.Message, state: FSMContext):
    msg = await message.answer("Получение истории...")
    await state.update_data(key=message.text)
    entries = await keys_accounting_table.get_all_entries()
    keys_obj = await keys_table.get_all_keys()
    key_names = {entry.key_name for entry in entries} | {key.key_name for key in keys_obj}
    similarities = await sheets.find_similar(message.text, key_names)

    if not similarities:
        await msg.edit_text("Ключ не найден")
        await state.clear()
        return

    if len(similarities) > 1:
        kb = []
        for sim in similarities:
            kb.append([KeyboardButton(text=sim)])
        await msg.delete()
        await message.answer(
            "Выберите ключ из найденных:",
            reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True))
        return

    await msg.delete()
    history_msg_strs = await get_key_history_str(similarities[0])
    for msg in history_msg_strs:
        await message.answer(msg, parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())
    await state.clear()


async def get_key_history_str(key_name: str):
    key, entries = await asyncio.gather(
        keys_table.get_by_name(key_name),
        keys_accounting_table.get_all_entries()
    )
    key_entries = [entry for entry in entries if entry.key_name == key_name]
    response_strs = [""]
    if key:
        response_strs[-1] = (
            f"*Ключ*: `{key_name}`\n"
            f"*Количество ключей*: `{key.count}`\n"
            f"*Тип ключа*: `{key.key_type}`\n"
            f"*Тип аппаратный*: `{key.hardware_type}`\n"
            f"*Этот ключ брали*: {len(key_entries)} раз(а)\n\n"
        )
    else:
        response_strs[-1] = (
            f"*Ключ*: `{key_name}`\n"
            f"*Этот ключ брали*: {len(key_entries)} раз(а)\n\n"
        )
    if not key_entries:
        response_strs[-1] += "По этому ключу нет записей"
        return response_strs
    for entry in key_entries:
        if len(response_strs[-1]) > 2000:
            response_strs.append("")
        response_strs[-1] += (
            f"*Имя*: `{entry.emp_firstname} {entry.emp_lastname}`\n"
            f"| *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
            f"{f"| *Вернул в*: `{entry.time_returned.strftime('%H:%M (%d.%m.%Y)')}`\n" if entry.time_returned else ""}"
            f"| *Контакт*: {phone_format(entry.emp_phone)}\n"
            f"{f"| *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
        )
        response_strs[-1] += "\n"

    return response_strs


class GetEmpHistoryState(StatesGroup):
    waiting_for_name = State()


@dp.message(Command("emp_history"))
async def get_emp_history(message: types.Message, state: FSMContext):
    if not await has_role("user", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    await message.answer("Введите ФИ сотрудника для поиска")
    await state.set_state(GetEmpHistoryState.waiting_for_name)


@dp.message(GetEmpHistoryState.waiting_for_name)
async def waiting_for_emp_name(message: types.Message, state: FSMContext):
    msg = await message.answer("Получение истории...")
    await state.update_data(key=message.text)
    entries = await keys_accounting_table.get_all_entries()
    emp_obj = await emp_table.get_all_employees()
    emp_names = (
        {f"{entry.emp_firstname} {entry.emp_lastname}" for entry in entries} |
        {f"{emp.first_name} {emp.last_name}" for emp in emp_obj}
    )
    similarities = list(
        set(await sheets.find_similar(message.text, emp_names)) |
        set(await sheets.find_similar(sheets.flip(message.text), emp_names))
    )

    if not similarities:
        print("No similarities found")
        await msg.edit_text("Сотрудник не найден")
        await state.clear()
        return

    if len(similarities) > 1:
        kb = []
        for sim in similarities:
            kb.append([KeyboardButton(text=sim)])
        await msg.delete()
        await message.answer(
            "Выберите из найденных:",
            reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True))
        return

    await msg.delete()
    history_msg_strs = await get_emp_history_str(similarities[0])
    for msg in history_msg_strs:
        await message.answer(msg, parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())
    await state.clear()


async def get_emp_history_str(emp_name: str):
    first_name, last_name = emp_name.split(" ", 1)
    emp, entries = await asyncio.gather(
        emp_table.get_by_name(first_name, last_name),
        keys_accounting_table.get_all_entries()
    )
    emp_entries = []
    for entry in entries:
        if entry.emp_firstname == first_name and entry.emp_lastname == last_name:
            emp_entries.append(entry)
    response_strs = [""]
    if emp:
        tg = await bot.get_chat(emp.telegram)
        response_strs[-1] = (
            f"*Имя*: `{emp.first_name} {emp.last_name}`\n"
            f"*Телефон*: {phone_format(emp.phone_number)}\n"
            f"{f"*Телеграм*: @{tg.username}\n" if tg.username else ""}"
            f"*Роли*: {', '.join(emp.roles) if emp.roles else 'Нет'}\n"
            f"*Этот сотрудник брал ключи*: {len(emp_entries)} раз(а)\n\n"
        )
    else:
        response_strs[-1] = (
            f"*Имя*: `{first_name} {last_name}`\n"
            f"*Этот сотрудник брал ключи*: {len(emp_entries)} раз(а)\n\n"
        )
    if not emp_entries:
        response_strs[-1] += "По этому сотруднику нет записей"
        return response_strs
    for entry in emp_entries:
        if len(response_strs[-1]) > 2000:
            response_strs.append("")
        response_strs[-1] += (
            f"*Ключ*: `{entry.key_name}`\n"
            f"| *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
            f"{f"| *Вернул в*: `{entry.time_returned.strftime('%H:%M (%d.%m.%Y)')}`\n" if entry.time_returned else ""}"
            f"{f"| *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
        )
        response_strs[-1] += "\n"

    return response_strs


@dp.message(Command("my_keys"))
async def my_history(message: types.Message):
    if not await has_role("user", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return
    user = await emp_table.get_by_telegram(message.from_user.id)

    history_msg_strs = []

    not_returned_entries = await keys_accounting_table.get_not_returned_keys()

    for entry in not_returned_entries:
        if entry.emp_firstname != user.first_name or entry.emp_lastname != user.last_name:
            continue
        key_data = await keys_table.get_by_name(entry.key_name)
        if not key_data:
            history_msg_strs.append(
                f"*Ключ*: `{entry.key_name}`\n"
                f"| *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"{f"| *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
            )
        else:
            history_msg_strs.append(
                f"*Ключ*: `{entry.key_name}`\n"
                f"| *Взял в*: `{entry.time_received.strftime('%H:%M (%d.%m.%Y)')}`\n"
                f"| *Количество ключей*: `{key_data.count}`\n"
                f"| *Тип ключа*: `{key_data.key_type}`\n"
                f"| *Тип аппаратный*: `{key_data.hardware_type}`\n"
                f"{f"|  *Комментарии*: \"{escape_markdown(entry.comment)}\"\n" if entry.comment else ""}"
            )

    if not history_msg_strs:
        await message.answer("У вас нет взятых ключей")
        return

    history_msg_strs.insert(0, f"Ваши активные ключи ({len(history_msg_strs)})")
    for msg in history_msg_strs:
        await message.answer(msg, parse_mode="Markdown", reply_markup=types.ReplyKeyboardRemove())


# endregion


# region Security Commands


@dp.message(Command("not_returned"))
async def not_returned(message: types.Message):
    user = await emp_table.get_by_telegram(message.from_user.id)
    if "user" not in user.roles and "security" not in user.roles:
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    msg = await message.answer("Поиск ключей...")

    keys = await keys_accounting_table.get_not_returned_keys()

    if not keys:
        await msg.edit_text("Сейчас все ключи на месте.")
        return

    await msg.delete()

    for key in keys:
        if "security" in user.roles:
            user_id = (await emp_table.get_by_name(key.emp_firstname, key.emp_lastname)).telegram
            kb = [[InlineKeyboardButton(text="Вернуть", callback_data=f"return_key:{key.key_name}:{user_id}")]]
            await message.answer(
                await state_format(key, False),
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
                parse_mode="Markdown")
        elif "user" in user.roles:
            await message.answer(await state_format(key, False), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("return_key"))
async def return_key(callback: CallbackQuery):
    key_name, telegram_id = callback.data.split(":")[1:3]
    await keys_accounting_table.set_return_time_by_key_name(key_name)
    await bot.send_message(
        chat_id=telegram_id,
        text=f"Охранник подтвердил возврат ключа: {key_name}",
    )
    await callback.message.edit_text(f"{callback.message.text}\n\nВремя возврата записано.")


class ReturnKeyState(StatesGroup):
    waiting_for_key = State()


@dp.message(Command("return_key"))
async def return_key(message: types.Message, state: FSMContext):
    if not await has_role("security", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return

    emp = await emp_table.get_by_telegram(message.from_user.id)
    await state.update_data(emp=emp)
    await message.answer("Введите название ключа или номер базовой станции\n\n(/cancel для отмены)")
    await state.set_state(ReturnKeyState.waiting_for_key)


@dp.message(ReturnKeyState.waiting_for_key)
async def get_key_name(message: types.Message, state: FSMContext):
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.", reply_markup=types.ReplyKeyboardRemove())
        return
    msg = await message.answer("Поиск ключа...", reply_markup=types.ReplyKeyboardRemove())
    key_names = {key.key_name for key in await keys_table.get_all_keys()}
    not_returned_keys = await keys_accounting_table.get_not_returned_keys()
    not_returned_key_names = {key.key_name for key in not_returned_keys}
    similarities = await sheets.find_similar(message.text, key_names)

    if message.text in key_names or len(similarities) == 1:
        if similarities:
            key_name = similarities[0]
        else:
            key_name = message.text
        if key_name not in not_returned_key_names:
            await msg.delete()
            await message.answer("Этот ключ сейчас на месте:")
            await message.answer(await get_key_state_str(key_name), reply_markup=types.ReplyKeyboardRemove(), parse_mode="Markdown")
            await state.clear()
            return
        await state.update_data(key=key_name)
        await msg.delete()
        key_entry = next(key for key in not_returned_keys if key.key_name == key_name)
        user_id = (await emp_table.get_by_name(key_entry.emp_firstname, key_entry.emp_lastname)).telegram
        kb = [[InlineKeyboardButton(text="Вернуть", callback_data=f"return_key:{key_entry.key_name}:{user_id}")]]
        await message.answer(
            await state_format(next(key for key in not_returned_keys if key.key_name == key_name), True),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
            parse_mode="Markdown")
        await state.clear()
        return
    else:
        if similarities:
            kb = []
            for sim in similarities:
                kb.append([KeyboardButton(text=sim)])
            await msg.delete()
            await message.answer("Выберите ключ из найденных:",
                                 reply_markup=ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True,
                                                                  one_time_keyboard=True))
        else:
            await msg.delete()
            await message.answer("Ключ не найден")
            await state.clear()


# endregion


# region Administrator Commands


@dp.message(Command("drop_cache"))
async def drop_cache(message: types.Message):
    if not await has_role("admin", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return
    await sheets.drop_cache()
    await message.answer("Кэш очищен")


@dp.message(Command("send_cache"))
async def send_cache(message: types.Message):
    if not await has_role("admin", message.from_user.id):
        await message.answer("Вы не имеете доступа к этой команде.")
        return
    data = make_serializable(sheets.cache)
    await message.answer(f"Кэш:\n\n{json.dumps(data, indent=4, ensure_ascii=False)}")


# endregion


@dp.message()
async def echo(message: types.Message):
    await message.answer("Неизвестная команда")
