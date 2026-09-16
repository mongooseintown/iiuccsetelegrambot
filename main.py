"""
Production-ready Telegram Bot for Managing Course Join Requests.
Framework: aiogram 3
Storage: SQLite (aiosqlite) with WAL mode
"""

import os
import hmac
import logging
import asyncio
from typing import Optional, List

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
    ChatJoinRequest,
    ChatMemberUpdated,
    ContentType,
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

import db

# Load environment variables
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("telegram_course_bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOOTSTRAP_CODE = os.getenv("BOOTSTRAP_CODE", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/bot.db").strip()

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN is missing! Please configure BOT_TOKEN in environment variables.")

if not BOOTSTRAP_CODE:
    logger.warning("BOOTSTRAP_CODE is not set! Owner claim /claim command will be disabled until set.")

# --- Keyboard Builders ---

async def build_semesters_keyboard() -> InlineKeyboardMarkup:
    semesters = await db.get_semesters(active_only=True, db_path=DB_PATH)
    buttons = []
    for sem in semesters:
        buttons.append([
            InlineKeyboardButton(text=f"🎓 {sem['name']}", callback_data=f"sem:{sem['id']}")
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_courses_keyboard(semester_id: int, user_id: int) -> InlineKeyboardMarkup:
    courses = await db.get_courses_by_semester(semester_id, active_only=True, db_path=DB_PATH)
    buttons = []

    for c in courses:
        chat_id = c["chat_id"]
        status = await db.get_user_course_status(chat_id, user_id, db_path=DB_PATH)

        if status == "approved":
            # Already joined: non-reusable alert button
            btn = InlineKeyboardButton(
                text=f"✅ {c['code']} - {c['name']}",
                callback_data=f"status:approved:{c['id']}"
            )
        elif status == "pending":
            # Request pending: non-reusable alert button
            btn = InlineKeyboardButton(
                text=f"⏳ {c['code']} - {c['name']}",
                callback_data=f"status:pending:{c['id']}"
            )
        else:
            # Available: direct invite link with creates_join_request=True
            btn = InlineKeyboardButton(
                text=f"➕ {c['code']} - {c['name']}",
                url=c["invite_link"]
            )
        buttons.append([btn])

    # Control buttons
    buttons.append([
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"refresh:{semester_id}"),
        InlineKeyboardButton(text="📂 Change Semester", callback_data="change_semester")
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def refresh_student_menu(bot: Bot, user_id: int) -> None:
    """Helper to live-update the student's open menu in private chat."""
    try:
        student = await db.get_student(user_id, db_path=DB_PATH)
        if not student or not student["selected_semester_id"] or not student["last_menu_message_id"]:
            return

        sem = await db.get_semester_by_id(student["selected_semester_id"], db_path=DB_PATH)
        if not sem:
            return

        kb = await build_courses_keyboard(sem["id"], user_id)
        text = (
            f"📚 *Semester:* {sem['name']}\n\n"
            "Select a course to request joining:\n"
            "➕ = Available (Tap to request)\n"
            "⏳ = Request Pending\n"
            "✅ = Already Joined"
        )
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=student["last_menu_message_id"],
            text=text,
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.debug(f"Could not live-refresh student {user_id} menu: {e}")


# --- Bot Setup ---

dp = Dispatcher()


# --- Student Handlers ---

@dp.message(CommandStart(), F.chat.type == "private")
async def handle_start(message: Message):
    user = message.from_user
    if not user:
        return

    # Record or update student info
    await db.upsert_student(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
        db_path=DB_PATH
    )

    semesters = await db.get_semesters(active_only=True, db_path=DB_PATH)
    if not semesters:
        await message.answer(
            "👋 Welcome! Currently no semesters or courses are published.\n"
            "Please check back soon or contact your department admin."
        )
        return

    kb = await build_semesters_keyboard()
    sent = await message.answer(
        "👋 *Welcome to the Course Group Join Bot!*\n\n"
        "Please select your semester to view available courses:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    await db.set_student_menu_message(user.id, sent.message_id, db_path=DB_PATH)


@dp.callback_query(F.data.startswith("sem:"))
async def handle_select_semester(callback: CallbackQuery, bot: Bot):
    user = callback.from_user
    sem_id_str = callback.data.split(":")[1]
    if not sem_id_str.isdigit():
        await callback.answer("Invalid semester.", show_alert=True)
        return

    sem_id = int(sem_id_str)
    sem = await db.get_semester_by_id(sem_id, db_path=DB_PATH)
    if not sem:
        await callback.answer("Semester not found.", show_alert=True)
        return

    await db.set_student_semester(user.id, sem_id, db_path=DB_PATH)
    kb = await build_courses_keyboard(sem_id, user.id)

    text = (
        f"📚 *Semester:* {sem['name']}\n\n"
        "Select a course to request joining:\n"
        "➕ = Available (Tap to request)\n"
        "⏳ = Request Pending\n"
        "✅ = Already Joined"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
            await db.set_student_menu_message(user.id, callback.message.message_id, db_path=DB_PATH)
        except TelegramBadRequest:
            pass
    await callback.answer()


@dp.callback_query(F.data.startswith("refresh:"))
async def handle_refresh_courses(callback: CallbackQuery):
    user = callback.from_user
    sem_id_str = callback.data.split(":")[1]
    if not sem_id_str.isdigit():
        await callback.answer("Error refreshing.", show_alert=True)
        return

    sem_id = int(sem_id_str)
    sem = await db.get_semester_by_id(sem_id, db_path=DB_PATH)
    if not sem:
        await callback.answer("Semester not found.", show_alert=True)
        return

    kb = await build_courses_keyboard(sem_id, user.id)
    text = (
        f"📚 *Semester:* {sem['name']}\n\n"
        "Select a course to request joining:\n"
        "➕ = Available (Tap to request)\n"
        "⏳ = Request Pending\n"
        "✅ = Already Joined"
    )

    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            pass  # Message is already up to date
    await callback.answer("List refreshed!")


@dp.callback_query(F.data == "change_semester")
async def handle_change_semester(callback: CallbackQuery):
    kb = await build_semesters_keyboard()
    if callback.message:
        try:
            await callback.message.edit_text(
                "Please select your semester:",
                reply_markup=kb
            )
        except TelegramBadRequest:
            pass
    await callback.answer()


@dp.callback_query(F.data.startswith("status:pending:"))
async def handle_pending_click(callback: CallbackQuery):
    await callback.answer(
        "⏳ Your join request is currently pending admin review. Please wait for approval.",
        show_alert=True
    )


@dp.callback_query(F.data.startswith("status:approved:"))
async def handle_approved_click(callback: CallbackQuery):
    await callback.answer(
        "✅ You have already been approved and joined this group!",
        show_alert=True
    )


# --- Admin Claim & Privilege Commands ---

@dp.message(Command("claim"), F.chat.type == "private")
async def handle_claim(message: Message):
    user = message.from_user
    if not user:
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.answer("⚠️ Usage: `/claim SECRET_CODE`", parse_mode=ParseMode.MARKDOWN)
        return

    supplied_code = parts[1]

    if not BOOTSTRAP_CODE:
        await message.answer("⚠️ BOOTSTRAP_CODE is not configured on the server.")
        return

    # Check constant-time equality
    if not hmac.compare_digest(supplied_code, BOOTSTRAP_CODE):
        await message.answer("❌ Invalid secret code.")
        return

    # Check if admins exist
    if await db.has_admins(db_path=DB_PATH):
        await message.answer("⚠️ Bot ownership has already been claimed.")
        return

    success = await db.claim_admin(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
        db_path=DB_PATH
    )

    if success:
        logger.info(f"Admin claimed successfully by user_id {user.id}")
        await message.answer(
            "🎉 *Admin Claim Successful!*\n\n"
            "You are now registered as the Owner Admin.\n"
            "Type /adminhelp to see available administrative commands.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await message.answer("⚠️ Failed to claim admin privileges. Admin may already exist.")


@dp.message(Command("adminhelp"))
async def handle_admin_help(message: Message):
    if not await db.is_admin(message.from_user.id, db_path=DB_PATH):
        return

    help_text = (
        "🛠 *Admin Command Reference:*\n\n"
        "• `/addsemester <CODE> <Full Name>`\n"
        "  _Create or update semester (e.g. `/addsemester S1 Semester 1`)_\n\n"
        "• `/registercourse <SEM_CODE> <COURSE_CODE> <Full Title>`\n"
        "  _Run inside the course Telegram group! Bot will generate join link and register group._\n"
        "  _(e.g. `/registercourse S1 CSE101 Data Structures`)_\n\n"
        "• `/courses`\n"
        "  _View all registered courses and their status._\n\n"
        "• `/panel`\n"
        "  _View system statistics and pending requests._"
    )
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("addsemester"))
async def handle_add_semester(message: Message):
    if not await db.is_admin(message.from_user.id, db_path=DB_PATH):
        return

    parts = message.text.strip().split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("⚠️ Usage: `/addsemester <CODE> <Full Name>`\nExample: `/addsemester S1 Semester 1`", parse_mode=ParseMode.MARKDOWN)
        return

    code = parts[1].strip()
    name = parts[2].strip()

    await db.add_semester(code, name, db_path=DB_PATH)
    await message.answer(f"✅ Semester registered successfully:\n*Code:* `{code}`\n*Name:* {name}", parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("registercourse"))
async def handle_register_course(message: Message, bot: Bot):
    if not await db.is_admin(message.from_user.id, db_path=DB_PATH):
        return

    if message.chat.type not in ("group", "supergroup"):
        await message.answer("⚠️ `/registercourse` must be run inside the Telegram course group!", parse_mode=ParseMode.MARKDOWN)
        return

    parts = message.text.strip().split(maxsplit=3)
    if len(parts) < 4:
        await message.answer(
            "⚠️ Usage inside group:\n`/registercourse <SEM_CODE> <COURSE_CODE> <Full Course Name>`\n"
            "Example:\n`/registercourse S1 CSE101 Data Structures`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    sem_code = parts[1].strip().upper()
    course_code = parts[2].strip().upper()
    course_name = parts[3].strip()

    sem = await db.get_semester_by_code(sem_code, db_path=DB_PATH)
    if not sem:
        await message.answer(f"❌ Semester code `{sem_code}` not found. Please create it first using `/addsemester`.", parse_mode=ParseMode.MARKDOWN)
        return

    # Verify bot permissions in this group
    try:
        bot_member = await message.chat.get_member(bot.id)
        if bot_member.status != "administrator":
            await message.answer("⚠️ Bot must be an administrator in this group before registering!")
            return
        if not getattr(bot_member, "can_invite_users", False):
            await message.answer("⚠️ Bot lacks *Invite Users* permission in this group!", parse_mode=ParseMode.MARKDOWN)
            return
        if not getattr(bot_member, "can_manage_chat", False) and not getattr(bot_member, "can_manage_topics", False):
            logger.warning(f"Bot registered in chat {message.chat.id} with basic admin permissions.")
    except Exception as e:
        logger.warning(f"Could not verify bot admin status in chat {message.chat.id}: {e}")

    # Generate dedicated join-request invite link
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=message.chat.id,
            name=f"{course_code} - Join Request",
            creates_join_request=True
        )
    except Exception as e:
        logger.error(f"Failed to create join-request invite link in chat {message.chat.id}: {e}")
        await message.answer(f"❌ Failed to create join-request invite link: {e}")
        return

    # Save course in database
    await db.register_course(
        semester_id=sem["id"],
        code=course_code,
        name=course_name,
        chat_id=message.chat.id,
        invite_link=invite.invite_link,
        db_path=DB_PATH
    )

    await message.answer(
        f"✅ *Course Registered Successfully!*\n\n"
        f"📚 *Semester:* {sem['name']} (`{sem['code']}`)\n"
        f"📖 *Course:* `{course_code}` - {course_name}\n"
        f"🆔 *Chat ID:* `{message.chat.id}`\n"
        f"🔗 *Join Request Link:* Created",
        parse_mode=ParseMode.MARKDOWN
    )


@dp.message(Command("courses"))
async def handle_list_courses(message: Message):
    if not await db.is_admin(message.from_user.id, db_path=DB_PATH):
        return

    courses = await db.get_all_courses(db_path=DB_PATH)
    if not courses:
        await message.answer("No courses currently registered.")
        return

    lines = ["📋 *Registered Courses:*"]
    for c in courses:
        lines.append(
            f"• [{c['semester_code']}] `{c['code']}` - {c['name']} (Group: `{c['chat_id']}`)"
        )
    await message.answer("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@dp.message(Command("panel"))
async def handle_admin_panel(message: Message):
    if not await db.is_admin(message.from_user.id, db_path=DB_PATH):
        return

    admins = await db.get_all_admins(db_path=DB_PATH)
    courses = await db.get_all_courses(db_path=DB_PATH)
    pending = await db.get_pending_requests(db_path=DB_PATH)

    text = (
        "📊 *Bot Administration Panel*\n\n"
        f"👑 *Admins Count:* {len(admins)}\n"
        f"📚 *Total Courses:* {len(courses)}\n"
        f"⏳ *Pending Join Requests:* {len(pending)}\n"
    )

    if pending:
        text += "\n*Pending Requests:*\n"
        for p in pending[:10]:
            name = p.get("student_name") or f"ID {p['user_id']}"
            text += f"• `{p['course_code']}` - {name} (ID: `{p['user_id']}`)\n"
        if len(pending) > 10:
            text += f"_...and {len(pending) - 10} more._\n"

    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


# --- Join Request & Admin Approval Handlers ---

@dp.chat_join_request()
async def handle_chat_join_request(event: ChatJoinRequest, bot: Bot):
    chat_id = event.chat.id
    user = event.from_user

    course = await db.get_course_by_chat_id(chat_id, db_path=DB_PATH)
    if not course:
        logger.warning(f"Join request in unregistered group: {chat_id}")
        return

    sem = await db.get_semester_by_id(course["semester_id"], db_path=DB_PATH)
    sem_code = sem["code"] if sem else "N/A"
    sem_name = sem["name"] if sem else "N/A"

    # Record student & request in DB
    await db.upsert_student(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name,
        db_path=DB_PATH
    )

    req_id = await db.record_join_request(
        chat_id=chat_id,
        user_id=user.id,
        course_id=course["id"],
        status="pending",
        db_path=DB_PATH
    )

    # Immediately refresh student's menu if they have it open
    await refresh_student_menu(bot, user.id)

    # Notify all admins in private chat
    admins = await db.get_all_admins(db_path=DB_PATH)
    username_str = f"@{user.username}" if user.username else "None"

    admin_notification = (
        "🔔 *New Join Request Submitted*\n\n"
        f"👤 *Student:* {user.full_name}\n"
        f"🏷 *Username:* {username_str}\n"
        f"🆔 *User ID:* `{user.id}`\n\n"
        f"📚 *Semester:* {sem_code} - {sem_name}\n"
        f"📖 *Course:* `{course['code']}` - {course['name']}\n"
        f"🏢 *Group:* {event.chat.title}"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Approve", callback_data=f"approve:{req_id}"),
        InlineKeyboardButton(text="❌ Reject", callback_data=f"reject:{req_id}")
    ]])

    for admin in admins:
        try:
            await bot.send_message(
                chat_id=admin["telegram_id"],
                text=admin_notification,
                reply_markup=kb,
                parse_mode=ParseMode.MARKDOWN
            )
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            logger.warning(f"Failed to notify admin {admin['telegram_id']}: {e}")


@dp.callback_query(F.data.startswith("approve:") | F.data.startswith("reject:"))
async def handle_admin_action(callback: CallbackQuery, bot: Bot):
    admin_id = callback.from_user.id
    if not await db.is_admin(admin_id, db_path=DB_PATH):
        await callback.answer("⛔ You are not an authorized admin.", show_alert=True)
        return

    action, req_id_str = callback.data.split(":")
    if not req_id_str.isdigit():
        await callback.answer("Invalid request ID.", show_alert=True)
        return

    req_id = int(req_id_str)
    req = await db.get_join_request_by_id(req_id, db_path=DB_PATH)

    if not req:
        await callback.answer("⚠️ Request record not found.", show_alert=True)
        return

    # Check if already processed
    if req["status"] != "pending":
        await callback.answer(f"⚠️ Already processed as '{req['status']}'.", show_alert=True)
        if callback.message:
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass
        return

    chat_id = req["chat_id"]
    user_id = req["user_id"]
    course_code = req["course_code"]
    course_name = req["course_name"]

    if action == "approve":
        try:
            await bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logger.error(f"approve_chat_join_request failed: {e}")
            await callback.answer(f"Telegram error: {e}", show_alert=True)
            return

        await db.update_join_request_status(req_id, "approved", processed_by=admin_id, db_path=DB_PATH)
        await callback.answer("✅ Request Approved!")

        # Update admin message
        if callback.message:
            try:
                updated_text = (
                    f"{callback.message.text}\n\n"
                    f"🟢 *Status:* Approved by {callback.from_user.full_name}"
                )
                await callback.message.edit_text(updated_text, reply_markup=None, parse_mode=None)
            except TelegramBadRequest:
                pass

        # Notify student in private chat
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"🎉 *Congratulations!*\nYour request to join *{course_code} - {course_name}* has been *approved*!",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.debug(f"Could not notify student {user_id} of approval: {e}")

        # Live refresh student's menu
        await refresh_student_menu(bot, user_id)

    elif action == "reject":
        try:
            await bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as e:
            logger.error(f"decline_chat_join_request failed: {e}")
            await callback.answer(f"Telegram error: {e}", show_alert=True)
            return

        await db.update_join_request_status(req_id, "rejected", processed_by=admin_id, db_path=DB_PATH)
        await callback.answer("❌ Request Declined!")

        # Update admin message
        if callback.message:
            try:
                updated_text = (
                    f"{callback.message.text}\n\n"
                    f"🔴 *Status:* Declined by {callback.from_user.full_name}"
                )
                await callback.message.edit_text(updated_text, reply_markup=None, parse_mode=None)
            except TelegramBadRequest:
                pass

        # Notify student in private chat
        try:
            await bot.send_message(
                chat_id=user_id,
                text=f"ℹ️ Your request to join *{course_code} - {course_name}* was declined.\n"
                     "You may re-apply later if appropriate.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.debug(f"Could not notify student {user_id} of decline: {e}")

        # Live refresh student's menu
        await refresh_student_menu(bot, user_id)


# --- Membership Change Handler (Student Left/Kicked) ---

@dp.chat_member()
async def handle_chat_member_updated(event: ChatMemberUpdated, bot: Bot):
    chat_id = event.chat.id
    user_id = event.from_user.id
    new_status = event.new_chat_member.status

    if new_status in ("left", "kicked"):
        logger.info(f"User {user_id} left/kicked from course group {chat_id}")
        await db.set_member_status(chat_id, user_id, "left", db_path=DB_PATH)
        # Update menu if student is viewing
        await refresh_student_menu(bot, user_id)


# --- Service Message Auto-Deletion ---

@dp.message(F.content_type.in_({ContentType.NEW_CHAT_MEMBERS, ContentType.LEFT_CHAT_MEMBER}))
async def handle_service_messages(message: Message):
    """Automatically delete 'X joined/left the group' service messages."""
    try:
        await message.delete()
    except TelegramBadRequest as e:
        logger.warning(f"Could not delete service message in chat {message.chat.id}: {e}")
    except Exception as e:
        logger.debug(f"Service message deletion error: {e}")


# --- Application Startup ---

async def main():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing! Exiting.")
        return

    logger.info("Initializing database...")
    await db.init_db(db_path=DB_PATH)

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    logger.info("Starting Telegram long polling...")
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "callback_query", "chat_join_request", "chat_member"]
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
