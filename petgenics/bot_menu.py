from __future__ import annotations

import logging

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    MenuButtonCommands,
    ReplyKeyboardMarkup,
)
from telegram.error import TelegramError

logger = logging.getLogger(__name__)

SALESPERSON_BUTTONS = (
    "🛒 New order",
    "🧺 Cart",
    "🔁 Switch brand",
    "✅ Submit order",
    "↩ Back",
    "❌ Cancel order",
    "❓ Help",
)
ADMIN_BUTTONS = SALESPERSON_BUTTONS + ("🛠 Admin panel", "📊 Today's report")
ALL_BUTTON_LABELS = SALESPERSON_BUTTONS + ("🛠 Admin panel", "📊 Today's report")
MENU_ACTIONS = {
    "🛠 Admin panel": "admin",
    "📊 Today's report": "report_today",
    "🛒 New order": "new_order",
    "🧺 Cart": "cart",
    "🔁 Switch brand": "switch_brand",
    "✅ Submit order": "submit",
    "↩ Back": "back",
    "❌ Cancel order": "cancel",
    "❓ Help": "help",
}

DEFAULT_COMMANDS = (
    BotCommand("start", "Start the Petgenics bot"),
    BotCommand("order", "Create or resume an order"),
    BotCommand("neworder", "Start a new order"),
    BotCommand("cart", "Review the current cart"),
    BotCommand("switchbrand", "Switch the selected brand"),
    BotCommand("back", "Go back one order step"),
    BotCommand("remarks", "Add order remarks"),
    BotCommand("submit", "Submit the current order"),
    BotCommand("cancel", "Cancel the current draft"),
    BotCommand("help", "Show help and available actions"),
    BotCommand("myid", "Show your Telegram user ID"),
)
ADMIN_COMMANDS = DEFAULT_COMMANDS + (
    BotCommand("admin", "Open the admin panel"),
    BotCommand("adminreport", "Download an order date report"),
    BotCommand("adminorder", "Download one order's files"),
    BotCommand("adminpermission", "Set salesperson permissions"),
)


def reply_keyboard(is_admin: bool) -> ReplyKeyboardMarkup:
    labels = ADMIN_BUTTONS if is_admin else SALESPERSON_BUTTONS
    rows = [list(labels[index:index + 2]) for index in range(0, len(labels), 2)]
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Choose an option or type a product name",
    )


async def register_bot_menu(bot, admin_ids: tuple[int, ...]) -> None:
    try:
        await bot.set_my_commands(DEFAULT_COMMANDS, scope=BotCommandScopeDefault())
        for admin_id in admin_ids:
            try:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            except TelegramError as exc:
                logger.warning(
                    "Could not register Telegram commands for an admin chat (%s).",
                    type(exc).__name__,
                )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError as exc:
        logger.error(
            "Could not register the Telegram command menu (%s).",
            type(exc).__name__,
        )
