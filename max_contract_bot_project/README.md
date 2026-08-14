# MAX Contract Bot

Готовый учебно-рабочий проект MAX-бота для формирования договоров из реквизитов.

## 1. Установка Python

Рекомендуется Python 3.11 или 3.12.

```powershell
py install 3.11
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Если PowerShell блокирует активацию:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## 2. Настройка

1. Скопируйте `.env.example` в `.env`.
2. Заполните токен MAX, ID сотрудников и ключи Yandex Cloud.
3. При необходимости замените `template2.docx` собственным шаблоном.

```powershell
Copy-Item .env.example .env
```

## 3. Запуск

```powershell
python max_contract_bot.py
```

## Важно

- Код использует Long Polling, что удобно для локального запуска и тестирования.
- Для постоянной production-работы MAX рекомендует Webhook.
- Поддерживаются DOCX, PDF с текстовым слоем, PDF-сканы и изображения.
- Старый формат DOC намеренно не поддерживается. Сохраните его как DOCX.
- В шаблоне Word используются переменные вида `{{ contract_number }}`.
