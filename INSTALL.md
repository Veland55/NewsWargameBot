# Установка

Инструкция для **Ubuntu 24.04 LTS**. На 22.04 и Debian 12 всё работает так же.
Требуется Python 3.10+ (в Ubuntu 24.04 штатный 3.12 — подходит).

Ресурсы: боту хватает 128 МБ памяти и любого VPS за минимальные деньги.
В простое он спит и процессор не потребляет.

---

## Шаг 1. Что подготовить заранее

Понадобятся три вещи. Соберите их до установки — на шаге 4 они пригодятся сразу.

**1. Токен бота.** В Telegram напишите [@BotFather](https://t.me/BotFather):

```
/newbot
```

Он спросит имя (любое, например `Мои новости`) и username — должен заканчиваться
на `bot`, например `my_news_feed_bot`. В ответ придёт токен вида
`123456789:AAEhBOweik6ad9r_QXwHOOsvS4Tr...` — это и есть `BOT_TOKEN`.

**2. Канал.** Создайте канал, если его нет. Затем добавьте бота в администраторы:
профиль канала → «Администраторы» → «Добавить администратора» → найдите своего бота →
обязательно оставьте право **«Публикация сообщений»**.

Идентификатор канала: для публичного достаточно `@имя_канала`. Для приватного нужен
числовой id — переслайте любое сообщение из канала боту
[@userinfobot](https://t.me/userinfobot), он покажет id вида `-1001234567890`.

**3. Ваш Telegram id.** Напишите [@userinfobot](https://t.me/userinfobot) — он ответит
вашим числовым id. Это `ADMIN_IDS`: только с этого аккаунта бот будет принимать команды.

**4. Ключ OpenRouter.** Зарегистрируйтесь на [openrouter.ai](https://openrouter.ai),
затем [openrouter.ai/keys](https://openrouter.ai/keys) → «Create key». Ключ вида
`sk-or-v1-...`. Про деньги и выбор модели — в [SETUP.md](SETUP.md#модель-и-деньги);
коротко: DeepSeek стоит около 22 ₽ за 1000 новостей, есть и бесплатные модели.

---

## Шаг 2. Системные пакеты

```bash
sudo apt update
sudo apt install -y python3 python3-venv git
```

`python3-venv` в Ubuntu отдельным пакетом и на минимальных образах не установлен —
без него не создастся виртуальное окружение.

---

## Шаг 3. Код

```bash
sudo mkdir -p /opt/rss-deepseek-bot
sudo chown "$USER:$USER" /opt/rss-deepseek-bot
git clone <адрес-репозитория> /opt/rss-deepseek-bot
cd /opt/rss-deepseek-bot
```

Если репозитория нет и вы просто скопировали папку — распакуйте её в
`/opt/rss-deepseek-bot` и перейдите туда. Каталог может быть любым, установщик
подставит его сам.

---

## Шаг 4. Установка

```bash
./install.sh
```

Первый запуск создаст виртуальное окружение, поставит зависимости, сделает `.env`
из шаблона и остановится с просьбой его заполнить. Заполняйте:

```bash
nano .env
```

Минимум четыре строки — значения из шага 1:

```ini
BOT_TOKEN=123456789:AAEhBOweik6ad9r_QXwHOOsvS4Tr...
CHANNEL_ID=@my_news_channel
ADMIN_IDS=123456789
LLM_API_KEY=sk-or-v1-...
```

Если новости нужны ещё и на стене сообщества VK, добавьте две строки — ключ
сообщества с правами «Стена» и «Фотографии» и числовой id сообщества
(подробно — в [SETUP.md](SETUP.md#публикация-в-vk)):

```ini
VK_TOKEN=vk1.a....
VK_GROUP_ID=123456789
```

Сохраните (`Ctrl+O`, `Enter`, `Ctrl+X`) и запустите установщик снова:

```bash
./install.sh
```

Теперь он поставит systemd-сервис, включит автозапуск и покажет его состояние.
В конце появится:

```
Готово. Логи:      journalctl -u rss-deepseek-bot -f
        Перезапуск: sudo systemctl restart rss-deepseek-bot
Напишите боту /help в Telegram.
```

---

## Шаг 5. Проверка

```bash
systemctl status rss-deepseek-bot
```

Должно быть `Active: active (running)`. В логах при успешном старте:

```bash
journalctl -u rss-deepseek-bot -n 20 --no-pager
```

```
бот @my_news_feed_bot запущен; канал: @my_news_channel; модель: deepseek/deepseek-v4-flash
цикл опроса запущен
```

Теперь напишите боту в личку `/help` — он должен ответить списком команд.
Если ответа нет, проверьте `ADMIN_IDS`: посторонним бот молчит намеренно.

Дальше переходите к [SETUP.md](SETUP.md) — там про добавление лент и настройку
шаблона. Первую ленту стоит проверить в режиме отладки, не публикуя в канал.

---

## Запуск без systemd

Для пробы или отладки можно запустить руками:

```bash
cd /opt/rss-deepseek-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
.venv/bin/python -m bot.main
```

Логи пойдут в терминал, остановка — `Ctrl+C`. Так удобно смотреть подробности,
особенно с `LOG_LEVEL=DEBUG` в `.env`.

---

## Обновление

```bash
cd /opt/rss-deepseek-bot
sudo systemctl stop rss-deepseek-bot
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl start rss-deepseek-bot
```

База переживает обновление: недостающие колонки добавляются автоматически при
старте, ленты и настройки сохраняются. `.env` и `data/` git не трогает.

---

## Резервная копия

Всё состояние — два файла:

```bash
tar czf ~/rss-bot-backup-$(date +%F).tar.gz -C /opt/rss-deepseek-bot .env data/
```

Восстановление — распаковать их в каталог проекта и перезапустить сервис.

---

## Удаление

```bash
sudo systemctl disable --now rss-deepseek-bot
sudo rm /etc/systemd/system/rss-deepseek-bot.service
sudo systemctl daemon-reload
sudo rm -rf /opt/rss-deepseek-bot
```

---

## Если что-то не так

Бот сообщает о проблемах конфигурации внятным текстом, без трейсбеков — смотрите
первую строку в логах.

| Что видно | В чём дело |
|---|---|
| `BOT_TOKEN не задан` | пустая строка в `.env`; заполните значением от @BotFather |
| `Telegram отклонил BOT_TOKEN` | токен неверный или пересоздан — возьмите актуальный у @BotFather |
| `ADMIN_IDS не задан` / `содержит не числа` | нужны числовые id через запятую, узнать у @userinfobot |
| `CHANNEL_ID=... не похож на канал` | ожидается `@имя` или `-100...`, без `https://t.me/` |
| `канал не задан` в логах | пустой `CHANNEL_ID`; можно задать на ходу командой `/setchannel` |
| `не удалось отправить в @канал: Forbidden` | бот не админ канала или у него нет права публикации |
| `LLM_API_KEY не задан` | ключ OpenRouter не вписан; новости будут уходить без обработки |
| `HTTP 401` от модели | ключ неверный или отозван, проверьте на openrouter.ai/keys |
| `HTTP 429` от модели | суточный лимит бесплатной модели исчерпан, см. [SETUP.md](SETUP.md#лимиты-ии-и-предупреждения) |
| `лента недоступна` | адрес RSS неверный или сайт закрыт; проверьте `curl -I <адрес>` |
| Сервис в состоянии `activating (auto-restart)` | бот падает на старте — смотрите `journalctl -u rss-deepseek-bot -n 50` |
| `python3-venv` не найден | `sudo apt install -y python3-venv` |

Полезные команды:

```bash
journalctl -u rss-deepseek-bot -f              # живые логи
journalctl -u rss-deepseek-bot -n 100 --no-pager  # последние 100 строк
journalctl -u rss-deepseek-bot -p err           # только ошибки
sudo systemctl restart rss-deepseek-bot         # перезапуск
```

Подробные логи включаются строкой `LOG_LEVEL=DEBUG` в `.env` и перезапуском сервиса.
