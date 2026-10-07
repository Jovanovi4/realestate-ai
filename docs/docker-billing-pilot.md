# Проверка тарифных ограничений на пилоте

Проводите её только после появления `VERIFIED` у резервной копии PostgreSQL и
медиа. До завершения сверки оставьте `BILLING_ENFORCEMENT_ENABLED=False` в
`.env.docker`. Команды выполняются в `~/realestate-ai` на VPS; содержимое
`.env.docker` и полные отчёты со сведениями о счетах не публикуйте.

Сначала проверьте серверные сценарии на отдельной базе, создаваемой тестовым
раннером Django. Она получает префикс `test_` и удаляется после прогона; том
медиа пилота не используется для тестовых загрузок:

```sh
sudo docker compose --env-file .env.docker run --rm --no-deps \
  -e DJANGO_DEBUG=True \
  -e CACHE_BACKEND=django.core.cache.backends.locmem.LocMemCache \
  -e DJANGO_MEDIA_ROOT=/tmp/reaicrm-test-media \
  -e BILLING_ENFORCEMENT_ENABLED=True \
  web python manage.py test billing.test_enforcement billing.test_postgres_contention --verbosity 2
```

Ожидаются успешные проверки отказа при исчерпании квоты объекта, лендинга и
ИИ-кредитов, сохранения данных при блокировке и двух одновременных запросов
на последнее место/последние кредиты. Тесты не доказывают корректность
учёта уже существующих данных пилота.

Далее сохраните отчёт о *текущих* тестовых счетах в приватном каталоге:

```sh
sudo docker compose --env-file .env.docker exec -T web \
  python manage.py billing_usage_report > ~/reaicrm-backups/billing-before.json
```

Сверьте у каждого счёта `inventory.references_complete`,
`inventory.file_sizes_known`, `inventory.issue_count`,
`unknown_operations`, `running_operations`, расход относительно `limits` и
остаток `ai`. Проверьте также `unassigned_media`. Если снимки инвентаря ещё не
созданы, после резервной копии выполните `reconcile_billing_usage`: команда
записывает учётные снимки, но не списывает старые ИИ-кредиты и не удаляет
файлы. Затем повторите `billing_usage_report` и разберите все расхождения.

Даже при успешном прогоне тестов не включайте флаг, пока не рассмотрены
состояния активных счетов и возможное превышение квот. Включение блокирует
некоторые действия и публичные лендинги сразу после перезапуска контейнеров.

Результат первого прогона на VPS зафиксирован в
[протоколе от 7 октября 2026 года](pilot-billing-verification-2026-10-07.md).
