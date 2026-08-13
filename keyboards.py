from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Темы", callback_data="admin:topics"),
         InlineKeyboardButton(text="⏰ Расписание", callback_data="admin:schedule")],
        [InlineKeyboardButton(text="⚡ Срочная статья", callback_data="admin:urgent"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🔄 Перезапустить бота", callback_data="admin:restart")],
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
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Случайная тема из очереди", callback_data="urgent:random")],
        [InlineKeyboardButton(text="✍️ Ввести тему вручную", callback_data="urgent:manual")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin:home")],
    ])
