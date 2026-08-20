# Podcast Cutter Privacy Policy

Version: 2026-08-18

This Policy describes data processed by the Telegram bot
`@podcast_cutter_bot`, operated as an independent software project by the Podcast Cutter
project. Privacy and rights contact: `podcast_cutter@inbox.ru`.

## Data processed

The Service may receive or store:

- Telegram user id and chat id supplied by Telegram;
- Telegram interface language and the language explicitly selected in the bot;
- the accepted Terms version and acceptance time;
- up to ten recently opened episodes, including title, feed id and source URL;
- operational events such as action, outcome, episode, timing and output size;
- a pending transcription job, including user/chat id, episode metadata and
  media URL;
- user requests in memory while they are being handled.

Raw podcast, person and transcript-search phrases are not written to the
operational journal. The Service does not intentionally request a phone number,
email address, contacts, precise location, payment data or Telegram password.

Podcast transcripts are indexed by source audio rather than by user and may be
reused to answer later searches. They concern public podcast material, but may
contain personal information spoken in the episode.

## Purposes

Data is used only to operate requested features, remember language and recent
episodes, deliver or resume transcription work, enforce limits and Terms,
investigate failures, measure basic reliability, handle deletion and rights
requests, prevent abuse and comply with law.

The Service does not sell personal data, build advertising profiles or use bot
messages to train machine-learning models.

## Services and transfers

- Telegram delivers updates and bot messages and independently processes them
  under its policies.
- Podcast Index receives directory search terms and API requests. The Service
  does not intentionally send it Telegram user ids.
- A podcast publisher or hosting provider receives a media request from the
  Service's network when an episode is processed.
- If an optional external speech-recognition backend is explicitly enabled,
  audio may be sent to that configured provider; the default backend is local.
- Encrypted operational backups may be stored with the configured backup
  provider.

These services may process data in other countries under their own terms. Do
not submit sensitive search text if this transfer is unacceptable to you.

## Retention

- terminal transcription-queue rows: up to 24 hours by default;
- operational journal: up to 90 days by default;
- language, Terms acceptance and recent episodes: up to 365 days after the
  profile was last updated by default;
- temporary downloaded media: deleted when the job ends;
- transcripts: retained until removed by the Operator, a rights request,
  policy change or termination of the Service;
- encrypted backups: deleted through the configured rotation, currently up to
  eight weeks, unless preservation is legally required.

The Operator may configure shorter periods. Active legal, security or abuse
investigations may require limited preservation.

## Security

The application encrypts its production database and WAL at rest with
SQLCipher, keeps transient SQLite pages in memory, creates files with
restricted filesystem permissions, keeps credentials outside the public
repository and limits access to the Operator. The project recovery bundle
stores the encrypted database and configuration together inside a separately
encrypted restic snapshot. No system can be guaranteed perfectly secure.

## Your choices and rights

- `/privacy` shows the current summary and Policy link.
- `/mydata` shows categories and counts linked to your Telegram id.
- `/delete_me confirm` deletes the stored language, acceptance, recents,
  journal events and transcription-queue rows linked to your id and detaches
  live waiters.
- `/copyright` provides the rights-request route.

Deletion cannot retract a clip or message already delivered to Telegram or a
copy another user made. Deleted rows may remain in encrypted backups until
rotation, where they are not restored except for disaster recovery and will be
deleted again after restoration.

You may also contact `podcast_cutter@inbox.ru` to request access, correction, restriction,
deletion or information about processing. Reasonable verification may be
required to prevent disclosure or deletion of another person's data.

## Children and changes

The Service is not directed to children who cannot validly accept these Terms
under applicable law. Material Policy changes receive a new date and, where
appropriate, a new Terms version requiring acceptance.

---

# Политика конфиденциальности Podcast Cutter

Версия: 2026-08-18

Политика описывает обработку данных Telegram-ботом `@podcast_cutter_bot`,
который поддерживается Ильёй Медведевым как независимый программный проект.
Контакт по конфиденциальности и правам: `podcast_cutter@inbox.ru`.

## Какие данные обрабатываются

Сервис может получать или сохранять:

- Telegram ID пользователя и chat ID, переданные Telegram;
- язык интерфейса Telegram и явно выбранный в боте язык;
- версию и время принятия Условий;
- до десяти недавно открытых эпизодов, включая название, feed ID и URL;
- служебные события: действие, результат, эпизод, длительность обработки и
  размер результата;
- ожидающее задание расшифровки с Telegram/chat ID, метаданными и URL медиа;
- запрос пользователя в оперативной памяти на время обработки.

Исходные поисковые фразы по подкастам, людям и расшифровкам не записываются в
служебный журнал. Сервис намеренно не запрашивает номер телефона, email,
контакты, точную геолокацию, платёжные данные или пароль Telegram.

Расшифровки индексируются по исходному аудио, а не по пользователю, и могут
повторно использоваться для последующих поисков. Они относятся к публичным
материалам подкаста, но могут содержать персональные сведения, прозвучавшие в
эпизоде.

## Цели обработки

Данные используются только для запрошенных функций, сохранения языка и списка
недавнего, доставки и восстановления расшифровки, применения лимитов и
Условий, расследования ошибок, базовой статистики надёжности, удаления данных,
обработки жалоб, предотвращения злоупотреблений и соблюдения закона.

Сервис не продаёт персональные данные, не строит рекламные профили и не
использует сообщения пользователей для обучения моделей.

## Сервисы и передача

- Telegram доставляет обновления и сообщения и самостоятельно обрабатывает их
  по своим правилам.
- Podcast Index получает поисковые фразы каталога и API-запросы. Telegram ID
  намеренно ему не передаётся.
- Издатель или хостинг подкаста получает сетевой запрос Сервиса при обработке
  эпизода.
- Если явно включён внешний сервис распознавания речи, аудио может быть
  передано этому провайдеру; по умолчанию распознавание локальное.
- Зашифрованные резервные копии могут храниться у настроенного провайдера.

Эти сервисы могут обрабатывать данные в других странах по собственным
правилам. Не отправляйте чувствительные поисковые запросы, если такая передача
для вас неприемлема.

## Сроки хранения

- завершённые строки очереди расшифровки: по умолчанию до 24 часов;
- служебный журнал: по умолчанию до 90 дней;
- язык, принятие Условий и недавние эпизоды: по умолчанию до 365 дней после
  последнего обновления профиля;
- временно загруженное медиа: удаляется после завершения задания;
- расшифровки: до удаления Оператором, по обращению о правах, при изменении
  политики или прекращении Сервиса;
- зашифрованные резервные копии: в пределах настроенной ротации, сейчас до
  восьми недель, если закон не требует сохранить сведения.

Оператор может установить меньшие сроки. Правомерное расследование нарушения
или инцидента может потребовать ограниченного сохранения.

## Безопасность

Приложение шифрует production-базу и WAL с помощью SQLCipher, держит временные страницы SQLite в памяти,
создаёт файлы с ограниченными правами, держит ключи вне публичного репозитория и
ограничивает доступ Оператором. В recovery-комплекте зашифрованная БД и конфигурация хранятся вместе
внутри отдельно зашифрованного restic-снимка. Абсолютная безопасность не гарантируется.

## Ваши права и управление

- `/privacy` показывает краткое описание и ссылку на Политику.
- `/mydata` показывает категории и количество строк, связанных с Telegram ID.
- `/delete_me confirm` удаляет язык, принятие Условий, недавние эпизоды,
  события журнала и строки очереди, связанные с Telegram ID, и отключает
  ожидающие запросы.
- `/copyright` показывает канал обращения о правах.

Удаление не отзывает клип или сообщение, уже доставленные в Telegram, либо
копию другого пользователя. Удалённые строки могут оставаться в зашифрованных
резервных копиях до ротации; они не используются иначе чем для аварийного
восстановления и после восстановления удаляются повторно.

Также можно обратиться на `podcast_cutter@inbox.ru` для доступа, исправления, ограничения,
удаления данных или сведений об обработке. Для защиты чужих данных может
потребоваться разумная проверка личности.

## Дети и изменения

Сервис не предназначен для детей, которые не могут самостоятельно принять
Условия по применимому закону. Существенные изменения получают новую дату и,
при необходимости, новую версию Условий с повторным принятием.
