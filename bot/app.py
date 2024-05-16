import io
import logging
import pymongo
import asyncio
import traceback
import html
import json
import tempfile
import pydub
from pathlib import Path
from datetime import datetime, timedelta

import openai

import telegram
from telegram import Update, User, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    JobQueue,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode, ChatAction

import cryptomus

import base64

from bot import config
from bot.config import mxp, strings
from bot import database
from bot import openai_utils
from bot.payment import CryptomusPayment


# setup
db = database.Database()
logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}


# --- Utility Functions --- #
def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def register_user(update: Update, context: CallbackContext, user: User):
    is_new_user = False
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            initial_token_balance=config.initial_token_balance,
            username=user.username,
            first_name=user.first_name,
            last_name= user.last_name
        )
        db.start_new_dialog(user.id)

        is_new_user = True

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    # token balance
    if not db.check_if_user_attribute_exists(user.id, "token_balance"):
        db.set_user_attribute(user.id, "token_balance", config.initial_token_balance)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    # back compatibility for n_used_tokens field
    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int):  # old format
        new_n_used_tokens = {
            "gpt-3.5-turbo": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    # image generation
    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)

    # voice message transcription
    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    # lang
    if db.get_user_attribute(user.id, "lang") is None:
        db.set_user_attribute(user.id, "lang", config.default_lang)

    # invites
    if db.get_user_attribute(user.id, "invites") is None:
        db.set_user_attribute(user.id, "invites", [])

    # mxp
    user_dict = db.user_collection.find_one({"_id": user.id})
    mxp.people_set(user.id, user_dict)

    return is_new_user


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)

    if user_semaphores[user_id].locked():
        text = strings["previous_message_is_not_answered_yet"][lang]
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def is_bot_mentioned(update: Update, context: CallbackContext):
    try:
        message = update.message

        if message.chat.type == "private":
            return True

        if message.text is not None and config.bot_username in message.text:
            return True

        if message.reply_to_message is not None:
            if message.reply_to_message.from_user.id == context.bot.id:
                return True
    except:
        return True
    else:
        return False


async def check_if_user_has_enough_tokens(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id

    token_balance = db.get_user_attribute(user_id, "token_balance")
    if token_balance <= 0:
        await show_balance_handle(update, context)
        return False

    return True


async def send_user_message_about_n_added_tokens(context: CallbackContext, n_tokens_added: int, chat_id: int = None, user_id: int = None):
    if chat_id is None:
        if user_id is None:
            raise ValueError(f"chat_id and user_id can't be None simultaneously")
        chat_id = db.get_user_attribute(user_id, "chat_id")

    lang = db.get_user_lang(user_id)
    text = strings["n_tokens_added"][lang].format(n_tokens_added=n_tokens_added)
    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def notify_admins_about_successfull_payment(context: CallbackContext, payment_id: int):
    if config.admin_chat_id is not None:
        text = "🟣 Successfull payment:\n"

        payment_dict = db.payment_collection.find_one({"_id": payment_id})
        for key in ["amount", "currency", "product_key", "n_tokens_to_add", "payment_method", "user_id", "status"]:
            text += f"- {key}: <b>{payment_dict[key]}</b>\n"

        user_dict = db.user_collection.find_one({"_id": payment_dict["user_id"]})
        if user_dict["username"] is not None:
            text += f"- username: @{user_dict['username']}\n"

        # tag admins
        for admin_username in config.admin_usernames:
            if not admin_username.startswith("@"):
                admin_username = "@" + admin_username
            text += f"\n{admin_username}"

        await context.bot.send_message(config.admin_chat_id, text, parse_mode=ParseMode.HTML)


async def check_payment_status_job_fn(context: CallbackContext):
    job = context.job
    payment_id = job.data["payment_id"]
    payment_dict = db.payment_collection.find_one({"_id": payment_id})

    is_paid = False
    if payment_dict["payment_method_type"] == "cryptomus":
        cryptomus_payment_instance = CryptomusPayment(
            config.payment_methods["cryptomus"]["api_key"],
            config.payment_methods["cryptomus"]["merchant_id"]
        )

        is_paid = cryptomus_payment_instance.check_invoice_status(payment_id)
    else:
        context.job.schedule_removal()
        return

    if is_paid:
        db.set_payment_attribute(payment_id, "status", "paid")
        user_id = payment_dict["user_id"]

        n_tokens_to_add = payment_dict["n_tokens_to_add"]
        if not payment_dict["are_tokens_added"]:
            db.set_user_attribute(
                user_id,
                "token_balance",
                db.get_user_attribute(user_id, "token_balance") + n_tokens_to_add
            )
            db.set_payment_attribute(payment_id, "are_tokens_added", True)

            await send_user_message_about_n_added_tokens(context, n_tokens_to_add, chat_id=job.chat_id, user_id=user_id)
            await notify_admins_about_successfull_payment(context, payment_id)

            # mxp
            payment_dict = db.payment_collection.find_one({"_id": payment_id})

            product = config.products[payment_dict["product_key"]]
            amount = product["price"]
            if product["currency"] == "RUB":
                amount /= 77

            distinct_id, event_name, properties = (
                user_id,
                "successful_payment",
                {
                    "token_balance": db.get_user_attribute(user_id, "token_balance"),
                    "payment_method": payment_dict["payment_method"],
                    "product": payment_dict["product_key"],
                    "payment_id": payment_id,
                    "amount": amount
                }
            )
            mxp.track(distinct_id, event_name, properties)

        context.job.schedule_removal()


def run_repeating_payment_status_check(job_queue: JobQueue, payment_id: int, chat_id: int, how_long: int = 4000, interval: int = 30):
    job_queue.run_repeating(
        check_payment_status_job_fn,
        interval,
        first=0,
        last=how_long,
        name=str(payment_id),
        data={"payment_id": payment_id},
        chat_id=chat_id
    )


def run_not_expired_payment_status_check(job_queue: JobQueue, how_long: int = 4000, interval: int = 30):
    payment_ids = db.get_all_not_expried_payment_ids()
    for payment_id in payment_ids:
        user_id = db.get_payment_attribute(payment_id, "user_id")
        chat_id = db.get_user_attribute(user_id, "chat_id")
        run_repeating_payment_status_check(job_queue, payment_id, chat_id, how_long, interval)


def convert_text_tokens_to_bot_tokens(model: str, n_input_tokens: int, n_output_tokens: int):
    """
    Text token – LLM token
    Bot token – currency inside bot (1 bot token price == 1 gpt-3.5-turbo token price)
    """
    baseline_price_per_1000_tokens = config.models["info"]["gpt-3.5-turbo"]["price_per_1000_input_tokens"]

    n_bot_input_tokens = int(n_input_tokens * (config.models["info"][model]["price_per_1000_input_tokens"] / baseline_price_per_1000_tokens))
    n_bot_output_tokens = int(n_output_tokens * (config.models["info"][model]["price_per_1000_output_tokens"] / baseline_price_per_1000_tokens))

    return n_bot_input_tokens + n_bot_output_tokens


def convert_generated_images_to_bot_tokens(model: str, n_generated_images: int):
    baseline_price_per_1000_tokens = config.models["info"]["gpt-3.5-turbo"]["price_per_1000_input_tokens"]

    n_spent_dollars = n_generated_images * (config.models["info"][model]["price_per_1_image"])
    n_bot_tokens = int(n_spent_dollars / (baseline_price_per_1000_tokens / 1000))

    return n_bot_tokens


def convert_transcribed_seconds_to_bot_tokens(model: str, n_transcribed_seconds: float):
    baseline_price_per_1000_tokens = config.models["info"]["gpt-3.5-turbo"]["price_per_1000_input_tokens"]

    n_spent_dollars = n_transcribed_seconds * (config.models["info"][model]["price_per_1_min"] / 60)
    n_bot_tokens = int(n_spent_dollars / (baseline_price_per_1000_tokens / 1000))

    return n_bot_tokens


def parse_deeplink_parameters(s):
    try:
        if len(s) == 0:
            return {}

        deeplink_parameters = {}
        for key_value in s.split("-"):
            key, value = key_value.split("=")
            deeplink_parameters[key] = value

        return deeplink_parameters
    except:
        print(f"Wrong deeplink parameters: {s}")
        return {}



# --- System Handles --- #
async def start_handle(update: Update, context: CallbackContext):
    is_new_user = await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)

    # deeplink parameters
    argv = update.message.text.split(" ")
    if len(argv) > 1:
        # example: https://t.me/chatgpt_karfly_bot?start=lang=en-source=durov-ref=karfly
        deeplink_parameters = parse_deeplink_parameters(argv[1])

        if "lang" in deeplink_parameters:
            db.set_user_attribute(user_id, "lang", deeplink_parameters["lang"])


            if "source" in deeplink_parameters:
                if db.get_user_attribute(user_id, "deeplink_source") is None:
                    db.set_user_attribute(user_id, "deeplink_source", deeplink_parameters["source"])

        if "ref" in deeplink_parameters and is_new_user:
            ref_user_id = int(deeplink_parameters["ref"])

            # set ref for new user
            if db.get_user_attribute(user_id, "ref") is None:
                db.set_user_attribute(user_id, "ref", ref_user_id)

            if db.check_if_user_exists(ref_user_id):
                ref_user_invites = db.get_user_attribute(ref_user_id, "invites")
                if user_id not in ref_user_invites and len(ref_user_invites) < config.max_invites_per_user:
                    # add tokens to ref user
                    db.set_user_attribute(ref_user_id, "token_balance", db.get_user_attribute(ref_user_id, "token_balance") + config.n_tokens_to_add_to_ref)

                    # save in database
                    payment_id = db.get_new_unique_payment_id()
                    db.add_new_payment(
                        payment_id=payment_id,
                        payment_method="add_tokens_for_ref",
                        payment_method_type="add_tokens_for_ref",
                        product_key="add_tokens_for_ref",
                        user_id=ref_user_id,
                        amount=0.0,
                        currency=None,
                        status="not_paid",  # don't give paid features to users for invited users
                        invoice_url="",
                        expired_at=datetime.now(),
                        n_tokens_to_add=config.n_tokens_to_add_to_ref
                    )

                    # update invites
                    db.set_user_attribute(ref_user_id, "invites", db.get_user_attribute(ref_user_id, "invites") + [user_id])

                    # send message to ref user
                    ref_chat_id = db.get_user_attribute(ref_user_id, "chat_id")
                    ref_user_lang = db.get_user_lang(ref_user_id)

                    text = strings["your_friend_joined"][ref_user_lang].format(friend_user_id=user_id)
                    await context.bot.send_message(ref_chat_id, text, parse_mode=ParseMode.HTML)

                    await send_user_message_about_n_added_tokens(context, config.n_tokens_to_add_to_ref, chat_id=ref_chat_id, user_id=ref_user_id)

                    # send message to admins
                    if config.admin_chat_id is not None:
                        text = f'🟣 User <a href="tg://user?id={ref_user_id}">{ref_user_id}</a> invited <a href="tg://user?id={user_id}">{user_id}</a>. <b>{config.n_tokens_to_add_to_ref}</b> tokens were successfully added to his balance!'
                        await context.bot.send_message(config.admin_chat_id, text, parse_mode=ParseMode.HTML)

                    # mxp
                    distinct_id, event_name, properties = (
                        ref_user_id,
                        "user_joined_via_invite",
                        {
                            "invited_user_id": user_id
                        }
                    )
                    mxp.track(distinct_id, event_name, properties)

        # mxp
        user_dict = db.user_collection.find_one({"_id": user_id})
        mxp.people_set(user_id, user_dict)

    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.start_new_dialog(user_id)

    if is_new_user:
        await send_welcome_message_to_new_user(update, context)
    else:
        text = f"{strings['hello'][lang]}\n\n{strings['help'][lang].format(support_username=config.support_username)}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

        await asyncio.sleep(2.0)
        await show_chat_modes_handle(update, context)

    # mxp
    distinct_id, event_name, properties = (user_id, "start", {})
    mxp.track(distinct_id, event_name, properties)


async def send_welcome_message_to_new_user(update: Update, context: CallbackContext):
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)

    for message_key in ["welcome_message_1", "welcome_message_2", "welcome_message_3"]:
        placeholder_message = await update.message.reply_text("...")
        await asyncio.sleep(1.0)

        text = ""
        for i, text_chunk in enumerate(strings[message_key][lang]):
            text += text_chunk + "\n"

            # skip empty line
            if len(text_chunk) == 0:
                continue

            current_text = text
            if i != len(strings[message_key][lang]) - 1:
                current_text += "..."

            await context.bot.edit_message_text(
                current_text,
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )
            delay = min(3.5, max(1.0, 0.025 * len(text_chunk)))
            await asyncio.sleep(delay)

    await show_chat_modes_handle(update, context)


async def help_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)

    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = strings["help"][lang].format(support_username=config.support_username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)

    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = strings["help_group_chat"][lang].format(bot_username=config.bot_username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    await update.message.reply_video(config.help_group_chat_video_path)

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "help_group_chat",
        {}
    )
    mxp.track(distinct_id, event_name, properties)


# --- Message Handles --- #
async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    # check if message is edited
    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    _message = message or update.message.text

    # remove bot mention (in group chats)
    if update.message.chat.type != "private":
        _message = _message.replace(config.bot_username, "").strip()

    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return
    if not await check_if_user_has_enough_tokens(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return
        
    current_model = db.get_user_attribute(user_id, "current_model")

    async def message_handle_fn():
        # new dialog timeout
        if use_new_dialog_timeout:
            if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                text = strings["starting_new_dialog_due_to_timeout"][lang].format(chat_mode_name=config.chat_modes[chat_mode]["name"][lang])
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # in case of CancelledError
        n_input_tokens, n_output_tokens = 0, 0
        current_model = db.get_user_attribute(user_id, "current_model")

        try:
            # send typing action
            await update.message.chat.send_action(action="typing")
            if _message is None or len(_message) == 0:
                text = strings["empty_message"][lang]
                await update.message.reply_text(text, parse_mode=ParseMode.HTML)
                return

            # send placeholder message to user
            placeholder_message = await update.message.reply_text("⏳")

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)
            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages, chat_mode=chat_mode)
            else:
                answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            # send message to user
            prev_answer = ""
            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                if status != "finished":
                    answer = answer + "..."
                answer = answer[:4096]  # telegram message limit

                # update only when 100 new symbols are ready
                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id, parse_mode=parse_mode)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue
                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id, message_id=placeholder_message.message_id)

                await asyncio.sleep(0.01)  # wait a bit to avoid flooding

                prev_answer = answer

            # update user data
            new_dialog_message = {"user": [{"type": "text", "text": _message}], "bot": answer, "date": datetime.now()}
            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

            n_used_bot_tokens = convert_text_tokens_to_bot_tokens(current_model, n_input_tokens, n_output_tokens)
            db.set_user_attribute(user_id, "token_balance", max(0, db.get_user_attribute(user_id, "token_balance") - n_used_bot_tokens))

            # mxp
            distinct_id, event_name, properties = (
                user_id,
                "send_message",
                {
                    "dialog_id": db.get_user_attribute(user_id, "current_dialog_id"),
                    "chat_mode": chat_mode,
                    "model": current_model,
                    "n_used_bot_tokens": n_used_bot_tokens
                }
            )
            mxp.track(distinct_id, event_name, properties)

        except asyncio.CancelledError:
            # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            n_used_bot_tokens = convert_text_tokens_to_bot_tokens(current_model, n_input_tokens, n_output_tokens)
            db.set_user_attribute(user_id, "token_balance", max(0, db.get_user_attribute(user_id, "token_balance") - n_used_bot_tokens))
            raise

        # send message if some messages were removed from the context
        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = strings["dialog_is_too_long_first_message"][lang]
            else:
                text = strings["dialog_is_too_long"][lang].format(n_first_dialog_messages_removed=n_first_dialog_messages_removed)
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        if current_model == "gpt-4-vision-preview" or update.message.photo is not None and len(update.message.photo) > 0:
            logger.error('gpt-4-vision-preview')
            if current_model != "gpt-4-vision-preview":
                current_model = "gpt-4-vision-preview"
                db.set_user_attribute(user_id, "current_model", "gpt-4-vision-preview")
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                message_handle_fn()
            )
            
        user_tasks[user_id] = task

        try:
           await task
        except asyncio.CancelledError:
           text = strings["canceled"][lang]
           await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        else:
           pass
        finally:
           if user_id in user_tasks:
               del user_tasks[user_id]


async def edited_message_handle(update: Update, context: CallbackContext):
    user_id = update.edited_message.from_user.id
    lang = db.get_user_lang(user_id)

    text = strings["edited_message"][lang]
    await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def retry_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        text = strings["no_message_to_retry"][lang]
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)  # last message was removed from the context

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)
    
    
async def _vision_message_handle_fn(
    update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True
):
    logger.info('_vision_message_handle_fn')
    user_id = update.message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model != "gpt-4-vision-preview":
        await update.message.reply_text(
            "🥲 Images processing is only available for <b>gpt-4-vision-preview</b> model. Please change your settings in /settings",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    # new dialog timeout
    if use_new_dialog_timeout:
        if (datetime.now() - db.get_user_attribute(user_id, "last_interaction")).seconds > config.new_dialog_timeout and len(db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)
            await update.message.reply_text(f"Starting new dialog due to timeout (<b>{config.chat_modes[chat_mode]['name']}</b> mode) ✅", parse_mode=ParseMode.HTML)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    buf = None
    if update.message.effective_attachment:
        photo = update.message.effective_attachment[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        # store file in memory, not on disk
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"  # file extension is required
        buf.seek(0)  # move cursor to the beginning of the buffer

    # in case of CancelledError
    n_input_tokens, n_output_tokens = 0, 0

    try:
        # send placeholder message to user
        placeholder_message = await update.message.reply_text("...")
        message = update.message.caption or update.message.text or ''

        # send typing action
        await update.message.chat.send_action(action="typing")

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        parse_mode = {"html": ParseMode.HTML, "markdown": ParseMode.MARKDOWN}[
            config.chat_modes[chat_mode]["parse_mode"]
        ]

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        if config.enable_message_streaming:
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (
                    n_input_tokens,
                    n_output_tokens,
                ), n_first_dialog_messages_removed

            gen = fake_gen()

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]  # telegram message limit

            # update only when 100 new symbols are ready
            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)  # wait a bit to avoid flooding

            prev_answer = answer

        # update user data
        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {"user": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image",
                            "image": base_image,
                        }
                    ]
                , "bot": answer, "date": datetime.now()}
        else:
            new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer, "date": datetime.now()}
        
        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

    except asyncio.CancelledError:
        # note: intermediate token updates only work when enable_message_streaming=True (config.yml)
        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        error_text = f"Something went wrong during completion. Reason: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return

async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):
    error_text = f"I don't know how to read files or videos. Send the picture in normal mode (Quick Mode)."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    db.set_user_attribute(user_id, "current_model", "gpt-3.5-turbo")

    db.start_new_dialog(user_id)
    text = strings["new_dialog"][lang]
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
    current_model = db.get_user_attribute(user_id, "current_model")

    text = ""
    if config.chat_modes[chat_mode]["model_type"] == "text":
        text += f"<i>{config.models['info'][current_model]['name']}</i>: "

    text += config.chat_modes[chat_mode]["welcome_message"][lang]

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        text = strings["nothing_to_cancel"][lang]
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # check if user has enough tokens
    if not await check_if_user_has_enough_tokens(update, context):
        return

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    try:
        image_urls = await openai_utils.generate_images(message, n_images=config.return_n_generated_images, size=config.image_size)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = strings["request_doesnt_comply"][lang]
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            raise

    # token usage
    db.set_user_attribute(user_id, "n_generated_images", config.return_n_generated_images + db.get_user_attribute(user_id, "n_generated_images"))

    n_used_bot_tokens = convert_generated_images_to_bot_tokens("dalle-2", config.return_n_generated_images)
    db.set_user_attribute(user_id, "token_balance", max(0, db.get_user_attribute(user_id, "token_balance") - n_used_bot_tokens))

    for i, image_url in enumerate(image_urls):
        await update.message.chat.send_action(action="upload_photo")

        text = f"<i>{message}</i>"
        if len(image_urls) > 1:
            text += f" {i + 1}/{len(image_urls)}"

        text += "\n"
        text += strings["image_created_with"][lang].format(bot_username=config.bot_username)

        await update.message.reply_photo(image_url, caption=text, parse_mode=ParseMode.HTML)

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "generate_image",
        {"prompt": message, "n_used_bot_tokens": n_used_bot_tokens}
    )
    mxp.track(distinct_id, event_name, properties)


async def voice_message_handle(update: Update, context: CallbackContext):
    # check if bot was mentioned (for group chats)
    if not await is_bot_mentioned(update, context):
        return

    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    # check if user has enough tokens
    if not await check_if_user_has_enough_tokens(update, context):
        return

    voice = update.message.voice
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_dir = Path(tmp_dir)
        voice_ogg_path = tmp_dir / "voice.ogg"

        # download
        voice_file = await context.bot.get_file(voice.file_id)
        await voice_file.download_to_drive(voice_ogg_path)

        # convert to mp3
        voice_mp3_path = tmp_dir / "voice.mp3"
        pydub.AudioSegment.from_file(voice_ogg_path).export(voice_mp3_path, format="mp3")

        if voice.duration > 3 * 60:
            text = strings["voice_message_is_too_long"][lang]
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return

        # transcribe
        with open(voice_mp3_path, "rb") as f:
            transcribed_text = await openai_utils.transcribe_audio(f)

            if transcribed_text is None:
                transcribed_text = ""

    text = f"🎤: <i>{transcribed_text}</i>"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    # token usage
    db.set_user_attribute(user_id, "n_transcribed_seconds", voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))

    n_used_bot_tokens = convert_transcribed_seconds_to_bot_tokens("whisper", voice.duration)
    db.set_user_attribute(user_id, "token_balance", max(0, db.get_user_attribute(user_id, "token_balance") - n_used_bot_tokens))

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "send_voice_message",
        {"n_used_bot_tokens": n_used_bot_tokens}
    )
    mxp.track(distinct_id, event_name, properties)

    await message_handle(update, context, message=transcribed_text)



# --- Chat Mode Handles --- #
def get_chat_mode_menu(page_index: int, lang: str = "en"):
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = strings["select_chat_mode"][lang].format(n_chat_modes=len(config.chat_modes))

    # buttons
    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    for chat_mode_key in page_chat_mode_keys:
        name = config.chat_modes[chat_mode_key]["name"][lang]
        if "is_pro" in config.chat_modes[chat_mode_key] and (config.chat_modes[chat_mode_key]["is_pro"] == True):
            name += " [PRO]"
        keyboard.append([InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}")])

    # pagination
    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        if is_first_page:
            keyboard.append([
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])
        elif is_last_page:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
            ])
        else:
            keyboard.append([
                InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"),
                InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
            ])

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0, lang=lang)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    if await is_previous_message_not_answered_yet(update.callback_query, context): return

    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    query = update.callback_query
    await query.answer()

    page_index = int(query.data.split("|")[1])
    if page_index < 0:
        return

    text, reply_markup = get_chat_mode_menu(page_index, lang=lang)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "select_chat_mode",
        {"chat_mode": chat_mode}
    )
    mxp.track(distinct_id, event_name, properties)

    # is redirect to chatgpt_plus?
    if "type" in config.chat_modes[chat_mode] and config.chat_modes[chat_mode]["type"] == "redirect_to_chatgpt_plus":
        text = config.strings["redirect_to_chatgpt_plus"][lang]
        await context.bot.send_message(
            update.callback_query.message.chat.id,
            text,
            parse_mode=ParseMode.HTML
        )
        return

    # is pro?
    is_pro = ("is_pro" in config.chat_modes[chat_mode]) and (config.chat_modes[chat_mode]["is_pro"] == True)
    if is_pro and not db.does_user_have_successful_payment(user_id):
        text = config.strings["pro_chat_mode"][lang].format(chat_mode_name=config.chat_modes[chat_mode]["name"][lang])
        await context.bot.send_message(
            update.callback_query.message.chat.id,
            text,
            parse_mode=ParseMode.HTML
        )

        await asyncio.sleep(3.0)
        await show_payment_methods_handle(update, context)
        return

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    try:
        current_model = db.get_user_attribute(user_id, "current_model")

        text = ""
        if config.chat_modes[chat_mode]["model_type"] == "text":
            text += f"<i>{config.models['info'][current_model]['name']}</i>: "
        text += config.chat_modes[chat_mode]["welcome_message"][lang]

        # await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        await context.bot.send_message(
            update.callback_query.message.chat.id,
            text,
            parse_mode=ParseMode.HTML
        )
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "set_chat_mode",
        {"chat_mode": chat_mode}
    )
    mxp.track(distinct_id, event_name, properties)



# --- Settings Handles ---
def get_settings_menu(user_id: int):
    lang = db.get_user_lang(user_id)
    current_model = db.get_user_attribute(user_id, "current_model")
    text = config.models["info"][current_model]["description"][lang]

    text += "\n\n"
    scores = config.models["info"][current_model]["scores"]
    for score_dict in scores:
        text += "🟢" * score_dict["score"] + "⚪️" * (5 - score_dict["score"]) + f" – {score_dict['title'][lang]}\n\n"

    text += strings["select_model"][lang]

    # buttons to choose models
    buttons = []
    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        buttons.append(
            InlineKeyboardButton(title, callback_data=f"set_settings|{model_key}")
        )
    reply_markup = InlineKeyboardMarkup([buttons])

    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")

    # is pro?
    is_pro = ("is_pro" in config.models["info"][model_key]) and (config.models["info"][model_key]["is_pro"] == True)
    if is_pro and not db.does_user_have_successful_payment(user_id):
        text = config.strings["pro_model"][lang].format(model_name=config.models["info"][model_key]["name"])
        await context.bot.send_message(
            update.callback_query.message.chat.id,
            text,
            parse_mode=ParseMode.HTML
        )

        await asyncio.sleep(3.0)
        await show_payment_methods_handle(update, context)
        return

    db.set_user_attribute(user_id, "current_model", model_key)
    db.start_new_dialog(user_id)

    text, reply_markup = get_settings_menu(user_id)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "set_settings",
        {"model": model_key}
    )
    mxp.track(distinct_id, event_name, properties)



# --- Payment Handles --- #
async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    lang = db.get_user_lang(user_id)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    token_balance = max(0, db.get_user_attribute(user_id, "token_balance"))
    if token_balance > 0:
        text = strings["you_have_have_n_tokens_left"][lang].format(token_balance=token_balance)
    else:
        text = strings["you_have_have_no_tokens_left"][lang]

    text += "\n"

    # total token usage
    total_n_used_bot_tokens = 0
    for model_key, model_values in db.get_user_attribute(user_id, "n_used_tokens").items():
        total_n_used_bot_tokens += convert_text_tokens_to_bot_tokens(model_key, model_values["n_input_tokens"], model_values["n_output_tokens"])

    # voice messages
    voice_recognition_n_used_bot_tokens = convert_transcribed_seconds_to_bot_tokens("whisper", db.get_user_attribute(user_id, "n_transcribed_seconds"))
    total_n_used_bot_tokens += voice_recognition_n_used_bot_tokens

    # image generation
    image_generation_n_used_bot_tokens = convert_generated_images_to_bot_tokens("dalle-2", db.get_user_attribute(user_id, "n_generated_images"))
    total_n_used_bot_tokens += image_generation_n_used_bot_tokens

    text += strings["you_totally_spent_n_tokens"][lang].format(total_n_used_bot_tokens=total_n_used_bot_tokens)

    if token_balance <= 0:
        text += "\n\n"
        text += strings["to_continue_using_the_bot"][lang]

    buttons = []
    buttons.append([InlineKeyboardButton(strings["get_tokens_button"][lang], callback_data=f"show_payment_methods")])
    if config.enable_ref_system:
        buttons.append([InlineKeyboardButton(strings["invite_friend_button"][lang], callback_data=f"invite_friend")])
    reply_markup = InlineKeyboardMarkup(buttons)

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "show_balance",
        {"token_balance": token_balance}
    )
    mxp.track(distinct_id, event_name, properties)


async def show_payment_methods_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    text = strings["choose_payment_method"][lang]

    buttons = []
    for payment_method_key, payment_method_values in config.payment_methods.items():
        button = InlineKeyboardButton(
            payment_method_values["name"][lang],
            callback_data=f"show_products|{payment_method_key}"
        )
        buttons.append([button])
    reply_markup = InlineKeyboardMarkup(buttons)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "show_payment_methods",
        {"token_balance": db.get_user_attribute(user_id, "token_balance")}
    )
    mxp.track(distinct_id, event_name, properties)


async def show_products_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    _, payment_method_key = query.data.split("|")

    text = strings["choose_product"][lang]

    product_keys = config.payment_methods[payment_method_key]["product_keys"]
    buttons = []
    for product_key in product_keys:
        product = config.products[product_key]
        button = InlineKeyboardButton(
            product["title_on_button"],
            callback_data=f"send_invoice|{payment_method_key}|{product_key}"
        )
        buttons.append([button])
    reply_markup = InlineKeyboardMarkup(buttons)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML
    )

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "show_products",
        {
            "token_balance": db.get_user_attribute(user_id, "token_balance"),
            "payment_method": payment_method_key
        }
    )
    mxp.track(distinct_id, event_name, properties)


async def invite_friend_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    text = strings["invite_friend"][lang].format(
        n_tokens_to_add_to_ref=config.n_tokens_to_add_to_ref,
        max_invites_per_user=config.max_invites_per_user,
        n_already_invited_users=len(db.get_user_attribute(user_id, "invites"))
    )
    await context.bot.send_message(update.callback_query.message.chat.id, text, parse_mode=ParseMode.HTML)

    bot_username_without_at = config.bot_username[1:]  # t.me/@... doesn't work sometimes
    invite_url = f"https://t.me/{bot_username_without_at}?start=ref={user_id}"
    text = strings["invite_message"][lang].format(invite_url=invite_url, bot_name=config.bot_name)
    await context.bot.send_message(update.callback_query.message.chat.id, text, parse_mode=ParseMode.HTML)

    # mxp
    distinct_id, event_name, properties = (
        user_id,
        "invite_friend",
        {
            "token_balance": db.get_user_attribute(user_id, "token_balance"),
        }
    )
    mxp.track(distinct_id, event_name, properties)


async def send_invoice_handle(update: Update, context: CallbackContext):
    await register_user(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id
    lang = db.get_user_lang(user_id)

    query = update.callback_query
    await query.answer()

    _, payment_method_key, product_key = query.data.split("|")
    product = config.products[product_key]
    payment_method_type = config.payment_methods[payment_method_key]["type"]

    payment_id = db.get_new_unique_payment_id()

    if payment_method_type == "telegram_payments":
        chat_id = update.callback_query.message.chat.id

        # save in database
        db.add_new_payment(
            payment_id=payment_id,
            payment_method=payment_method_key,
            payment_method_type=payment_method_type,
            product_key=product_key,
            user_id=user_id,
            amount=product["price"],
            currency=product["currency"],
            status="not_paid",
            invoice_url="",
            expired_at=datetime.now() + timedelta(hours=1),
            n_tokens_to_add=product["n_tokens_to_add"]
        )

        # create invoice
        payload = f"{payment_id}"
        prices = [LabeledPrice(product["title"], int(product["price"] * 100))]

        photo_url = None
        if "photo_url" in product and len(product["photo_url"]) > 0:
            photo_url = product["photo_url"]

        # send invoice
        await context.bot.send_invoice(
            chat_id,
            product["title"],
            product["description"],
            payload,
            config.payment_methods[payment_method_key]["token"],
            product["currency"],
            prices,
            photo_url=photo_url
        )
    elif payment_method_type == "cryptomus":
        # create invoice
        cryptomus_payment_instance = CryptomusPayment(
            config.payment_methods[payment_method_key]["api_key"],
            config.payment_methods[payment_method_key]["merchant_id"]
        )

        invoice_url, status, expired_at = cryptomus_payment_instance.create_invoice(
            payment_id,
            product["price"],
            product["currency"]
        )

        # save in database
        db.add_new_payment(
            payment_id=payment_id,
            payment_method=payment_method_key,
            payment_method_type=payment_method_type,
            product_key=product_key,
            user_id=user_id,
            amount=product["price"],
            currency=product["currency"],
            status=status,
            invoice_url=invoice_url,
            expired_at=expired_at,
            n_tokens_to_add=product["n_tokens_to_add"]
        )

        # run status check polling
        run_repeating_payment_status_check(context.job_queue, payment_id, update.callback_query.message.chat.id, how_long=14400, interval=60)

        # send invoice
        text = strings["invoice_cryptomus"][lang].format(
            invoice_url=invoice_url,
            n_tokens_to_add=product['n_tokens_to_add'],
            support_username=config.support_username
        )
        await context.bot.send_message(update.callback_query.message.chat.id, text, parse_mode=ParseMode.HTML)
    else:
        raise ValueError(f"Unknown payment method: {payment_method_type}")

    # mxp
    amount = product["price"]
    if product["currency"] == "RUB":  # convert RUB to USD
        amount /= 77.0

    distinct_id, event_name, properties = (
        user_id,
        "send_invoice",
        {
            "token_balance": db.get_user_attribute(user_id, "token_balance"),
            "payment_method": payment_method_key,
            "product": product_key,
            "payment_id": payment_id,
            "amount": amount
        }
    )
    mxp.track(distinct_id, event_name, properties)


async def pre_checkout_handle(update: Update, context: CallbackContext):
    await register_user(update.pre_checkout_query, context, update.pre_checkout_query.from_user)
    user_id = update.pre_checkout_query.from_user.id

    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    chat_id = db.get_user_attribute(user_id, "chat_id")

    payment_id = int(update.message.successful_payment.invoice_payload)
    payment_dict = db.payment_collection.find_one({"_id": payment_id})

    # update payment in database
    db.set_payment_attribute(payment_id, "status", "paid")

    n_tokens_to_add = payment_dict["n_tokens_to_add"]
    if not payment_dict["are_tokens_added"]:
        db.set_user_attribute(
            user_id,
            "token_balance",
            db.get_user_attribute(user_id, "token_balance") + n_tokens_to_add
        )
        db.set_payment_attribute(payment_id, "are_tokens_added", True)

        # send messages
        await send_user_message_about_n_added_tokens(context, n_tokens_to_add, chat_id=chat_id, user_id=user_id)
        await notify_admins_about_successfull_payment(context, payment_id)

    # mxp
    product = config.products[payment_dict["product_key"]]

    amount = product["price"]
    if product["currency"] == "RUB":
        amount /= 77

    distinct_id, event_name, properties = (
        user_id,
        "successful_payment",
        {
            "token_balance": db.get_user_attribute(user_id, "token_balance"),
            "payment_method": payment_dict["payment_method"],
            "product": payment_dict["product_key"],
            "payment_id": payment_id,
            "amount": amount
        }
    )
    mxp.track(distinct_id, event_name, properties)


# --- Admin Handles --- #
async def add_tokens_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)

    username_or_user_id, n_tokens_to_add = context.args
    n_tokens_to_add = int(n_tokens_to_add)

    try:
        user_id = int(username_or_user_id)
        user_dict = db.user_collection.find_one({"_id": user_id})
    except:
        username = username_or_user_id
        user_dict = db.user_collection.find_one({"username": username})

    if user_dict is None:
        text = f"Username or user_id <b>{username_or_user_id}</b> not found in DB"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    # add tokens
    db.set_user_attribute(user_dict["_id"], "token_balance", db.get_user_attribute(user_dict["_id"], "token_balance") + n_tokens_to_add)

    # save in database
    payment_id = db.get_new_unique_payment_id()
    db.add_new_payment(
        payment_id=payment_id,
        payment_method="add_tokens",
        payment_method_type="add_tokens",
        product_key="add_tokens",
        user_id=user_dict["_id"],
        amount=0.0,
        currency=None,
        status="paid",
        invoice_url="",
        expired_at=datetime.now(),
        n_tokens_to_add=n_tokens_to_add
    )

    # send message to user
    await send_user_message_about_n_added_tokens(context, n_tokens_to_add, chat_id=user_dict["chat_id"], user_id=user_dict["_id"])

    # send message to admin
    text = f"🟣 <b>{n_tokens_to_add}</b> tokens were successfully added to <b>{username_or_user_id}</b> balance!"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def user_info_handle(update: Update, context: CallbackContext):
    await register_user(update, context, update.message.from_user)
    user = update.message.from_user

    text = "User info:\n"
    text += f"- <b>user_id</b>: {user.id}\n"
    text += f"- <b>username</b>: {user.username}\n"
    text += f"- <b>chat_id</b>: {update.message.chat_id}\n"

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)
    if config.admin_chat_id is not None:
        await context.bot.send_message(config.admin_chat_id, text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    try:
        if update.message is not None:
            user_id = update.message.from_user.id
        else:
            user_id = update.callback_query.from_user.id
        lang = db.get_user_lang(user_id)

        text = strings["exception"][lang].format(support_username=config.support_username)
        await context.bot.send_message(update.effective_chat.id, text, parse_mode=ParseMode.HTML)

        if config.admin_chat_id is not None:
            # collect error message
            tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
            tb_string = "".join(tb_list)
            update_str = update.to_dict() if isinstance(update, Update) else str(update)
            message = (
                f"An exception was raised while handling an update\n"
                f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
                "</pre>\n\n"
                f"<pre>{html.escape(tb_string)}</pre>"
            )

            # split text into multiple messages due to 4096 character limit
            for message_chunk in split_text_into_chunks(message, 4096):
                try:
                    await context.bot.send_message(config.admin_chat_id, message_chunk, parse_mode=ParseMode.HTML)
                except telegram.error.BadRequest:
                    # answer has invalid characters, so we send it without parse_mode
                    await context.bot.send_message(config.admin_chat_id, message_chunk)
    except Exception as e:
        text = "🥲 Some error in error handler...\n"
        text += f"Please <b>try again later</b> or contact us: {config.support_username}"
        await context.bot.send_message(update.effective_chat.id, text)

        text = f"Some error in error handler. Reason: {e}"
        try:
            await context.bot.send_message(config.admin_chat_id, text, parse_mode=ParseMode.HTML)
        except:
            # answer has invalid characters, so we send it without parse_mode
            await context.bot.send_message(config.admin_chat_id, text)



def run_bot() -> None:
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    # check not expired payments
    run_not_expired_payment_status_check(application.job_queue, how_long=14400, interval=60)

    # add handlers
    if len(config.allowed_telegram_usernames) == 0:
        user_filter = filters.ALL
    else:
        user_filter = filters.User(username=config.allowed_telegram_usernames)

    if len(config.admin_usernames) == 0:
        raise ValueError("You must specify at least 1 admin username in config")
    admin_filter = filters.User(username=config.admin_usernames)

    # system
    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    # message
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    # chat mode
    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    # settings
    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))

    # payment
    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_payment_methods_handle, pattern="^show_payment_methods$"))
    application.add_handler(CallbackQueryHandler(show_products_handle, pattern="^show_products"))
    application.add_handler(CallbackQueryHandler(invite_friend_handle, pattern="^invite_friend"))
    application.add_handler(CallbackQueryHandler(send_invoice_handle, pattern="^send_invoice"))

    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handle))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT & user_filter, successful_payment_handle))

    # admin
    application.add_handler(CommandHandler("add_tokens", add_tokens_handle, filters=admin_filter))
    application.add_handler(CommandHandler("info", user_info_handle, filters=user_filter))
    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()

if __name__ == "__main__":

    run_app()
