# Прокторинг EKTU

Сервис записывает экзамен в браузере слушателя: веб-камеру, микрофон и весь экран.
Запись по частям уходит на наш сервер и хранится только у нас. Сотрудник смотрит её
на `/proctoring/review/` и решает, засчитывается ли экзамен.

Устанавливать слушателю ничего не нужно: достаточно компьютера с камерой и браузера
Chrome, Edge, Firefox или Яндекс. С телефона и планшета записать экран нельзя.

## Как устроено

| Часть | Файл | Что делает |
|---|---|---|
| Ядро | `models.py`, `services.py`, `storage.py`, `tasks.py` | Сеансы, записи, события, файлы на диске. Про Open edX ничего не знает. |
| Страница записи | `views_capture.py`, `proctoring_capture.html`, `proctoring_capture.js` | Вкладка, которую слушатель держит открытой весь экзамен. |
| Проверка | `views_review.py`, `proctoring_review.html`, `proctoring_review.js` | Список записей, плеер, решение. |
| Open edX | `backend.py`, `edx.py` | Провайдер `ektu` для штатного `edx-proctoring`. |
| Другие системы | `views_api.py` | REST API с ключами. |

Сеанс проходит состояния `created` → `recording` → `stopping` → `finished`.
Решение проверяющего: `pending`, `passed` или `failed`.

## Настройки

Словарь `EKTU_PROCTORING` в настройках Django, значения по умолчанию в `conf.py`:

- `ROOT` — папка с записями. Не должна лежать внутри `MEDIA_ROOT`: тот раздаётся наружу.
- `MIN_FREE_MB` — новая запись не начнётся, если на диске свободно меньше.
- `WEBCAM_BITS`, `SCREEN_BITS`, `AUDIO_BITS` — качество записи. По умолчанию выходит
  около 220 МБ на человека за час.
- `RETENTION_DAYS` — срок хранения для команды `./manage.py lms ektu_proctoring_cleanup`.
  Сама по расписанию она не запускается.

## В Open edX

1. В Studio у курса: Advanced Settings → «Enable Proctored Exams» = `true`.
2. У подраздела с экзаменом: Configure → Special Exam → Proctored, задать время.
3. Слушатели должны быть записаны в режиме `honor` (или `verified`). Слушателям в
   режиме `audit` Open edX показывает экзамен без прокторинга: это штатная логика.

Слушатель нажимает «Начать проверку системы», в новой вкладке соглашается на запись,
включает камеру и экран, возвращается и начинает экзамен. Запись останавливается сама,
когда экзамен отправлен. После решения проверяющего попытка получает статус
`verified` или `rejected`, слушателю уходит штатное письмо.

## REST API для других систем

Ключ выдаётся в админке Django: «EKTU proctoring» → «API clients». Он показывается один
раз, в базе хранится только его хеш. Каждый запрос несёт заголовок
`Authorization: Bearer <ключ>`. Клиент видит только свои сеансы.

| Запрос | Что делает |
|---|---|
| `POST /proctoring/api/v1/sessions/` | Заказать сеанс. Поля: `external_ref`, `subject_name`, `subject_ref`, `exam_name`, `return_url`, `record_webcam`, `record_audio`, `record_screen`. В ответе `id` и `capture_url`. |
| `GET /proctoring/api/v1/sessions/<id>/` | Состояние, события, записи и `review_url`. |
| `POST /proctoring/api/v1/sessions/<id>/exam-started/` | Отметить начало самого экзамена. |
| `POST /proctoring/api/v1/sessions/<id>/finish/` | Экзамен окончен: страница записи досылает остаток и останавливается. |
| `POST /proctoring/api/v1/sessions/<id>/review/` | Записать решение: `verdict` = `passed` или `failed`, `comment`. |

Порядок работы внешней системы:

1. Заказать сеанс и открыть слушателю `capture_url` в новой вкладке.
2. Дождаться состояния `recording` (опросом или вебхуком) и пустить слушателя к экзамену.
3. Когда экзамен сдан, вызвать `finish`.
4. Открыть проверяющему `review_url`. Ссылка подписана, действует 12 часов и не требует
   учётной записи у нас; свежую даёт каждый `GET`.

Если у клиента задан `webhook_url`, на него приходит `POST` с JSON
`{"event": "ready" | "finished" | "reviewed", "session": {...}}`. Заголовок
`X-Ektu-Signature: sha256=<hex>` — это HMAC-SHA256 тела с ключом `webhook_secret`.
