import os
import signal
import sys
import logging
import re
import html

from collections import OrderedDict
from datetime import datetime, timedelta
from functools import wraps

from django.core.management.base import BaseCommand
from django.conf import settings as main_settings
from django.utils.html import strip_tags
from django.db.models import Q, Count, Max
from django.db import connection
from django.contrib.auth.models import User
from django.utils.translation import gettext as _
from django.utils import translation

from asgiref.sync import sync_to_async

from django.contrib.postgres.aggregates import StringAgg

from opds_catalog.models import Book
from opds_catalog import settings, dl
from opds_catalog.opds_paginator import Paginator as OPDS_Paginator
from sopds_web_backend.settings import HALF_PAGES_LINKS
from constance import config

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import InvalidToken, BadRequest

from opds_catalog.network import build_telegram_request

query_delimiter = "####"

# Emoji vocabulary for friendlier, more expressive output.
EMOJI = {
    "books": "📚",
    "book": "📖",
    "author": "✍️",
    "search": "🔎",
    "download": "⬇️",
    "zip": "🗜️",
    "epub": "📱",
    "mobi": "📚",
    "ok": "✅",
    "warn": "⚠️",
    "info": "ℹ️",
    "error": "❌",
    "sparkles": "✨",
    "annotation": "📝",
    "year": "🗓️",
    "size": "📏",
    "lang": "🌐",
    "format": "📄",
    "hello": "👋",
    "hint": "💡",
    "first": "⏮",
    "prev": "◀️",
    "next": "▶️",
    "last": "⏭",
}


def esc(value):
    """Escape a dynamic value for safe use inside HTML-formatted messages."""
    return html.escape(str(value), quote=False)


def human_size(num):
    """Format a byte count into a compact, human-readable string."""
    try:
        num = int(num)
    except (TypeError, ValueError):
        return str(num)
    if num >= 1024 * 1024:
        return "%.1f МБ" % (num / (1024 * 1024))
    if num >= 1024:
        return "%.0f КБ" % (num / 1024)
    return "%d Б" % num


def cmdtrans(func):
    @wraps(func)
    async def wrapper(self, update: Update, context):
        translation.activate(config.SOPDS_LANGUAGE)
        try:
            result = await func(self, update, context)
        finally:
            translation.deactivate()
        return result

    return wrapper


def check_auth_decorator(func):
    @wraps(func)
    async def wrapper(self, update: Update, context):
        if not config.SOPDS_TELEBOT_AUTH:
            return await func(self, update, context)

        if connection.connection and not connection.is_usable():
            connection.close()

        if update.message:
            username = update.message.from_user.username
            chat_id = update.message.chat_id
        else:
            username = update.callback_query.from_user.username
            chat_id = update.callback_query.message.chat_id

        def _get_user():
            return User.objects.filter(username__iexact=username).first()

        user = await sync_to_async(_get_user)()
        if user and user.is_active:
            return await func(self, update, context)

        await context.bot.send_message(
            chat_id=chat_id,
            text=_("Hello %s!\nUnfortunately you do not have access to information. Please contact the bot administrator.") % username)
        self.logger.info(_("Denied access for user: %s") % username)

        return None

    return wrapper


class Command(BaseCommand):
    help = 'SimpleOPDS Telegram Bot engine.'

    # The above code is creating a variable named "query_cache".
    query_cache = OrderedDict()
    query_cache_max_size = 10
    query_cache_max_age = timedelta(days=2)

    can_import_settings = True
    leave_locale_alone = True

    def add_arguments(self, parser):
        parser.add_argument('command', help='Use [ start | stop | restart ]')
        parser.add_argument('--verbose', action='store_true', dest='verbose', default=False, help='Set verbosity level for SimpleOPDS telebot.')
        return None

    def handle(self, *args, **options):
        self.pidfile = os.path.join(main_settings.BASE_DIR, config.SOPDS_TELEBOT_PID)
        action = options['command']
        self.logger = logging.getLogger('')
        self.logger.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')

        if settings.LOGLEVEL != logging.NOTSET:
            # Создаем обработчик для записи логов в файл
            fh = logging.FileHandler(config.SOPDS_TELEBOT_LOG)
            fh.setLevel(settings.LOGLEVEL)
            fh.setFormatter(formatter)
            self.logger.addHandler(fh)

        if options['verbose']:
            # Создадим обработчик для вывода логов на экран с максимальным уровнем вывода
            ch = logging.StreamHandler()
            ch.setLevel(logging.DEBUG)
            ch.setFormatter(formatter)
            self.logger.addHandler(ch)

        if action == "start":
            self.start()
        elif action == "stop":
            pid = open(self.pidfile, "r").read()
            self.stop(pid)
        elif action == "restart":
            pid = open(self.pidfile, "r").read()
            self.restart(pid)
        return None

    @cmdtrans
    @check_auth_decorator
    async def startCommand(self, update: Update, context):
        username = update.message.from_user.username or ""
        greeting = _('%(subtitle)s\nHello %(username)s! To search for a book, enter part of her title or author:') % {'subtitle': esc(settings.SUBTITLE), 'username': esc(username)}
        text = (
            f"{EMOJI['books']} {greeting}\n\n"
            f"<blockquote>{EMOJI['hint']} Минимум 3 символа — найду книги и предложу форматы для скачивания.</blockquote>"
        )
        await context.bot.send_message(chat_id=update.message.chat_id, text=text, parse_mode='HTML')
        self.logger.info("Start talking with user: %s" % update.message.from_user)
        return None

    def bookFilter(self, query):
        if connection.connection and not connection.is_usable():
            connection.close()
        if query in self.query_cache:
            self.query_cache.move_to_end(query)
            timestamp, books = self.query_cache[query]
            if datetime.now() - timestamp <= self.query_cache_max_age:
                return books
            else:
                self.logger.info("Query '%s' is too old in query cache." % query)
        q_objects = Q()
        q_objects.add(Q(search_title__contains=query.upper()), Q.OR)
        q_objects.add(Q(authors__search_full_name__contains=query.upper()), Q.OR)
        books = Book.objects.filter(q_objects).annotate(authors_set=StringAgg("authors__full_name", delimiter=", "))
        if config.SOPDS_DOUBLES_HIDE:
            books = books.values("title", "search_title", "authors_set").annotate(doubles=Count("filename"), id=Max("id")).order_by("search_title").distinct()
        else:
            books = books.values("title", "search_title", "authors_set", "id", "docdate").order_by('search_title', '-docdate').distinct()
        self.query_cache[query] = datetime.now(), books
        if len(self.query_cache) > self.query_cache_max_size:
            query_old, _books_old = self.query_cache.popitem(0)
            self.logger.info("Query cache is overloaded. Query '%s' is removed from query cache." % query_old)

        return books

    def bookPager(self, books, page_num, query):
        # as I can understand, len de-facto reads all items in memory or QuerySet cache
        books_count = len(books)
        op = OPDS_Paginator(books_count, 0, page_num, config.SOPDS_TELEBOT_MAXITEMS, HALF_PAGES_LINKS)
        summary_doubles = config.SOPDS_DOUBLES_HIDE

        start = op.d1_first_pos if (op.d1_first_pos == 0) else op.d1_first_pos - 1
        finish = op.d1_last_pos

        response = ''
        for b in books[start:finish + 1]:
            doubles = _("(doubles:%s) ") % b['doubles'] if summary_doubles and b['doubles'] else ''
            title = esc(b['title'])
            author = esc(b['authors_set'] or '—')
            entry = (
                f"{EMOJI['book']} <b>{title}</b>\n"
                f"{EMOJI['author']} {author}\n"
                f"{EMOJI['download']} <code>/download{b['id']}</code>"
            )
            if doubles:
                entry += f"  {EMOJI['info']} {doubles.strip()}"
            response += entry + "\n\n"

        # fix for rare empty response
        if response:
            buttons = [
                InlineKeyboardButton(f"{EMOJI['first']} 1", callback_data='%s%s%s' % (query, query_delimiter, 1)),
                InlineKeyboardButton(f"{EMOJI['prev']} {op.previous_page_number}", callback_data='%s%s%s' % (query, query_delimiter, op.previous_page_number)),
                InlineKeyboardButton(f"{op.number} / {op.num_pages}", callback_data='%s%s%s' % (query, query_delimiter, 'current')),
                InlineKeyboardButton(f"{op.next_page_number} {EMOJI['next']}", callback_data='%s%s%s' % (query, query_delimiter, op.next_page_number)),
                InlineKeyboardButton(f"{op.num_pages} {EMOJI['last']}", callback_data='%s%s%s' % (query, query_delimiter, op.num_pages)),
            ]
            markup = InlineKeyboardMarkup([buttons]) if op.num_pages > 1 else None
            return {'message': response, 'buttons': markup}
        else:
            return self.bookPager(books, page_num - 1, query)

    @cmdtrans
    @check_auth_decorator
    async def getBooks(self, update: Update, context):
        query = update.message.text
        self.logger.info("Got message from user %s: %s" % (update.message.from_user.username, query))

        if len(query) < 3:
            response = f"{EMOJI['warn']} {_('Too short for search, please try again.')}"
        else:
            response = f"{EMOJI['search']} " + _("I'm searching for the book: %s") % esc(query)

        await context.bot.send_message(chat_id=update.message.chat_id, text=response, parse_mode='HTML')
        self.logger.info("Send message to user %s: %s" % (update.message.from_user.username, response))

        if len(query) < 3:
            return None

        books = await sync_to_async(self.bookFilter)(query)
        books_count = len(books)

        if books_count == 0:
            response = f"{EMOJI['error']} {_('No results were found for your query, please try again.')}"
            await context.bot.send_message(chat_id=update.message.chat_id, text=response, parse_mode='HTML')
            self.logger.info("Send message to user %s: %s" % (update.message.from_user.username, response))
            return

        response = f"{EMOJI['ok']} " + _("Found %s books.\nI create list, after a few seconds, select the file to download:") % books_count
        await context.bot.send_message(chat_id=update.message.chat_id, text=response, parse_mode='HTML')
        self.logger.info("Send message to user %s: %s" % (update.message.from_user.username, response))

        response = self.bookPager(books, 1, query)
        await context.bot.send_message(chat_id=update.message.chat_id, text=response['message'], parse_mode='HTML', reply_markup=response['buttons'])
        return None

    @cmdtrans
    @check_auth_decorator
    async def getBooksPage(self, update: Update, context):
        callback_query = update.callback_query
        (query, page_num) = callback_query.data.split(query_delimiter, maxsplit=1)
        if (page_num == 'current'):
            return
        try:
            page_num = int(page_num)
        except ValueError:
            page_num = 1

        books = await sync_to_async(self.bookFilter)(query)
        response = self.bookPager(books, page_num, query)
        try:
            await context.bot.edit_message_text(chat_id=callback_query.message.chat_id, message_id=callback_query.message.message_id, text=response['message'], parse_mode='HTML', reply_markup=response['buttons'])
        except BadRequest:
            pass
        return None

    @cmdtrans
    @check_auth_decorator
    async def downloadBooks(self, update: Update, context):
        book_id_set = re.findall(r'\d+$', update.message.text)

        def _get_book(book_id):
            return Book.objects.get(id=book_id)

        book = None
        if len(book_id_set) == 1:
            try:
                book = await sync_to_async(_get_book)(int(book_id_set[0]))
            except (Book.DoesNotExist, ValueError):
                book = None

        if book is None:
            response = f"{EMOJI['error']} {_('The book on the link you specified is not found, try to repeat the book search first.')}"
            await context.bot.send_message(chat_id=update.message.chat_id, text=response, parse_mode='HTML')
            self.logger.info("Not find download links: %s" % response)
            return

        authors = ', '.join([a['full_name'] for a in book.authors.values()])
        annotation = esc(strip_tags(book.annotation)[:3000])

        meta = (
            "<pre>"
            f"Год:    {esc(book.docdate or '—')}\n"
            f"Язык:   {esc(book.lang or '—')}\n"
            f"Размер: {human_size(book.filesize)}\n"
            f"Формат: {esc(book.format.upper())}"
            "</pre>"
        )

        response = (
            f"{EMOJI['book']} <b>{esc(book.title)}</b>\n"
            f"{EMOJI['author']} {esc(authors or '—')}\n\n"
            f"{meta}"
        )
        if annotation:
            response += f"\n{EMOJI['annotation']} <b>{_('Annotation:')}</b>\n<span class=\"tg-spoiler\">{annotation}</span>"

        buttons = [InlineKeyboardButton(f"{EMOJI['format']} {book.format.upper()}", callback_data='/getfileorig%s' % book.id)]
        if book.format not in settings.NOZIP_FORMATS:
            buttons += [InlineKeyboardButton(f"{EMOJI['zip']} {book.format.upper()}.ZIP", callback_data='/getfilezip%s' % book.id)]
        if (config.SOPDS_FB2TOEPUB != "") and (book.format == 'fb2'):
            buttons += [InlineKeyboardButton(f"{EMOJI['epub']} EPUB", callback_data='/getfileepub%s' % book.id)]
        if (config.SOPDS_FB2TOMOBI != "") and (book.format == 'fb2'):
            buttons += [InlineKeyboardButton(f"{EMOJI['mobi']} MOBI", callback_data='/getfilemobi%s' % book.id)]

        markup = InlineKeyboardMarkup([buttons])
        await context.bot.send_message(chat_id=update.message.chat_id, text=response, parse_mode='HTML', reply_markup=markup)
        self.logger.info("Send download buttons.")
        return None

    @cmdtrans
    @check_auth_decorator
    async def getBookFile(self, update: Update, context):
        callback_query = update.callback_query
        query = callback_query.data
        book_id_set = re.findall(r'\d+$', query)

        def _get_book(book_id):
            return Book.objects.get(id=book_id)

        book = None
        if len(book_id_set) == 1:
            try:
                book = await sync_to_async(_get_book)(int(book_id_set[0]))
            except (Book.DoesNotExist, ValueError):
                book = None

        if book is None:
            response = f"{EMOJI['error']} {_('The book on the link you specified is not found, try to repeat the book search first.')}"
            await context.bot.send_message(chat_id=callback_query.message.chat_id, text=response, parse_mode='HTML')
            self.logger.info("Not find download links: %s" % response)
            return

        filename = await sync_to_async(dl.getFileName)(book)
        document = None

        if re.match(r'/getfileorig', query):
            document = await sync_to_async(dl.getFileData)(book)

        if re.match(r'/getfilezip', query):
            document = await sync_to_async(dl.getFileDataZip)(book)
            filename = filename + '.zip'

        if re.match(r'/getfileepub', query):
            document = await sync_to_async(dl.getFileDataEpub)(book)
            filename = filename.replace('.fb2', '.epub')

        if re.match(r'/getfilemobi', query):
            document = await sync_to_async(dl.getFileDataMobi)(book)
            filename = filename.replace('.fb2', '.mobi')

        if document:
            await context.bot.send_document(chat_id=callback_query.message.chat_id, document=document, filename=filename)
            document.close()
            self.logger.info("Send file: %s" % filename)
        else:
            response = f"{EMOJI['error']} {_('There was a technical error, please contact the Bot administrator.')}"
            await context.bot.send_message(chat_id=callback_query.message.chat_id, text=response, parse_mode='HTML')
            self.logger.info("Book get error: %s" % response)

        return None

    @cmdtrans
    @check_auth_decorator
    async def botCallback(self, update: Update, context):
        query = update.callback_query

        if re.match(r'/getfile', query.data):
            return await self.getBookFile(update, context)
        else:
            return await self.getBooksPage(update, context)

    def start(self):
        if not config.SOPDS_TELEBOT_API_TOKEN:
            self.stdout.write('Telegram bot token is not set.\nSet correct token for telegram API by command:\n python3 manage.py sopds_util setconf SOPDS_TELEBOT_API_TOKEN "<token>"')
            return None

        try:
            proxy_url = str(config.SOPDS_TELEBOT_PROXY_URL or "").strip()
            request = build_telegram_request(proxy_url)
            updates_request = build_telegram_request(proxy_url)
            writepid(self.pidfile)
            application = (
                Application.builder()
                .token(config.SOPDS_TELEBOT_API_TOKEN)
                .request(request)
                .get_updates_request(updates_request)
                .build()
            )

            start_command_handler = CommandHandler('start', self.startCommand)
            download_handler = MessageHandler(filters.Regex('^/download\\d+$'), self.downloadBooks)
            get_book_handler = MessageHandler(filters.TEXT, self.getBooks)

            application.add_handler(start_command_handler)
            # change order of handlers, to handle download(regexp) before common text(book name)
            application.add_handler(download_handler)
            application.add_handler(get_book_handler)
            application.add_handler(CallbackQueryHandler(self.botCallback))

            quit_command = 'CTRL-BREAK' if sys.platform == 'win32' else 'CONTROL-C'
            self.stdout.write("Quit the sopds_telebot with %s.\n" % quit_command)

            application.run_polling(drop_pending_updates=True)
        except ValueError as exc:
            self.stdout.write(f'Invalid Telegram proxy configuration: {exc}')
            self.logger.error('Invalid Telegram proxy configuration: %s', exc)
        except InvalidToken:
            self.stdout.write('Invalid telegram token.\nSet correct token for telegram API by command:\n python3 manage.py sopds_util setconf SOPDS_TELEBOT_API_TOKEN "<token>"')
            self.logger.error('Invalid telegram token.')

        except (KeyboardInterrupt, SystemExit):
            pass

        return None

    def stop(self, pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except OSError as e:
            self.stdout.write("Error stopping sopds_telebot: %s" % str(e))
        return None

    def restart(self, pid):
        self.stop(pid)
        self.start()
        return None


def writepid(pid_file):
    """
    Write the process ID to disk.
    """
    fp = open(pid_file, "w")
    fp.write(str(os.getpid()))
    fp.close()
