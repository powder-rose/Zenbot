from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Темы", callback_data="admin:topics"),
         InlineKeyboardButton(text="⏰ Расписание", callback_data="admin:schedule")],
        [InlineKeyboardButton(text="🔥 Срочные статьи", callback_data="admin:urgent"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🧠 Промпты", callback_data="admin:prompts")],
        [InlineKeyboardButton(text="🔥 Комментарии Дзена", callback_data="admin:dzencomments")],
        [InlineKeyboardButton(text="💬 Автоответы Дзена", callback_data="admin:dzenresponder")],
        [InlineKeyboardButton(text="🔄 Перезапустить бота", callback_data="admin:restart")],
        [InlineKeyboardButton(text="🎟 Промокоды", callback_data="super:promos")],
    ])

def topics_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить темы", callback_data="topics:add")],
        [InlineKeyboardButton(text="🟢 Неиспользованные", callback_data="topics:list:unused"),
         InlineKeyboardButton(text="✅ Использованные", callback_data="topics:list:used")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
    ])

def topic_actions(topic_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✏️ Изменить", callback_data=f"topic:edit:{topic_id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"topic:delete:{topic_id}")
    ]])

def schedule_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить время", callback_data="schedule:add")],
        [InlineKeyboardButton(text="▶️/⏸ Вкл./выкл. автопубликацию", callback_data="schedule:toggle")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
    ])

def schedule_delete(schedule_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Удалить это время", callback_data=f"schedule:delete:{schedule_id}")
    ]])


def urgent_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔥 Добавить приоритетные темы",
                    callback_data="urgent:priority:add",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 Приоритетная очередь",
                    callback_data="urgent:priority:list",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚀 Случайную опубликовать сейчас",
                    callback_data="urgent:random",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✍️ Свою тему опубликовать сейчас",
                    callback_data="urgent:manual",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⬅️ Назад",
                    callback_data="admin:home",
                )
            ],
        ]
    )

def prompts_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✍️ Основной текст статьи",
            callback_data="prompt:view:article",
        )],
        [InlineKeyboardButton(
            text="📱 Короткая версия Telegram",
            callback_data="prompt:view:short",
        )],
        [InlineKeyboardButton(
            text="🖼 Промпт изображения",
            callback_data="prompt:view:image",
        )],
        [InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="admin:home",
        )],
    ])


def prompt_detail_menu(kind: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✏️ Редактировать",
            callback_data=f"prompt:edit:{kind}",
        )],
        [InlineKeyboardButton(
            text="♻️ Сбросить к стандартному",
            callback_data=f"prompt:reset:{kind}",
        )],
        [InlineKeyboardButton(
            text="⬅️ К промптам",
            callback_data="admin:prompts",
        )],
    ])


def prompt_edit_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✅ Сохранить",
            callback_data="prompt:save",
        )],
        [InlineKeyboardButton(
            text="❌ Отмена",
            callback_data="prompt:cancel",
        )],
    ])
