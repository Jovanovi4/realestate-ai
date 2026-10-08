# Автоматические внешние копии Docker-пилота

Для пилота рекомендуется отдельный приватный бакет Yandex Object Storage.
Скрипты используют S3 через restic и не зависят от конкретного S3-провайдера.
Restic шифрует файлы перед отправкой; бакет хранит только зашифрованные данные.
Ключ шифрования и ключ доступа храните отдельно от VPS и Git: без пароля restic
восстановить данные нельзя. Тестовый бакет и доступ создаются в вашем облачном
аккаунте, поэтому до этого шага внешняя копия **ещё не работает**.

## Подготовить внешнее хранилище

1. В Yandex Cloud создайте отдельный бакет без публичного доступа к объектам,
   списку объектов и настройкам. Для первого пилота подойдёт стандартный класс.
   Имя бакета должно быть уникальным; не используйте точку в имени. Настройте
   контроль расходов в облачном аккаунте.
2. Создайте сервисный аккаунт для резервных копий и дайте ему доступ к этому
   бакету (`storage.editor` на бакет). Создайте для него статический ключ S3.
   ID и секрет ключа не присылайте в чат, не записывайте в Git.
3. Сохраните вне VPS пароль restic, ID/секрет S3 и инструкцию восстановления.
   Пароль приложения Яндекс Почты, токены ботов и `.env.docker` тоже нужны при
   восстановлении сервиса, но **не входят** в эту копию. Их храните отдельно.

[Создание приватного бакета](https://yandex.cloud/ru/docs/storage/operations/buckets/create),
[аутентификация в S3 API](https://yandex.cloud/ru/docs/storage/s3/),
[формат S3-репозитория restic](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

## Установить на VPS

После доставки свежих файлов `deploy/` на VPS из этого проекта установите
пакет restic и root-owned копии скриптов. Служба работает от root, поскольку
ей нужен доступ к Docker; исполняемые скрипты поэтому нельзя оставлять
доступными на запись пользователю `deploy`.

```sh
cd ~/realestate-ai
sudo apt update
sudo apt install restic
sudo install -d -o root -g root -m 700 /etc/reaicrm-pilot-backup /var/backups/reaicrm-pilot
sudo install -d -o root -g root -m 755 /usr/local/libexec/reaicrm-pilot
sudo install -o root -g root -m 755 deploy/backup_verify_pilot.sh deploy/backup_offsite_pilot.sh deploy/restic_pilot_command.sh /usr/local/libexec/reaicrm-pilot/
sudo install -o root -g root -m 644 deploy/verify_media_archive.py deploy/prune_verified_pilot_backups.py /usr/local/libexec/reaicrm-pilot/
sudo install -o root -g root -m 600 deploy/systemd/reaicrm-pilot-backup.env.example /etc/reaicrm-pilot-backup/restic.env
sudo install -o root -g root -m 644 deploy/systemd/reaicrm-pilot-backup.service.example /etc/systemd/system/reaicrm-pilot-backup.service
sudo install -o root -g root -m 644 deploy/systemd/reaicrm-pilot-backup.timer.example /etc/systemd/system/reaicrm-pilot-backup.timer
```

Создайте уникальный пароль репозитория без вывода в терминал. Сохраните его
копию в отдельном защищённом месте: файл на VPS не переживёт потерю сервера.

```sh
sudo sh -c 'umask 077; openssl rand -base64 48 > /etc/reaicrm-pilot-backup/password'
sudoedit /etc/reaicrm-pilot-backup/restic.env
sudo systemctl daemon-reload
sudo systemd-analyze verify /etc/systemd/system/reaicrm-pilot-backup.service /etc/systemd/system/reaicrm-pilot-backup.timer
systemd-analyze calendar '*-*-* 00:30:00 UTC'
```

В `restic.env` замените `PRIVATE_BUCKET_NAME`, ID и секрет сервисного ключа.
Оставьте строки в формате `KEY='значение'`; значения не должны содержать
одинарную кавычку. Адрес репозитория для Yandex Object Storage:
`s3:https://storage.yandexcloud.net/ИМЯ_БАКЕТА/reaicrm-pilot`.
Ключи не копируйте в `.env.docker`: это отдельный доступ только для копий.

## Проверить вручную до запуска расписания

Инициализацию репозитория выполните **один раз**. Если `init` говорит, что
репозиторий уже существует, проверьте адрес и пароль; не создавайте новый
репозиторий поверх прежнего.

```sh
sudo /usr/local/libexec/reaicrm-pilot/restic_pilot_command.sh init
sudo systemctl start reaicrm-pilot-backup.service
sudo systemctl status reaicrm-pilot-backup.service --no-pager
sudo journalctl -u reaicrm-pilot-backup.service -n 80 --no-pager
sudo /usr/local/libexec/reaicrm-pilot/restic_pilot_command.sh snapshots --tag reaicrm-pilot
```

Успешный запуск выводит `Encrypted offsite backup verified: ID` и создаёт
`OFFSITE` рядом с локальной `VERIFIED` в `/var/backups/reaicrm-pilot/pilot-*`.
Если служба завершилась ошибкой, таймер пока не включайте. Команда сначала
останавливает `web`, `outbound`, `publications` и работающий `account-emails`,
делает согласованную копию и сразу запускает их обратно. Проверка и отправка
происходят после возобновления работы сайта.

Для независимого испытания скачайте новый снимок в отдельный каталог, не
затрагивая боевую БД. Подставьте ID из `snapshots` вместо `SNAPSHOT_ID`:

```sh
sudo install -d -o root -g root -m 700 /var/backups/reaicrm-offsite-restore-test
sudo /usr/local/libexec/reaicrm-pilot/restic_pilot_command.sh restore SNAPSHOT_ID --target /var/backups/reaicrm-offsite-restore-test
sudo find /var/backups/reaicrm-offsite-restore-test -name VERIFIED -type f -print
```

Найдите каталог с `VERIFIED`, затем в нём выполните `sha256sum -c SHA256SUMS`.
Ожидаются `database.dump: OK` и `media.tar.gz: OK`. Исходная копия до
отправки уже была восстановлена в отдельном PostgreSQL-контейнере, поэтому
совпадение этих хешей подтверждает, что облако вернуло те же проверенные
файлы. После проверки временный каталог можно удалить вручную.

Только после успешной удалённой проверки включите расписание:

```sh
sudo systemctl enable --now reaicrm-pilot-backup.timer
systemctl list-timers reaicrm-pilot-backup.timer
```

Запуск назначен ежедневно на 00:30 UTC с задержкой до 10 минут (03:30–03:40
по Москве и Каиру в текущем часовом поясе). После пропуска из-за перезагрузки
`Persistent=true` запускает задачу при следующем старте. Повторный запуск
защищён файловой блокировкой. После каждого успешного запуска restic проверяет
новый снимок и оставляет последние 2, 7 ежедневных, 4 еженедельных и 3
ежемесячных снимка. Локально остаются 3 последние проверенные и отправленные
копии; неотправленные копии не удаляются. Старые ручные копии в
`~/reaicrm-backups` не затрагиваются.

Состояние расписания и ошибок смотрите через `systemctl status` и `journalctl`.
Раз в месяц восстанавливайте последний снимок во временный каталог и
сверяйте хеши. Периодически выполняйте `restic check --read-data` в окно,
когда допустимо скачать весь репозиторий; обычный ежедневный `check` проверяет
структуру, но не перечитывает все данные. [Проверки restic](https://restic.readthedocs.io/en/stable/045_working_with_repos.html),
[политики хранения](https://restic.readthedocs.io/en/stable/060_forget.html).
