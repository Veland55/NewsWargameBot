#!/usr/bin/env bash
# Установка бота как systemd-сервиса. Проверено на Ubuntu 24.04.
# Запускать из каталога проекта: ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="rss-deepseek-bot"
RUN_USER="${SUDO_USER:-$USER}"

echo "==> Каталог:      $DIR"
echo "==> Пользователь: $RUN_USER"

if ! command -v python3 >/dev/null; then
    echo "python3 не найден. Установите: sudo apt install -y python3 python3-venv" >&2
    exit 1
fi

if ! python3 -c "import venv" 2>/dev/null; then
    echo "==> Ставлю python3-venv"
    sudo apt-get update -qq && sudo apt-get install -y python3-venv
fi

echo "==> Виртуальное окружение и зависимости"
# Если каталог приехал с другой машины, .venv внутри может быть нерабочим —
# такой пересоздаём с нуля.
if [[ -d "$DIR/.venv" ]] && ! "$DIR/.venv/bin/python" -c "" 2>/dev/null; then
    echo "    существующий .venv нерабочий — пересоздаю"
    rm -rf "$DIR/.venv"
fi
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

mkdir -p "$DIR/data"

if [[ ! -f "$DIR/.env" ]]; then
    cp "$DIR/.env.example" "$DIR/.env"
    chmod 600 "$DIR/.env"
    echo
    echo "==> Создан .env — заполните BOT_TOKEN, CHANNEL_ID, ADMIN_IDS, LLM_API_KEY:"
    echo "    nano $DIR/.env"
    echo "    затем запустите ./install.sh снова"
    exit 0
fi

if grep -q "^BOT_TOKEN=123456:AA" "$DIR/.env"; then
    echo "!! В .env остался шаблонный BOT_TOKEN — заполните файл и повторите." >&2
    exit 1
fi
chmod 600 "$DIR/.env"

echo "==> Устанавливаю сервис $SERVICE"
sed -e "s|__DIR__|$DIR|g" -e "s|__USER__|$RUN_USER|g" \
    "$DIR/deploy/$SERVICE.service" | sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sleep 2

echo
sudo systemctl status "$SERVICE" --no-pager --lines=15 || true
echo
echo "Готово. Логи:      journalctl -u $SERVICE -f"
echo "        Перезапуск: sudo systemctl restart $SERVICE"
echo "Напишите боту /help в Telegram."
