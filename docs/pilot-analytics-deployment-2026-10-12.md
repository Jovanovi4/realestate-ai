# Обновление пилота и проверка аналитики на PostgreSQL

Архив `reaicrm-pilot-design-form-20261012-v3.tar.gz` содержит текущее рабочее
дерево ветки `design_form`, включая незакоммиченные изменения аналитики. В нём
нет локальной SQLite, медиа, виртуального окружения, `.env.docker` и ключей.
Команды ниже не публикуют код в GitHub и не заменяют настройки пилота.

## 1. На Mac: передать два файла

```bash
scp /private/tmp/reaicrm-pilot-design-form-20261012-v3.tar.gz \
  /private/tmp/reaicrm-pilot-design-form-20261012-v3.tar.gz.sha256 \
  deploy@VPS_IP:~/
```

Пароль SSH вводится в терминале. Не отправляйте его в чат.

## 2. На VPS: проверить архив и сделать восстановимую копию

```bash
ssh deploy@VPS_IP
cd ~
sha256sum -c reaicrm-pilot-design-form-20261012-v3.tar.gz.sha256
cd ~/realestate-ai
sudo docker compose --env-file .env.docker --profile mail config --quiet
bash deploy/backup_verify_pilot.sh
sudo docker image tag realestate-ai:pilot realestate-ai:pilot-before-analytics-20261012
```

Продолжайте только после `OK` у контрольной суммы и строки `Verified backup`.
Скрипт копии на короткое время останавливает сервисы записи, затем запускает их
и восстанавливает БД и медиа в изолированной проверке. Сохраните путь копии.

## 3. На VPS: собрать образ и проверить его на отдельной тестовой БД

```bash
cd ~/realestate-ai
tar -xzf ~/reaicrm-pilot-design-form-20261012-v3.tar.gz -C ~/realestate-ai
sudo docker compose --env-file .env.docker --profile mail config --quiet
sudo docker compose --env-file .env.docker --profile mail build web
sudo docker compose --env-file .env.docker --profile mail run --rm --no-deps \
  -e DJANGO_DEBUG=True \
  -e CACHE_BACKEND=django.core.cache.backends.locmem.LocMemCache \
  -e DJANGO_MEDIA_ROOT=/tmp/reaicrm-test-media \
  web python manage.py test \
  properties.test_analytics_audit properties.test_analytics \
  properties.test_extended_analytics --verbosity 1 --noinput
```

Ожидается `OK` без пропуска PostgreSQL-теста. Если тесты не прошли, остановитесь:
работающие контейнеры ещё используют прежний образ. Результаты тестов относятся
к отдельной БД `test_realestate_ai`, а не к рабочим записям пилота.

## 4. На VPS: обновить сервисы и миграции

```bash
cd ~/realestate-ai
sudo docker compose --env-file .env.docker --profile mail stop \
  web outbound publications account-emails
sudo docker compose --env-file .env.docker --profile mail run --rm --no-deps migrate
sudo docker compose --env-file .env.docker --profile mail up -d --no-build \
  --no-deps --force-recreate web outbound publications account-emails
sudo docker compose --env-file .env.docker --profile mail ps --all
sudo docker compose --env-file .env.docker exec -T web \
  python manage.py check --deploy
sudo docker compose --env-file .env.docker exec -T web \
  python manage.py showmigrations properties
```

Команда `run ... migrate` должна завершиться с кодом 0. Затем дождитесь
`healthy` у `web`; миграция `0057` должна иметь `[X]`. Предупреждение только
о HSTS preload допустимо для пилота.
Если миграция или запуск не удались, сохраните копию и журнал ошибки, не
запускайте аудит на частично обновлённом приложении.

## 5. На VPS: сверить живые агрегаты

Сначала получите только числовые ID пользователей и команд:

```bash
cd ~/realestate-ai
sudo docker compose --env-file .env.docker exec -T web python manage.py shell -c \
  'from django.contrib.auth import get_user_model; from properties.models import AgencyMembership; print("user_ids:", list(get_user_model().objects.order_by("pk").values_list("pk", flat=True))); print("memberships:", list(AgencyMembership.objects.order_by("agency_id", "user_id").values_list("agency_id", "user_id", "role")))'
```

Замените `2` на ID своего пользователя из вывода. Аудит только читает рабочую
PostgreSQL и выполняется в одной read-only транзакции. Для каждого периода
файл содержит лишь числовые агрегаты и технические ID.

```bash
AUDIT_USER_ID=2
for days in 7 30 90; do
  sudo docker compose --env-file .env.docker exec -T web \
    python manage.py audit_crm_analytics \
    --user-id "$AUDIT_USER_ID" --scope personal --days "$days" \
    > "$HOME/reaicrm-backups/analytics-personal-${days}.json" || break
  python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["database"], r["scope"], r["period"], "mismatches:", r["mismatches"], "performance:", r["performance"])' \
    "$HOME/reaicrm-backups/analytics-personal-${days}.json"
done
```

Если в `memberships` есть ваша роль `owner` или `manager`, повторите цикл с
`--scope team` и именем файла `analytics-team-${days}.json`. Для проверки
сотрудника добавьте `--member-id ID_СОТРУДНИКА`; агент не получает доступ к
чужим показателям даже при таком параметре. Если команды на живом пилоте ещё
нет, командный путь уже проверен PostgreSQL-тестами из шага 3; сравнение с
живыми командными записями станет возможным после создания тестовой команды.

Ожидается `database: postgresql` и `mismatches: []`. Смотрите `page_ms` и
`page_queries` отдельно от `analytics_ms` и `analytics_queries`. Повторите
аудит трижды на прогретой базе для оценки времени. При расхождении сохраните
JSON и не считайте цифры отчёта подтверждёнными. Не присылайте `.env.docker`,
пароли, токены и полные выгрузки CRM.
