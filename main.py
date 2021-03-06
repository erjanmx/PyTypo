import os
import re
import logging
import datetime

from time import sleep
from github3 import GitHub
from langdetect import detect
from autocorrect import spell
from dotenv import load_dotenv
from tinydb import TinyDB, Query
from github3.exceptions import NotFoundError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, Filters, RegexHandler

load_dotenv()

DB_PATH = os.getenv('DB_PATH')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
TELEGRAM_USER_ID = int(os.getenv('TELEGRAM_USER_ID'))

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

logger = logging.getLogger(__name__)

db = TinyDB(DB_PATH)
gh = GitHub(token=GITHUB_TOKEN)

keyboard = [
    [
        InlineKeyboardButton("Skip", callback_data='skip'),
        InlineKeyboardButton("Skip repo", callback_data='skip-repo'),
        InlineKeyboardButton("Ignore", callback_data='ignore'),
    ],
    [
        InlineKeyboardButton("Approve", callback_data='approve')
    ]
]
reply_markup = InlineKeyboardMarkup(keyboard)

repo_gen = None
skip_repo = False
ignore_word = False
approve_typo = False


def get_a_repo(date):
    global skip_repo, approve_typo, ignore_word
    query = Query()
    repos = gh.search_repositories('created:{0}..{0}'.format(date), order='stars')

    for repo in repos:
        repository = repo.repository
        readme = repository.readme().decoded.decode('utf-8')

        # skip if text is equal to repo name
        readme_detected_language = detect(readme)
        if readme_detected_language != 'en':
            logger.info('Detected language is not English: "%s"', readme_detected_language)
            continue

        words = set(filter(lambda w: re.search('^[a-zA-Z]{4,}$', w) is not None, readme.split()))
        for word in words:
            if skip_repo:
                logger.info('Skipping repo "%s"', repository.full_name)
                break

            # skip if text is equal to repo name
            if word.lower() == repository.name.lower():
                logger.info('Word is equal to repo name "%s"', word)
                continue

            # allow only one PR per repo. Be polite
            if db.search(query.repo == repository.full_name):
                logger.info('Already sent PR to this repo "%s"', repository.full_name)
                break

            # do not allow words with uppercase anywhere except the first letter
            if 0 < sum(1 for l in word[1:] if l.isupper()):
                continue

            suggested = spell(word)
            if suggested.lower() == word.lower():
                continue

            # search in ignore list
            if db.search(query.word == word.lower()):
                continue

            typo = word
            ignore_word = False
            approve_typo = False

            yield repository, typo, suggested, readme

            if ignore_word:
                add_to_ignore_list(typo)
            elif approve_typo:
                print('approving', repository.full_name, typo, suggested, sep=' ')
                correct(repository, readme, typo, suggested)
                add_to_approved_list(repository.full_name, typo, suggested)

        skip_repo = False


def correct(repository, readme, typo, suggested):
    fork = repository.create_fork()

    try:
        sleep(3)
        ref = fork.ref('heads/{}'.format(fork.default_branch))

        fix_typo_branch = 'fix-readme-typo'

        sleep(3)
        fork.create_branch_ref(fix_typo_branch, ref.object.sha)

        modified_readme = re.sub(r'\b%s\b' % typo, suggested, readme)
        fork.readme().update('Fix typo', branch=fix_typo_branch, content=modified_readme.encode('utf-8'))

        # open pull request
        repository.create_pull(title='Fix readme typo', base=fork.default_branch,
                               head='erjanmx:{}'.format(fix_typo_branch))
    finally:
        fork.delete()


def send_next_word(bot, message_id=None):
    global last, repo_gen
    key_markup = None

    try:
        repository, typo, suggested, readme = next(repo_gen)

        typo_pos = readme.find(typo)
        context_end_pos = typo_pos + 100
        context_start_pos = typo_pos - 100 if typo_pos - 100 > 0 else 0

        context = readme[context_start_pos:context_end_pos] \
            .replace(typo, '__{}__'.format(typo))

        key_markup = reply_markup
        text = 'https://github.com/{}\n\n{} - {}\n\n{}'.format(repository.full_name, typo, suggested, context)
    except TypeError:
        text = 'Session has expired'
    except (StopIteration, NotFoundError) as e:
        logging.error(e)
        text = 'You have reviewed all repositories for the date'

    if message_id is None:
        bot.send_message(chat_id=TELEGRAM_USER_ID, text=text, reply_markup=key_markup, disable_web_page_preview=True)
    else:
        bot.edit_message_text(chat_id=TELEGRAM_USER_ID, message_id=message_id, text=text, reply_markup=key_markup,
                              disable_web_page_preview=True)


def callback_action(bot, update):
    global skip_repo, approve_typo, ignore_word

    query = update.callback_query

    q_data = query.data

    if q_data == 'skip-repo':
        # skip the current repo
        skip_repo = True
    elif q_data == 'ignore':
        # add this word to ignore list
        ignore_word = True
    elif q_data == 'approve':
        approve_typo = True

    bot.answer_callback_query(callback_query_id=query.id, text=q_data)
    send_next_word(bot, message_id=query.message.message_id)


def start(bot, update):
    global repo_gen
    date = datetime.datetime.now() - datetime.timedelta(days=7)
    repo_gen = get_a_repo(date.strftime('%Y-%m-%d'))
    send_next_word(bot)


def stop(bot, update):
    update.message.reply_text('ok')
    os._exit(1)


def for_date(bot, update, groups):
    global repo_gen
    date = groups[0]
    repo_gen = get_a_repo(date)
    send_next_word(bot)


def error(bot, update, error):
    logger.warning('Update "%s" caused error "%s"', update, error)


def poll():
    updater = Updater(TELEGRAM_TOKEN)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start, filters=Filters.user(user_id=TELEGRAM_USER_ID)))
    dp.add_handler(CommandHandler("stop", stop, filters=Filters.user(user_id=TELEGRAM_USER_ID)))
    dp.add_handler(RegexHandler('([\d]{4}-[\d]{2}-[\d]{2})', for_date, pass_groups=True))

    dp.add_handler(CallbackQueryHandler(callback_action))

    dp.add_error_handler(error)

    updater.start_polling()
    updater.idle()


def add_to_ignore_list(word):
    logger.info('Ignoring word "%s"', word)
    db.insert({'word': word.lower()})


def add_to_approved_list(full_name, typo, suggested):
    logger.info('Adding to approved list "%s"', full_name)
    db.insert({'repo': full_name, 'typo': typo, 'suggested': suggested})


def main():
    # start telegram bot
    poll()


if __name__ == '__main__':
    main()
