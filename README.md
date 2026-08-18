# SimpleOPDS (sopds)

**Простой OPDS-каталог для домашней библиотеки** — версия **0.47-devel**

![Screenshot](sopds_screenshot.png)

---

## Что это такое

**Simple OPDS** (sopds) — это веб-приложение для организации личной библиотеки
электронных книг (FB2, EPUB, PDF, DJVU и др.). Оно:

- сканирует каталог с книгами и строит по нему базу данных (авторы, жанры,
  серии, язык, обложки, аннотации);
- отдаёт библиотеку сразу в двух видах:
  - **Веб-интерфейс** — по адресу `/web/` (просмотр и поиск в браузере, онлайн-читалка);
  - **OPDS-каталог** — по адресу `/opds/` (стандарт, читаемый ридерами и
    приложениями вроде KyBook, FBReader, CoolReader, Moon+ Reader);
- позволяет скачивать книги и конвертировать FB2 в EPUB/MOBI;
- умеет выдавать библиотеку через **Telegram-бота**.

Официальный проект и автор — **Dmitry V. Shelepnev**:
[https://github.com/mitshel/sopds](https://github.com/mitshel/sopds)

---

## Откуда взят этот репозиторий

Это **форк** проекта SimpleOPDS. Цепочка происхождения:

1. **Оригинал** — [mitshel/sopds](https://github.com/mitshel/sopds), автор писал
   каждый компонент с нуля: сканер, парсеры FB2/ZIP/INPX, OPDS-фиды, веб-морду,
   Telegram-бота.
2. **Промежуточный форк** — [ichbinkirgiz/sopds](https://github.com/ichbinkirgiz/sopds):
   тёмная тема, русификация жанров, онлайн-читалка, аннотации книг и ряд
   косметических исправлений. Часть этих изменений (пул-реквест) была влита
   в историю.
3. **Этот форк** — [shohart/sopds](https://github.com/shohart/sopds): взят как база,
   затем выполнен технический ребрендинг стека и добавлен современный способ
   развёртывания (см. ниже).

### Что сделано в этом форке

Исходный проект был заморожен на старом стеке **Python 3.4 / Django 1.10–2.0**,
который уже не ставится на современные дистрибутивы и конфликтует с текущими
версиями библиотек. Цель форка — не «переписать», а **актуализировать и
адаптировать готовую, проверенную кодовую базу под сегодняшний день**, сохранив
всю функциональность и модель данных (коллекция из одного форка подходит для
переноса в этот).

Основные изменения:

- **Обновлён стек до современных версий**:
  - Python 3.12;
  - Django 5.2 (LTS);
  - python-telegram-bot 21 (асинхронный API);
  - psycopg3, gunicorn, whitenoise;
- **Удалён вендоренный модуль `constance`** — заменён на актуальный пакет
  `django-constance` из PyPI;
- **Переведён код под Django 5 / Python 3.12**:
  - `ugettext*` → `gettext*`, `django.conf.urls.url()` → `django.urls.re_path/path()`;
  - `import imp` → `importlib.util` (модуль удалён в Python 3.12);
  - `assertEquals/assertNotEquals` в тестах → `assertEqual/assertNotEqual`;
  - исправлен кэш обложек, обращавшийся к БД на этапе импорта;
  - чистые `SyntaxWarning` (экранирование `\&` и т.п.);
- **Telegram-бот переписан на асинхронную модель** python-telegram-bot 21
  (`Application.run_polling()`), работа с БД — через `sync_to_async`;
- **Добавлен современный способ развёртывания** — Docker Compose
  (PostgreSQL 16 + web + опциональный telebot) и документация, см. ниже;
- Конфигурация БД, `SECRET_KEY`, `DEBUG`, `TIME_ZONE` вынесены в переменные
  окружения.

> Сборка привязана к **PostgreSQL**. Поддержка sqlite/MySQL из старых инструкций
> здесь не используется — для большого числа книг многопользовательская БД
> нужнее, а Docker обеспечивает её «из коробки».

---

## Установка через Docker (рекомендуемый способ)

Это самый простой и надёжный путь. Весь стек — БД, веб-сервер, статика — поднимается
одной командой:

```bash
# 1. Клонируем
git clone https://github.com/shohart/sopds.git
cd sopds

# 2. Запускаем (PostgreSQL + web)
docker compose up -d --build

# 3. Открываем
#    Веб-интерфейс:  http://localhost:8080/web/
#    Админка:        http://localhost:8080/admin/
#    OPDS-каталог:   http://localhost:8080/opds/   (Basic Auth: admin / ADMIN_PASSWORD)
```

По умолчанию при первом старте создаётся администратор `admin` / `admin123`
(переопределяется переменными `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_EMAIL`).
**Перед выкладыванием в интернет обязательно замените `SECRET_KEY` на длинную
случайную строку** и поменяйте пароль администратора.

### Переменные окружения

Задаются в `docker-compose.yml` либо через файл `.env`:

| Переменная        | Назначение                                             | По умолчанию    |
|-------------------|--------------------------------------------------------|-----------------|
| `DB_NAME`         | Имя БД PostgreSQL                                      | `sopds`         |
| `DB_USER`         | Пользователь БД                                        | `sopds`         |
| `DB_PASS`         | Пароль БД                                              | `sopds`         |
| `DB_HOST`         | Хост БД (имя сервиса в compose)                        | `db`            |
| `DB_PORT`         | Порт БД                                                | `5432`          |
| `TIME_ZONE`       | Часовой пояс сервера                                   | `Europe/Moscow` |
| `DEBUG`           | Режим отладки Django (`true`/`false`)                  | `false`         |
| `SECRET_KEY`      | Секретный ключ Django                                  | задаётся в compose |
| `ADMIN_USER`      | Логин суперпользователя (создаётся при старте)         | `admin`         |
| `ADMIN_PASSWORD`  | Пароль суперпользователя                               | `admin123`      |
| `ADMIN_EMAIL`     | E-mail суперпользователя                               | пусто           |

### Настройка библиотеки

Каталог с книгами монтируется в именованный том `books` (внутри контейнера —
`/sopds/books`). Файл `Languages.txt` должен лежать там же, где и книги.

Корень библиотеки и прочие параметры каталогизатора настраиваются в веб-админке
(раздел **CONSTANCE → Настройки**) либо командой:

```bash
docker compose exec web python manage.py sopds_util setconf SOPDS_ROOT_LIB "/sopds/books"
```

Сканирование коллекции запускается автоматически по расписанию (по умолчанию
03:00 10-го числа каждого месяца) либо вручную через админку — для этого включите
опцию `SOPDS_SCAN_START_DIRECTLY`.

### Telegram-бот

Сервис `telebot` в `docker-compose.yml` по умолчанию выключен. Чтобы включить:

```bash
# задать токен (получить у @BotFather)
docker compose exec web python manage.py sopds_util setconf SOPDS_TELEBOT_API_TOKEN "123456:XXXX"

# при необходимости ограничить доступ только авторизованными пользователями БД
docker compose exec web python manage.py sopds_util setconf SOPDS_TELEBOT_AUTH True

# запустить сервис
docker compose up -d telebot
```

### Полезные команды

```bash
docker compose logs -f web              # логи веб-сервера
docker compose exec web python manage.py migrate   # миграции (выполняются автоматически)
docker compose down                     # остановить стек
docker compose down -v                  # остановить и удалить тома (БД и библиотеку!)
```

---

## Установка без Docker (вручную)

Требуется **Python 3.12** и PostgreSQL. Зависимости — из `requirements.txt`
(Django 5.2, django-constance, python-telegram-bot 21, psycopg3, gunicorn,
whitenoise, lxml, Pillow, APScheduler).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# переменные окружения (показано по умолчанию)
export DB_NAME=sopds DB_USER=sopds DB_PASS=sopds DB_HOST=127.0.0.1 DB_PORT=5432
export TIME_ZONE=Europe/Moscow SECRET_KEY="длинная-случайная-строка" DEBUG=false

python3 manage.py migrate
python3 manage.py createsuperuser

# корень библиотеки
python3 manage.py sopds_util setconf SOPDS_ROOT_LIB "путь-к-каталогу-с-книгами"
python3 manage.py sopds_util setconf SOPDS_LANGUAGE ru-RU

# одиночное сканирование
python3 manage.py sopds_scanner scan --verbose

# запуск через gunicorn (или ваш WSGI-сервер, точка входа ./sopds/wsgi.py)
gunicorn sopds.wsgi:application --bind 0.0.0.0:8080
```

Доступ: OPDS — `http://<сервер>:8080/opds/`, веб — `http://<сервер>:8080/web/`.

---

## Консольные команды

```bash
python3 manage.py sopds_util info                       # инфо о коллекции
python3 manage.py sopds_util clear [--verbose]          # очистить коллекцию, загрузить жанры
python3 manage.py sopds_util save_mygenres              # сохранить свой справочник жанров
python3 manage.py sopds_util load_mygenres              # загрузить свой справочник жанров
python3 manage.py sopds_util pg_optimize                # оптимизация таблицы книг (PostgreSQL)
python3 manage.py sopds_util getconf                    # все параметры конфигурации
python3 manage.py sopds_util getconf SOPDS_ROOT_LIB     # значение конкретного параметра
python3 manage.py sopds_util setconf SOPDS_ROOT_LIB "/path/to/books"   # задать параметр

python3 manage.py sopds_scanner scan [--verbose] [--daemon]     # разовое сканирование
python3 manage.py sopds_scanner start [--verbose] [--daemon]    # сканирование по расписанию
```

---

## Опции каталогизатора (раздел CONSTANCE в админке)

| Опция | Назначение | По умолчанию |
|-------|------------|--------------|
| `SOPDS_LANGUAGE` | Язык интерфейса | `en-US` |
| `SOPDS_ROOT_LIB` | Каталог с коллекцией книг | — |
| `SOPDS_BOOK_EXTENSIONS` | Расширения книг, попавших в каталог | `.pdf .djvu .fb2 .epub` |
| `SOPDS_DOUBLES_HIDE` | Скрывать найденные дубликаты | `True` |
| `SOPDS_FB2SAX` | Парсер FB2 (`True`=FB2sax, быстрый / `False`=FB2xpath) | `True` |
| `SOPDS_COVER_SHOW` | Показывать обложки | `True` |
| `SOPDS_ZIPSCAN` | Сканировать ZIP-архивы | `True` |
| `SOPDS_ZIPCODEPAGE` | Кодировка имён файлов в ZIP | `cp866` |
| `SOPDS_INPX_ENABLE` | Брать данные из INPX вместо сканирования | `True` |
| `SOPDS_INPX_SKIP_UNCHANGED` | Пропускать, если INPX не менялся | `True` |
| `SOPDS_INPX_TEST_ZIP` | Проверять наличие архивов из INPX | `False` |
| `SOPDS_INPX_TEST_FILES` | Проверять файлы книг внутри архивов | `False` |
| `SOPDS_DELETE_LOGICAL` | Логическое удаление (`True`) или физическое (`False`) | `False` |
| `SOPDS_SPLITITEMS` | Число элементов, при котором «раскрывается» группа | `300` |
| `SOPDS_MAXITEMS` | Результатов на страницу | `60` |
| `SOPDS_FB2TOEPUB` | Путь к конвертеру FB2→EPUB | `` |
| `SOPDS_FB2TOMOBI` | Путь к конвертеру FB2→MOBI | `` |
| `SOPDS_TEMP_DIR` | Временный каталог для конвертации | `<BASE_DIR>/tmp` |
| `SOPDS_TITLE_AS_FILENAME` | Имя скачиваемого файла = транслит названия книги | `True` |
| `SOPDS_ALPHABET_MENU` | Меню выбора алфавита | `True` |
| `SOPDS_NOCOVER_PATH` | Обложка для книг без обложки | `<BASE_DIR>/static/images/nocover.jpg` |
| `SOPDS_AUTH` | Включить BASIC-авторизацию | `True` |
| `SOPDS_SCAN_SHED_MIN/HOUR/DAY/DOW` | Расписание авто-сканирования | `0` / `0,12` / `*` / `*` |
| `SOPDS_SCAN_START_DIRECTLY` | Запустить внеочередное сканирование | `False` |
| `SOPDS_CACHE_TIME` | Время кэширования страницы, сек | `1200` |
| `SOPDS_TELEBOT_API_TOKEN` | Токен Telegram-бота | `` |
| `SOPDS_TELEBOT_AUTH` | Доступ к боту только пользователям БД | `True` |
| `SOPDS_TELEBOT_MAXITEMS` | Элементов на одно сообщение бота | `10` |

---

## Благодарности

- **Dmitry V. Shelepnev** ([@mitshel](https://github.com/mitshel)) — автор оригинального SimpleOPDS.
- **[@ichbinkirgiz](https://github.com/ichbinkirgiz)** — тёмная тема, русификация,
  онлайн-читалка и аннотации, ставшие основой промежуточного форка.
- **[@iAHTOH](https://github.com/iAHTOH)** — наводка на sopds и тёмная тема.
- **[@zveronline](https://github.com/zveronline)** — Docker.
- **[@bookpauk](https://github.com/bookpauk)** — [liberama](https://github.com/bookpauk/liberama), онлайн-читалка на omnireader.ru.

Права на исходный код принадлежат автору оригинального SimpleOPDS, **Dmitry V. Shelepnev**.