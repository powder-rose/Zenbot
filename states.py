from aiogram.fsm.state import State, StatesGroup

class AdminStates(StatesGroup):
    waiting_topics = State()
    waiting_edit_topic = State()
    waiting_schedule_time = State()
    waiting_urgent_topic = State()
    waiting_priority_topics = State()
    waiting_prompt_parts = State()
