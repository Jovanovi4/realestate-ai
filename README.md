# RealEstate AI

Django-сервис для риелторов: ведение объектов недвижимости, лендинги, заявки, ИИ-тексты и ручная выгрузка XML для Avito.

## Возможности

- Регистрация и вход по телефону.
- Карточки квартир, домов, дач, участков и коммерческих объектов.
- Фотогалерея: главное фото, удаление и изменение порядка.
- Три адаптивных шаблона публичных лендингов с SEO-настройками.
- Заявки с лендингов и личный кабинет риелтора.
- ИИ-помощник через Ollama с историей генераций; подготовлено переключение на OpenAI API.
- Ручная выгрузка отмеченных объектов в XML для Avito.

## Локальный запуск

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

Откройте `http://127.0.0.1:8000/`.

## Настройки окружения

Переменные приведены в [.env.example](.env.example). Их нужно задать в терминале или в настройках хостинга; реальный файл с ключами не добавляйте в Git.

По умолчанию ИИ использует локальную Ollama. Для OpenAI задайте:

```bash
export AI_PROVIDER=openai
export OPENAI_API_KEY=your_key
export OPENAI_MODEL=gpt-4.1-mini
```

## Уведомления о новых заявках

После публикации лендинга новая заявка всегда сохраняется в CRM. Дополнительно
можно включить уведомления на email и в Telegram — ошибка доставки не помешает
клиенту отправить форму.

1. В настройках окружения включите `EMAIL_NOTIFICATIONS_ENABLED=True` и
   укажите SMTP-параметры вместе с `DEFAULT_FROM_EMAIL`. Письмо уйдёт на email,
   указанный в профиле риелтора.
2. Создайте Telegram-бота через BotFather, сохраните его токен только в
   `TELEGRAM_BOT_TOKEN` на хостинге и включите
   `TELEGRAM_NOTIFICATIONS_ENABLED=True`.
3. Риелтор должен открыть диалог с ботом и отправить ему `/start`, затем указать
   свой приватный `chat_id` в личном кабинете. Это значение не публикуется на
   лендингах.
4. После изменения модели выполните `python manage.py migrate` при следующем
   развёртывании.

## Защита публичных форм

Каждая форма заявки уже защищена скрытым honeypot-полем и лимитом отправок с
одного IP: по умолчанию до 8 заявок суммарно и до 3 на один объект за 15 минут.
В production с несколькими процессами настройте общий cache backend, иначе
локальный cache ограничивает попытки только в рамках одного процесса.
`PUBLIC_FORMS_TRUST_X_FORWARDED_FOR=True` включайте только за доверенным
reverse proxy (например, Render), который сам формирует этот заголовок.

Для дополнительной проверки включите Cloudflare Turnstile: создайте widget для
доменов лендингов, сохраните публичный ключ в `TURNSTILE_SITE_KEY`, секретный —
только в `TURNSTILE_SECRET_KEY`, укажите `TURNSTILE_ALLOWED_HOSTNAMES` и задайте
`TURNSTILE_ENABLED=True`. Сервер проверяет каждый токен через Siteverify; без
успешной серверной проверки заявка не создаётся.

## Юридические документы и персональные данные

Сервис содержит публичные страницы политики обработки персональных данных,
согласия и условий использования. При отправке заявки и регистрации пользователь
должен отметить отдельное согласие на обработку персональных данных; дата и
версия согласия сохраняются в базе. Для регистрации отдельно сохраняется
принятие условий сервиса.

Перед публикацией обязательно задайте реальные значения `LEGAL_OPERATOR_NAME`,
`LEGAL_OPERATOR_INN`, `LEGAL_OPERATOR_OGRN`, `LEGAL_OPERATOR_ADDRESS`,
`LEGAL_PRIVACY_EMAIL` и `LEGAL_DATA_STORAGE_LOCATION`. Шаблоны не заменяют
проверку юристом: особенно если данные передаются за пределы России, используются
внешние сервисы или меняется модель оказания услуг.

## Тестовый стенд на PythonAnywhere Free

Для демонстрации одному пользователю проект можно разместить на бесплатном
PythonAnywhere без отдельной платной базы: не задавайте `DATABASE_URL`, и
Django использует SQLite на стороне PythonAnywhere. ИИ на стенде отключён.

1. Создайте бесплатный аккаунт PythonAnywhere и откройте **Consoles → Bash**.
2. Создайте виртуальное окружение с Python 3.12 или новее (версия должна
   совпадать с версией будущего web app):

   ```bash
   mkvirtualenv --python=python3.13 realestate-ai
   git clone --branch staging https://github.com/Jovanovi4/realestate-ai.git
   cd realestate-ai
   pip install -r requirements.txt
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py createsuperuser
   ```

3. В **Web → Add a new web app** выберите **Manual configuration** и ту же
   версию Python. В поле **Virtualenv** укажите
   `/home/ВАШ_ЛОГИН/.virtualenvs/realestate-ai`.
4. Откройте WSGI-файл из вкладки Web и замените его содержимое на шаблон
   [deployment/pythonanywhere_wsgi.py.example](deployment/pythonanywhere_wsgi.py.example),
   подставив свой логин и новый секретный ключ. Секрет можно сгенерировать
   командой `python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"`.
5. В разделе **Static files** добавьте два соответствия:
   - `/static/` → `/home/ВАШ_ЛОГИН/realestate-ai/staticfiles`;
   - `/media/` → `/home/ВАШ_ЛОГИН/realestate-ai/media`.
6. Нажмите **Reload**. Сайт откроется по адресу
   `https://ВАШ_ЛОГИН.pythonanywhere.com`.

При обновлении стенда выполните в Bash-консоли:

```bash
workon realestate-ai
cd ~/realestate-ai
git pull origin staging
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```

После этого нажмите **Reload** на вкладке Web. Не передавайте тестировщику
доступ администратора: создайте для него отдельный демо-аккаунт с тестовыми
данными.

## Тестовый стенд на Render

Проект подготовлен для отдельного стенда: PostgreSQL подключается через
`DATABASE_URL`, статика собирается WhiteNoise, а Gunicorn запускает Django.

1. В Render создайте PostgreSQL и Web Service из ветки `staging`.
2. В поле **Build Command** укажите `./build.sh`.
3. В поле **Start Command** укажите
   `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT`.
4. Добавьте переменные окружения:
   - `DJANGO_SECRET_KEY` — новый случайный секрет;
   - `DJANGO_DEBUG=False`;
   - `DATABASE_URL` — внутренний URL базы PostgreSQL из Render;
   - `DJANGO_SERVE_MEDIA=True`;
   - `DJANGO_MEDIA_ROOT=/var/data/media`;
   - `AI_ENABLED=False` — отключает ИИ-помощника на демо-стенде.
5. Подключите постоянный диск Render в `/var/data`, чтобы загруженные
   фотографии не пропадали после нового развёртывания.
6. После первого запуска создайте отдельного администратора командой
   `python manage.py createsuperuser` в Render Shell. Не используйте
   стандартные или общие пароли.

Render автоматически задаёт `RENDER_EXTERNAL_HOSTNAME`; он используется как
разрешённый хост и CSRF-origin. При подключении собственного домена задайте
`DJANGO_ALLOWED_HOSTS` и `DJANGO_CSRF_TRUSTED_ORIGINS` явно.

## Avito XML

В карточке объекта включите «Включить в экспорт Avito», заполните обязательные данные и перейдите в раздел «XML для Avito» на странице объектов. Скачанный файл загружается в кабинет Avito вручную.

Перед размещением на Avito убедитесь, что адреса фотографий доступны из интернета: локальные ссылки `127.0.0.1` площадка не сможет загрузить.
