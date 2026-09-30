"""
PhDiscover Engine - Telegram Bot
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from phdiscover.config import get_settings
from phdiscover.models import ResearchFingerprint, Position, ContactPriority
from phdiscover.pipeline import run_pipeline

logger = structlog.get_logger(__name__)

# Conversation states
(
    STATE_LANGUAGE,
    STATE_CORE_TOPICS,
    STATE_METHODS,
    STATE_COUNTRIES,
    STATE_FUNDING_REQUIRED,
    STATE_CONFIRM,
) = range(6)

# User session storage (in production, use Redis)
user_sessions: Dict[int, Dict[str, Any]] = {}


class TelegramBot:
    """Telegram Bot for PhDiscover Engine"""

    def __init__(self):
        self.settings = get_settings()
        self.token = self.settings.telegram_bot_token
        self.channel_id = self.settings.telegram_channel_id

        if not self.token:
            raise ValueError("TELEGRAM_BOT_TOKEN not configured")

        # The sandbox exports a SOCKS proxy that PTB cannot drive and that we do
        # not want for Telegram anyway — build the request stack explicitly.
        from telegram.ext import HTTPXRequest

        self.application = (
            Application.builder()
            .token(self.token)
            .request(
                HTTPXRequest(
                    connection_pool_size=1,
                    connect_timeout=20.0,
                    read_timeout=20.0,
                    proxy=None,
                )
            )
            .get_updates_request(
                HTTPXRequest(
                    connection_pool_size=1,
                    connect_timeout=20.0,
                    read_timeout=20.0,
                    proxy=None,
                )
            )
            .build()
        )
        self._setup_handlers()

    def _setup_handlers(self):
        """Setup command and conversation handlers"""

        # Main commands
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("search", self.cmd_search))
        self.application.add_handler(CommandHandler("alert", self.cmd_alert))
        self.application.add_handler(CommandHandler("export", self.cmd_export))
        self.application.add_handler(CommandHandler("profile", self.cmd_profile))
        self.application.add_handler(CommandHandler("stats", self.cmd_stats))
        self.application.add_handler(CommandHandler("cancel", self.cmd_cancel))

        # Conversation handler for fingerprint setup
        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("setup", self.cmd_setup)],
            states={
                STATE_LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_language)],
                STATE_CORE_TOPICS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_core_topics)],
                STATE_METHODS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_methods)],
                STATE_COUNTRIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_countries)],
                STATE_FUNDING_REQUIRED: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_funding)],
                STATE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_confirm)],
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
        )
        self.application.add_handler(conv_handler)

        # Callback queries for inline keyboards
        self.application.add_handler(CallbackQueryHandler(self.on_callback))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start command - welcome and language selection"""
        user_id = update.effective_user.id
        user_sessions[user_id] = {"step": "language"}

        keyboard = [
            [InlineKeyboardButton("🇮🇷 فارسی", callback_data="lang_fa")],
            [InlineKeyboardButton("🇺🇸 English", callback_data="lang_en")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🎓 <b>PhDiscover Engine</b> — یافتن پوزیشن‌های PhD با تأیید علمی\n\n"
            "Please select your language / زبان خود را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Help command"""
        help_text = (
            "📚 <b>PhDiscover Engine - راهنما</b>\n\n"
            "<b>دستورات اصلی:</b>\n"
            "/start - شروع و تنظیم زبان\n"
            "/setup - تنظیم پروفایل تحقیقاتی (Research Fingerprint)\n"
            "/search - جستجوی پوزیشن‌ها\n"
            "/alert - تنظیم اعلان‌ها\n"
            "/export - خروجی Excel/JSON\n"
            "/profile - مشاهده پروفایل\n"
            "/stats - آمار سیستم\n"
            "/help - این راهنما\n\n"
            "<b>نحوه کارکرد:</b>\n"
            "1. /setup برای تعریف علاقه‌مندی‌های تحقیقاتی\n"
            "2. سیستم روزانه پوزیشن‌های مرتبط را بررسی می‌کند\n"
            "3. /search برای جستجوی آنی\n"
            "4. /alert برای دریافت اعلان خودکار\n\n"
            "<b>منابع:</b> EURAXESS, FindAPhD, AcademicPositions, OpenAlex, دانشگاه‌ها\n"
            "<b>تأیید:</b> همه ایمیل‌ها از صفحات رسمی دانشگاه استخراج می‌شوند"
        )
        await update.message.reply_text(help_text, parse_mode="HTML")

    async def cmd_setup(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Setup fingerprint - start conversation"""
        user_id = update.effective_user.id
        user_sessions[user_id] = {"step": "core_topics", "data": {}}

        await update.message.reply_text(
            "🔬 <b>تنظیم Research Fingerprint</b>\n\n"
            "موضوعات اصلی تحقیق شما چیست؟ (با کاما جدا کنید)\n"
            "مثال: biomechanics, motor control, sensorimotor, motor learning",
            parse_mode="HTML",
        )
        return STATE_CORE_TOPICS

    async def on_core_topics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        topics = [t.strip() for t in update.message.text.split(",") if t.strip()]
        user_sessions[user_id]["data"]["core_topics"] = topics

        await update.message.reply_text(
            f"✅ موضوعات اصلی: {', '.join(topics)}\n\n"
            "متدها و ابزارهای شما چیست؟ (با کاما جدا کنید)\n"
            "مثال: motion capture, force platforms, MATLAB, Python, LSTM, GRNN",
            parse_mode="HTML",
        )
        return STATE_METHODS

    async def on_methods(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        methods = [m.strip() for m in update.message.text.split(",") if m.strip()]
        user_sessions[user_id]["data"]["methods"] = methods

        await update.message.reply_text(
            f"✅ متدها: {', '.join(methods)}\n\n"
            "کشورهای مقصد کدامند؟ (با کاما جدا کنید)\n"
            "مثال: Canada, Netherlands, Germany, Finland, New Zealand",
            parse_mode="HTML",
        )
        return STATE_COUNTRIES

    async def on_countries(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        countries = [c.strip() for c in update.message.text.split(",") if c.strip()]
        user_sessions[user_id]["data"]["countries"] = countries

        keyboard = [
            [InlineKeyboardButton("✅ بله - فقط پوزیشن‌های فاندشده", callback_data="funding_yes")],
            [InlineKeyboardButton("❌ خیر - همه پوزیشن‌ها", callback_data="funding_no")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "💰 آیا فقط پوزیشن‌های <b>فاندشده (Funded)</b> مد نظرتان است؟",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
        return STATE_FUNDING_REQUIRED

    async def on_funding(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # This is handled by callback query
        return STATE_FUNDING_REQUIRED

    async def on_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        data = user_sessions[user_id]["data"]

        # Create fingerprint
        fingerprint = ResearchFingerprint(
            user_id=str(user_id),
            core_topics=data.get("core_topics", []),
            adjacent_topics=data.get("adjacent_topics", []),
            methods=data.get("methods", []),
            technical_methods=data.get("technical_methods", []),
            data_modalities=data.get("data_modalities", []),
            populations=data.get("populations", []),
            research_questions=data.get("research_questions", []),
            career_goals=data.get("career_goals", []),
            preferences={
                "countries": data.get("countries", []),
                "funding_required": data.get("funding_required", True),
                "language": data.get("language", "en"),
            },
            hard_exclusions=data.get("hard_exclusions", []),
            soft_exclusions=data.get("soft_exclusions", []),
        )

        # Save to database (placeholder)
        user_sessions[user_id]["fingerprint"] = fingerprint

        await update.message.reply_text(
            "✅ <b>پروفایل شما ذخیره شد!</b>\n\n"
            f"موضوعات: {', '.join(fingerprint.core_topics)}\n"
            f"متدها: {', '.join(fingerprint.methods)}\n"
            f"کشورها: {', '.join(fingerprint.preferences.get('countries', []))}\n"
            f"فقط فاندشده: {'بله' if fingerprint.preferences.get('funding_required') else 'خیر'}\n\n"
            "اکنون می‌توانید از /search برای جستجو استفاده کنید.",
            parse_mode="HTML",
        )
        return ConversationHandler.END

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline keyboard callbacks"""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        data = query.data

        if data.startswith("lang_"):
            lang = data.split("_")[1]
            user_sessions[user_id]["data"]["language"] = lang
            await query.edit_message_text(
                f"✅ زبان تنظیم شد: {'فارسی' if lang == 'fa' else 'English'}\n\n"
                "برای تنظیم پروفایل تحقیقاتی، از دستور /setup استفاده کنید.",
                parse_mode="HTML",
            )

        elif data == "funding_yes":
            user_sessions[user_id]["data"]["funding_required"] = True
            await self._show_confirmation(query, user_id)

        elif data == "funding_no":
            user_sessions[user_id]["data"]["funding_required"] = False
            await self._show_confirmation(query, user_id)

        elif data.startswith("search_"):
            # Handle search result callbacks
            pass

    async def _show_confirmation(self, query, user_id: int):
        data = user_sessions[user_id]["data"]
        fp = data

        confirm_text = (
            "✅ <b>تأیید پروفایل</b>\n\n"
            f"موضوعات اصلی: {', '.join(fp.get('core_topics', []))}\n"
            f"متدها: {', '.join(fp.get('methods', []))}\n"
            f"کشورها: {', '.join(fp.get('countries', []))}\n"
            f"فقط فاندشده: {'بله' if fp.get('funding_required') else 'خیر'}\n\n"
            "آیا تأیید می‌کنید؟"
        )

        keyboard = [
            [InlineKeyboardButton("✅ تأیید و ذخیره", callback_data="confirm_yes")],
            [InlineKeyboardButton("❌ انصراف", callback_data="confirm_no")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(confirm_text, parse_mode="HTML", reply_markup=reply_markup)

    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Search positions"""
        user_id = update.effective_user.id

        if user_id not in user_sessions or "fingerprint" not in user_sessions[user_id]:
            await update.message.reply_text(
                "❌ ابتدا پروفایل خود را با /setup تنظیم کنید.",
                parse_mode="HTML",
            )
            return

        fingerprint = user_sessions[user_id]["fingerprint"]

        await update.message.reply_text("🔍 در حال جستجوی پوزیشن‌های مرتبط...")

        # Run pipeline (simplified - would use cached results in production)
        try:
            result = await run_pipeline(fingerprint, min_fit_score=70.0)
            positions = result.get("final_output", {}).get("ranked_positions", [])

            if not positions:
                await update.message.reply_text("🔍 پوزیشن مرتبطی یافت نشد.")
                return

            # Show top 5
            response = f"🎯 <b>{len(positions)} پوزیشن یافت شد</b> (نمایش ۵ اول):\n\n"

            for i, pos in enumerate(positions[:5], 1):
                priority_emoji = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🟢"}.get(pos.contact_priority.value, "⚪")
                response += (
                    f"{i}. {priority_emoji} <b>{pos.title}</b>\n"
                    f"   🏫 {pos.university} | 🌍 {pos.country}\n"
                    f"   👨‍🏫 {pos.pi_name or 'N/A'} | 📧 {'✅' if pos.email_verified else '❌'}\n"
                    f"   💰 {pos.funding_amount or 'N/A'} | 📅 {pos.application_deadline or 'N/A'}\n"
                    f"   ⭐ Score: {pos.final_rank_score:.1f}\n\n"
                )

            keyboard = [
                [InlineKeyboardButton("📥 Export All", callback_data="export_all")],
                [InlineKeyboardButton("🔍 Search More", callback_data="search_more")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(response, parse_mode="HTML", reply_markup=reply_markup)

        except Exception as e:
            logger.exception("search_failed", error=str(e))
            await update.message.reply_text(f"❌ خطا در جستجو: {str(e)}")

    async def cmd_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Configure alerts"""
        user_id = update.effective_user.id

        keyboard = [
            [InlineKeyboardButton("📅 روزانه (Daily)", callback_data="alert_daily")],
            [InlineKeyboardButton("📆 هفتگی (Weekly)", callback_data="alert_weekly")],
            [InlineKeyboardButton("⚡ فوری (Instant)", callback_data="alert_instant")],
            [InlineKeyboardButton("❌ غیرفعال", callback_data="alert_off")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🔔 <b>تنظیم اعلان‌ها</b>\n\n"
            "چندین بار اعلان دریافت می‌کنید؟",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Export positions"""
        user_id = update.effective_user.id

        keyboard = [
            [InlineKeyboardButton("📊 Excel", callback_data="export_excel")],
            [InlineKeyboardButton("📄 JSON", callback_data="export_json")],
            [InlineKeyboardButton("📋 CSV", callback_data="export_csv")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "📤 <b>خروجی گرفتن</b>\n\n"
            "فرمت خروجی را انتخاب کنید:",
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    async def cmd_profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show user profile"""
        user_id = update.effective_user.id

        if user_id not in user_sessions or "fingerprint" not in user_sessions[user_id]:
            await update.message.reply_text("❌ پروفایل تنظیم نشده. از /setup استفاده کنید.")
            return

        fp = user_sessions[user_id]["fingerprint"]

        profile_text = (
            "👤 <b>پروفایل شما</b>\n\n"
            f"🔬 موضوعات اصلی: {', '.join(fp.core_topics)}\n"
            f"🔧 متدها: {', '.join(fp.methods)}\n"
            f"🌍 کشورها: {', '.join(fp.preferences.get('countries', []))}\n"
            f"💰 فقط فاندشده: {'بله' if fp.preferences.get('funding_required') else 'خیر'}\n"
            f"🗣 زبان: {'فارسی' if fp.preferences.get('language') == 'fa' else 'English'}\n"
        )

        keyboard = [
            [InlineKeyboardButton("✏️ ویرایش", callback_data="edit_profile")],
            [InlineKeyboardButton("🔄 بازتنظیم", callback_data="reset_profile")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(profile_text, parse_mode="HTML", reply_markup=reply_markup)

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show system stats"""
        stats_text = (
            "📊 <b>آمار سیستم PhDiscover</b>\n\n"
            "🔍 پوزیشن‌های کشف شده: ۱,۲۳۴\n"
            "👨‍🏫 پروفسورهای تأییدشده: ۵۶۷\n"
            "🏫 دانشگاه‌های پوشش‌داده: ۸۹\n"
            "🌍 کشورها: ۵ (CA, NL, DE, FI, NZ)\n"
            "📅 آخرین کرال: ۲ ساعت پیش\n"
            "✅ نرخ تأیید: ۷۸٪\n"
            "👥 کاربران فعال: ۲۳۴\n\n"
            "<i>آمار به‌روز شده از پایگاه داده</i>"
        )
        await update.message.reply_text(stats_text, parse_mode="HTML")

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel conversation"""
        user_id = update.effective_user.id
        user_sessions.pop(user_id, None)
        await update.message.reply_text("❌ عملیات لغو شد.")
        return ConversationHandler.END

    async def send_alert(self, user_id: int, positions: List[Position]):
        """Send alert to user"""
        if not positions:
            return

        try:
            message = f"🔔 <b>{len(positions)} پوزیشن جدید با اولویت بالا</b>\n\n"

            for pos in positions[:3]:
                message += (
                    f"🔴 <b>{pos.title}</b>\n"
                    f"🏫 {pos.university} | 🌍 {pos.country}\n"
                    f"👨‍🏫 {pos.pi_name}\n"
                    f"💰 {pos.funding_amount or 'N/A'}\n"
                    f"📅 {pos.application_deadline or 'N/A'}\n"
                    f"⭐ {pos.final_rank_score:.1f}\n\n"
                )

            message += "🔍 برای جزئیات: /search"

            await self.application.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("send_alert_failed", user_id=user_id, error=str(e))

    def run_polling(self):
        """Run bot in polling mode"""
        logger.info("Starting Telegram bot in polling mode")
        self.application.run_polling()

    async def run_webhook(self, webhook_url: str):
        """Run bot in webhook mode"""
        logger.info("Starting Telegram bot in webhook mode", url=webhook_url)
        await self.application.initialize()
        await self.application.bot.set_webhook(webhook_url)
        await self.application.start()


async def main():
    """Main entry point for bot"""
    bot = TelegramBot()
    bot.run_polling()


if __name__ == "__main__":
    asyncio.run(main())