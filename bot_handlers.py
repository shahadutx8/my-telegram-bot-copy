"""Telegram bot command/message handlers.

Split out from main.py so each module stays under host upload limits.
Imported by main.py; shared application state is passed in as ``ctx``
(main's globals) and merged into this module's namespace.
"""

def register_handlers(b, ctx=None):
    if ctx:
        globals().update(ctx)

    def _get_enabled_fields():
        return CONFIG.get("bot_reply_fields", FIELD_KEYS)

    USER_MENU_BUTTONS = {
        "generate": "⚡ Generate Profile",
        "ai":       "🤖 AI Name",
        "history":  "📋 My History",
        "files":    "📂 Files",
        "help":     "❓ Help",
    }

    def _user_menu_keyboard():
        keyboard = telebot.types.ReplyKeyboardMarkup(
            resize_keyboard=True,
            row_width=2,
        )
        keyboard.add(
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["generate"]),
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["ai"]),
        )
        keyboard.add(
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["history"]),
            telebot.types.KeyboardButton(USER_MENU_BUTTONS["files"]),
        )
        keyboard.add(telebot.types.KeyboardButton(USER_MENU_BUTTONS["help"]))
        return keyboard

    def _country_keyboard(action: str):
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        buttons = []
        seen = set()
        for country in get_country_details():
            # "bd" is an alias for Bangladesh; show it only once.
            canonical = "bangladesh" if country == "bd" else country
            if canonical in seen:
                continue
            seen.add(canonical)
            buttons.append(
                telebot.types.InlineKeyboardButton(
                    f"🌍 {canonical.capitalize()}",
                    callback_data=f"menu:{action}:{canonical}",
                )
            )
        keyboard.add(*buttons)
        return keyboard

    def _send_user_menu(message):
        b.reply_to(
            message,
            get_text("user_menu"),
            reply_markup=_user_menu_keyboard(),
            parse_mode="Markdown",
        )

    def _notify_new_pending_user(message):
        user = getattr(message, "from_user", message)
        uname = f"@{user.username}" if user.username else "N/A"
        fullname = f"{user.first_name or ''} {user.last_name or ''}".strip() or "N/A"
        Thread(target=notify_admin, args=(
            get_text(
                "approval_admin_notify",
                fullname=fullname,
                uname=uname,
                user_id=user.id,
                total_users=f"{len(KNOWN_USERS):,}",
            ),
            build_approval_keyboard(user.id),
        ), daemon=True).start()

    def _track_entry_user(subject):
        user = getattr(subject, "from_user", subject)
        is_new = track_user(
            user.id,
            user.username,
            user.first_name,
        )
        if is_new and user.id != get_admin_id():
            _notify_new_pending_user(subject)
        return is_new

    def _approval_guard(message) -> bool:
        """Stop unapproved users before any user-facing bot action."""
        user_id = message.from_user.id
        if is_user_approved(user_id):
            return True
        status = get_approval_status(user_id)
        key = "approval_rejected" if status == "rejected" else "approval_pending"
        b.reply_to(message, get_text(key))
        return False

    def _send_country_menu(message, action: str):
        icon = "🤖" if action == "ai" else "⚡"
        b.reply_to(
            message,
            get_text("country_menu", icon=icon),
            reply_markup=_country_keyboard(action),
        )

    def _send_generated_profile(chat_id, user, country_input):
        """Generate and send a regular profile from a menu or typed country."""
        if not is_user_approved(user.id):
            key = "approval_rejected" if get_approval_status(user.id) == "rejected" else "approval_pending"
            b.send_message(chat_id, get_text(key))
            return
        if is_banned(user.id):
            b.send_message(chat_id, get_text("banned_reply"))
            return
        try:
            country_details = get_country_details()
            profile = generate_profile(country_input)
            if not profile:
                b.send_message(chat_id, get_text("profile_failed"))
                return
            recent_log.append(profile)
            Thread(target=record_user_profile, args=(user.id, profile), daemon=True).start()
            if country_details[country_input].get("is_bd"):
                log_name_usage(
                    bd_name=profile["full_name"],
                    user_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                )
            track_user(user.id, user.username, user.first_name, increment_count=True)
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"
            tg = user.username
            tg_mention = f"@{tg}" if tg else "Not Available"
            dev_name = get_developer_name()
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            b.send_message(
                chat_id,
                get_text(
                    "profile_reply",
                    dev_line=dev_line,
                    tg_mention=tg_mention,
                    country=country_input.capitalize(),
                    field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.send_message(chat_id, get_text("profile_failed"))

    def _run_ai_generation(chat_id, user, country_arg):
        """Generate an AI-assisted profile and send it asynchronously."""
        if not is_user_approved(user.id):
            key = "approval_rejected" if get_approval_status(user.id) == "rejected" else "approval_pending"
            b.send_message(chat_id, get_text(key))
            return
        if is_banned(user.id):
            b.send_message(chat_id, get_text("banned_reply"))
            return
        if not _GEMINI_KEY:
            b.send_message(chat_id, get_text("ai_unavailable"))
            return
        country_details = get_country_details()
        if country_arg not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.send_message(chat_id, get_text("ai_unknown_country", keys=keys))
            return

        wait_msg = b.send_message(chat_id, get_text("ai_generating"))

        def _do_ainame():
            ai_name = generate_ai_name(country_arg)
            profile = generate_profile(country_arg)
            if not profile:
                try:
                    b.delete_message(chat_id, wait_msg.message_id)
                except Exception:
                    pass
                b.send_message(chat_id, get_text("ai_profile_failed"))
                return

            if ai_name:
                profile["full_name"] = ai_name
                profile["facebook_id"] = ai_name
                clean = re.sub(r"[^a-zA-Z0-9]", "", ai_name).lower()
                if clean:
                    rnum = random.randint(1000, 9999)
                    profile["username"] = f"{clean[:12]}{rnum}"
                    profile["email"] = f"{clean}{rnum}@gmail.com"
                    profile["google"] = profile["email"]

            Thread(target=record_user_profile, args=(user.id, profile), daemon=True).start()
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"

            dev_name = get_developer_name()
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            tg = user.username
            tg_mention = f"@{tg}" if tg else "Not Available"
            ai_badge = "🤖 *AI Generated*" if ai_name else "⚡ *Auto Generated*"
            try:
                b.delete_message(chat_id, wait_msg.message_id)
            except Exception:
                pass
            b.send_message(
                chat_id,
                get_text(
                    "ai_reply",
                    dev_line=dev_line,
                    ai_badge=ai_badge,
                    tg_mention=tg_mention,
                    country=country_arg.capitalize(),
                    field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )

        Thread(target=_do_ainame, daemon=True).start()

    @b.message_handler(commands=['start'])
    def send_welcome(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        used_count   = len(USED_NAMES)
        remaining    = get_total_combinations() - used_count
        country_keys = ", ".join(k.capitalize() for k in get_country_details().keys())
        b.reply_to(message, get_text("welcome",
            country_keys=country_keys,
            used_count=f"{used_count:,}",
            remaining=f"{remaining:,}",
        ), reply_markup=_user_menu_keyboard())
    @b.message_handler(commands=['panel'])
    def admin_panel(message):
        admin_id = get_admin_id()
        if admin_id and message.from_user.id == admin_id:
            total = get_total_combinations()
            b.reply_to(message, get_text("panel_reply",
                developer_name=get_developer_name(),
                total_combinations=f"{total:,}",
                used_count=f"{len(USED_NAMES):,}",
                remaining=f"{total - len(USED_NAMES):,}",
            ))
        else:
            b.reply_to(message, get_text("panel_not_admin"))

    @b.message_handler(commands=['reset'])
    def reset_names(message):
        admin_id = get_admin_id()
        if admin_id and message.from_user.id == admin_id:
            global USED_NAMES
            USED_NAMES = set()
            save_used_names(USED_NAMES)
            b.reply_to(message, get_text("reset_success"))
        else:
            b.reply_to(message, get_text("reset_not_admin"))

    @b.message_handler(commands=['history'])
    def send_history(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        with user_profiles_lock:
            history = list(USER_PROFILES.get(message.from_user.id, []))
        if not history:
            b.reply_to(message, get_text("history_empty"))
            return
        lines = [f"📋 আপনার শেষ *{len(history)}* টি প্রোফাইল:\n"]
        for i, p in enumerate(reversed(history), 1):
            ts = p.get("timestamp", "")[:10]
            country = p.get("country", "").capitalize()
            lines.append(f"━━━ #{i} ┃ {country} ┃ {ts} ━━━")
            lines.append(f"👤 `{p.get('full_name', '')}`")
            lines.append(f"📧 `{p.get('email', '')}`")
            lines.append(f"📞 `{p.get('mobile', '')}`")
            lines.append("")
        lines.append(get_text("history_copy_hint"))
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['files'])
    def list_downloadable_files(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        keyboard = build_file_keyboard(message.from_user.id)
        b.reply_to(
            message,
            get_file_commands_text(message.from_user.id),
            reply_markup=keyboard,
        )

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("file:")))
    def download_file_button(call):
        user = call.from_user
        _track_entry_user(user)
        if not is_user_approved(user.id):
            try:
                b.answer_callback_query(
                    call.id,
                    get_text(
                        "approval_rejected"
                        if get_approval_status(user.id) == "rejected"
                        else "approval_pending",
                    )[:200],
                    show_alert=True,
                )
            except Exception:
                pass
            return
        try:
            b.answer_callback_query(call.id, "ফাইল পাঠানো হচ্ছে…")
        except Exception:
            pass
        file_id = call.data.split(":", 1)[1].strip()
        with stored_files_lock:
            item = next((entry.copy() for entry in STORED_FILES if entry.get("id") == file_id), None)
        if not item:
            b.send_message(call.message.chat.id, "❌ ফাইলটি আর পাওয়া যাচ্ছে না।")
            return
        ok, error = send_stored_file(b, call.message.chat.id, item, user)
        if not ok:
            b.send_message(call.message.chat.id, f"⚠️ {error}")

    @b.message_handler(commands=['download', 'getfile'])
    def download_stored_file(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        parts = (message.text or "").strip().split(maxsplit=1)
        command = parts[1].strip().lstrip("/").lower() if len(parts) > 1 else ""
        if not command:
            b.reply_to(message, "📥 কমান্ড দিন: `/download command`", parse_mode="Markdown")
            return
        item = get_stored_file(command)
        if not item or not file_is_available(item, message.from_user.id):
            b.reply_to(
                message,
                f"❌ `{command}` নামে কোনো ফাইল পাওয়া যায়নি, অথবা আপনার access নেই/মেয়াদ শেষ।\n\n"
                f"{get_file_commands_text(message.from_user.id)}",
                parse_mode="Markdown",
            )
            return
        ok, error = send_stored_file(b, message.chat.id, item, message.from_user)
        if not ok:
            b.reply_to(message, f"⚠️ {error}")

    @b.message_handler(commands=['help'])
    def help_command(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        b.reply_to(
            message,
            get_text("help"),
            reply_markup=_user_menu_keyboard(),
            parse_mode="Markdown",
        )

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["generate"])
    def menu_generate(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        _send_country_menu(message, "generate")

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["ai"])
    def menu_ai(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return
        _send_country_menu(message, "ai")

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["history"])
    def menu_history(message):
        send_history(message)

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["files"])
    def menu_files(message):
        list_downloadable_files(message)

    @b.message_handler(func=lambda message: (message.text or "").strip() == USER_MENU_BUTTONS["help"])
    def menu_help(message):
        help_command(message)

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("menu:")))
    def menu_callback(call):
        try:
            b.answer_callback_query(call.id)
        except Exception:
            pass
        parts = call.data.split(":", 2)
        if len(parts) != 3:
            return
        action, country = parts[1], parts[2].lower()
        user = call.from_user
        _track_entry_user(user)
        if not is_user_approved(user.id):
            try:
                b.answer_callback_query(
                    call.id,
                    get_text(
                        "approval_rejected"
                        if get_approval_status(user.id) == "rejected"
                        else "approval_pending",
                    )[:200],
                    show_alert=True,
                )
            except Exception:
                pass
            return
        if is_banned(user.id):
            b.send_message(call.message.chat.id, get_text("banned_reply"))
            return
        if action == "generate":
            _send_generated_profile(call.message.chat.id, user, country)
        elif action == "ai":
            _run_ai_generation(call.message.chat.id, user, country)

    @b.message_handler(commands=['ainame'])
    def send_ai_name(message):
        _track_entry_user(message)
        if not _approval_guard(message):
            return
        parts       = message.text.strip().split(maxsplit=1)
        country_arg = parts[1].strip().lower() if len(parts) > 1 else "bangladesh"
        _run_ai_generation(message.chat.id, message.from_user, country_arg)

    # ────────────────────────────────────────────
    # Admin Bot Commands — Dashboard Control via Bot
    # ────────────────────────────────────────────
    def _admin_only(msg) -> bool:
        """Returns True if sender is admin, otherwise replies and returns False."""
        admin_id = get_admin_id()
        if admin_id and msg.from_user.id == admin_id:
            return True
        b.reply_to(msg, "❌ এই কমান্ড শুধু অ্যাডমিন ব্যবহার করতে পারবেন!")
        return False

    @b.message_handler(commands=['admin', 'menu'])
    def admin_menu(message):
        if not _admin_only(message): return
        b.reply_to(message,
            "🔐 *অ্যাডমিন কন্ট্রোল প্যানেল*\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "📊 *পরিসংখ্যান ও লগ*\n"
            "• /stats — বট স্ট্যাটস\n"
            "• /users — ইউজার লিস্ট\n"
            "• /usage — নেম ইউজেজ লগ\n\n"
            "🚫 *ব্যান ম্যানেজমেন্ট*\n"
            "• /banned — ব্যানড লিস্ট\n"
            "• /ban `<id>` [কারণ] — ব্যান করুন\n"
            "• /unban `<id>` — ব্যান তুলুন\n\n"
            "⚙️ *কনফিগ ও ফিল্ড*\n"
            "• /config — বর্তমান কনফিগ\n"
            "• /fields — প্রোফাইল ফিল্ড স্ট্যাটাস\n"
            "• /setdev `<নাম>` — ডেভলপার নাম\n"
            "• /setadminid `<id>` — অ্যাডমিন ID\n\n"
            "🧪 *জেনারেট ও টেস্ট*\n"
            "• /gen `<country>` — টেস্ট প্রোফাইল\n"
            "• /reset — ব্যবহৃত নাম রিসেট\n\n"
            "📢 *ব্রডকাস্ট*\n"
            "• /broadcast `<টেক্সট>` — সবাইকে মেসেজ\n\n"
            "📂 *ফাইল ডাউনলোড*\n"
            "• /files — সংরক্ষিত ফাইলের তালিকা\n"
            "• /download `<কমান্ড>` — ফাইল পাঠান\n\n"
            "🛑 *বট স্টপ*\n"
            "• /stopbot — পোলিং বন্ধ করুন"
            + get_text("approval_admin_menu"),
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['pending'])
    def pending_users_cmd(message):
        if not _admin_only(message):
            return
        with users_lock:
            pending = [
                (uid, info.copy())
                for uid, info in KNOWN_USERS.items()
                if info.get("approval_status") == "pending"
            ]
        if not pending:
            b.reply_to(message, get_text("approval_none_pending"))
            return
        lines = []
        keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
        for uid, info in pending[:50]:
            username = f"@{info.get('username')}" if info.get("username") else "—"
            name = info.get("first_name") or "—"
            lines.append(f"• {name} {username} — ID: {uid}")
            keyboard.add(
                telebot.types.InlineKeyboardButton(
                    f"✅ {uid}",
                    callback_data=f"approval:approve:{uid}",
                ),
                telebot.types.InlineKeyboardButton(
                    f"❌ {uid}",
                    callback_data=f"approval:reject:{uid}",
                ),
            )
        if len(pending) > 50:
            lines.append(f"\n…আরও {len(pending) - 50} জন pending আছে।")
        b.reply_to(
            message,
            get_text(
                "approval_pending_list",
                count=len(pending),
                users="\n".join(lines),
            ),
            reply_markup=keyboard,
            parse_mode="Markdown",
        )

    def _set_user_approval_from_command(message, status: str):
        if not _admin_only(message):
            return
        parts = (message.text or "").strip().split()
        action = "approve" if status == "approved" else "reject"
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            b.reply_to(
                message,
                get_text("approval_admin_usage", action=action),
                parse_mode="Markdown",
            )
            return
        user_id = int(parts[1])
        if not set_approval_status(user_id, status):
            b.reply_to(message, get_text("approval_admin_not_found"))
            return
        key = "approval_admin_approved" if status == "approved" else "approval_admin_rejected"
        b.reply_to(message, get_text(key, user_id=user_id), parse_mode="Markdown")
        try:
            if status == "approved":
                b.send_message(
                    user_id,
                    get_text("approval_granted"),
                    reply_markup=_user_menu_keyboard(),
                )
            else:
                b.send_message(user_id, get_text("approval_rejected"))
        except Exception as e:
            print(f"[approval] user notification failed for {user_id}: {e}")

    @b.message_handler(commands=['approve'])
    def approve_user_cmd(message):
        _set_user_approval_from_command(message, "approved")

    @b.message_handler(commands=['reject'])
    def reject_user_cmd(message):
        _set_user_approval_from_command(message, "rejected")

    @b.callback_query_handler(func=lambda call: bool(call.data and call.data.startswith("approval:")))
    def approval_callback(call):
        if call.from_user.id != get_admin_id():
            try:
                b.answer_callback_query(call.id, "❌ শুধু অ্যাডমিন এই action করতে পারবেন।", show_alert=True)
            except Exception:
                pass
            return
        parts = call.data.split(":")
        if len(parts) != 3 or not parts[2].lstrip("-").isdigit():
            return
        action, user_id = parts[1], int(parts[2])
        status = "approved" if action == "approve" else "rejected" if action == "reject" else None
        if not status or not set_approval_status(user_id, status):
            try:
                b.answer_callback_query(call.id, get_text("approval_admin_not_found"), show_alert=True)
            except Exception:
                pass
            return
        key = "approval_admin_approved" if status == "approved" else "approval_admin_rejected"
        try:
            b.answer_callback_query(call.id, get_text(key, user_id=user_id))
            b.edit_message_reply_markup(
                call.message.chat.id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass
        try:
            if status == "approved":
                b.send_message(
                    user_id,
                    get_text("approval_granted"),
                    reply_markup=_user_menu_keyboard(),
                )
            else:
                b.send_message(user_id, get_text("approval_rejected"))
        except Exception as e:
            print(f"[approval] user notification failed for {user_id}: {e}")

    @b.message_handler(commands=['stats'])
    def bot_stats(message):
        if not _admin_only(message): return
        total  = get_total_combinations()
        used   = len(USED_NAMES)
        remain = max(total - used, 0)
        b.reply_to(message,
            "📊 *বট স্ট্যাটস*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔤 মোট নাম সম্ভাবনা: `{total:,}`\n"
            f"✅ ব্যবহৃত নাম: `{used:,}`\n"
            f"🆕 বাকি নাম: `{remain:,}`\n\n"
            f"👥 মোট ইউজার: `{len(KNOWN_USERS):,}`\n"
            f"🚫 ব্যানড ইউজার: `{len(BANNED_USERS):,}`\n"
            f"🌍 সাপোর্টেড দেশ: `{len(get_country_details())}`\n\n"
            f"👤 ডেভলপার: {get_developer_name() or '_(সেট নেই)_'}\n"
            f"🆔 Admin ID: `{get_admin_id() or '(সেট নেই)'}`",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['users'])
    def users_list(message):
        if not _admin_only(message): return
        parts    = message.text.strip().split()
        page     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        per_page = 10
        all_u    = sorted(KNOWN_USERS.items(),
                          key=lambda x: x[1].get("profile_count", 0), reverse=True)
        total        = len(all_u)
        total_pages  = max((total + per_page - 1) // per_page, 1)
        page         = max(1, min(page, total_pages))
        start        = (page - 1) * per_page
        chunk        = all_u[start:start + per_page]
        lines = [f"👥 *ইউজার লিস্ট* — পেজ {page}/{total_pages} (মোট {total:,})\n"]
        for rank, (uid, info) in enumerate(chunk, start + 1):
            uname = f"@{info['username']}" if info.get("username") else "—"
            name  = info.get("first_name") or "—"
            count = info.get("profile_count", 0)
            lines.append(f"{rank}. {name} {uname}\n   🆔 `{uid}` | 📋 {count} profiles")
        if page < total_pages:
            lines.append(f"\n➡️ পরের পেজ: /users {page + 1}")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['usage'])
    def usage_list(message):
        if not _admin_only(message): return
        parts    = message.text.strip().split()
        page     = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        per_page = 15
        names    = sorted(USED_NAMES)
        total    = len(names)
        if not total:
            b.reply_to(message, "ℹ️ এখনও কোনো নাম ব্যবহৃত হয়নি।")
            return
        total_pages = max((total + per_page - 1) // per_page, 1)
        page        = max(1, min(page, total_pages))
        start       = (page - 1) * per_page
        chunk       = names[start:start + per_page]
        lines = [f"📋 *ব্যবহৃত নাম লগ* — পেজ {page}/{total_pages} (মোট {total:,})\n"]
        for i, name in enumerate(chunk, start + 1):
            lines.append(f"{i}. `{name}`")
        if page < total_pages:
            lines.append(f"\n➡️ পরের পেজ: /usage {page + 1}")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['banned'])
    def banned_list_cmd(message):
        if not _admin_only(message): return
        if not BANNED_USERS:
            b.reply_to(message, "✅ কোনো ব্যানড ইউজার নেই।")
            return
        lines = [f"🚫 *ব্যানড ইউজার* (মোট {len(BANNED_USERS)})\n"]
        for uid, info in list(BANNED_USERS.items())[:20]:
            uname  = f"@{info.get('username')}" if info.get("username") else "—"
            reason = info.get("reason") or "—"
            lines.append(f"• `{uid}` {uname}\n  কারণ: {reason}")
        if len(BANNED_USERS) > 20:
            lines.append(f"\n…আরও {len(BANNED_USERS) - 20} জন (ড্যাশবোর্ড দেখুন)")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['ban'])
    def ban_user_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=2)
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /ban `<user_id>` [কারণ]", parse_mode="Markdown")
            return
        uid    = int(parts[1])
        reason = parts[2].strip() if len(parts) > 2 else "অ্যাডমিন কর্তৃক ব্যান"
        if uid in BANNED_USERS:
            b.reply_to(message, f"ℹ️ ইউজার `{uid}` ইতিমধ্যে ব্যান আছে।", parse_mode="Markdown")
            return
        info = KNOWN_USERS.get(uid, {})
        BANNED_USERS[uid] = {
            "username":   info.get("username"),
            "first_name": info.get("first_name"),
            "reason":     reason,
            "timestamp":  _dt.utcnow().isoformat(),
        }
        save_banned(BANNED_USERS)
        b.reply_to(message,
            f"✅ ইউজার `{uid}` ব্যান করা হয়েছে।\nকারণ: {reason}",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['unban'])
    def unban_user_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split()
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /unban `<user_id>`", parse_mode="Markdown")
            return
        uid = int(parts[1])
        if uid not in BANNED_USERS:
            b.reply_to(message, f"ℹ️ ইউজার `{uid}` ব্যানড নেই।", parse_mode="Markdown")
            return
        BANNED_USERS.pop(uid)
        save_banned(BANNED_USERS)
        b.reply_to(message, f"✅ ইউজার `{uid}` আনব্যান করা হয়েছে।", parse_mode="Markdown")

    @b.message_handler(commands=['broadcast'])
    def broadcast_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            b.reply_to(message, "⚠️ ব্যবহার: /broadcast `<মেসেজ টেক্সট>`", parse_mode="Markdown")
            return
        text     = parts[1].strip()
        user_ids = list(KNOWN_USERS.keys())
        if not user_ids:
            b.reply_to(message, "❌ কোনো ইউজার নেই।")
            return
        wait_msg = b.reply_to(message, f"📢 {len(user_ids):,} জনকে মেসেজ পাঠানো হচ্ছে…")
        def _do_bc():
            sent = failed = 0
            for uid in user_ids:
                try:
                    b.send_message(uid, text)
                    sent += 1
                    _time_module.sleep(0.05)   # rate-limit
                except Exception:
                    failed += 1
            try:
                b.edit_message_text(
                    f"✅ ব্রডকাস্ট সম্পন্ন!\n\n"
                    f"📤 পাঠানো: {sent}\n"
                    f"❌ ব্যর্থ: {failed}",
                    message.chat.id, wait_msg.message_id
                )
            except Exception:
                pass
        Thread(target=_do_bc, daemon=True).start()

    @b.message_handler(commands=['gen', 'generate'])
    def gen_profile_cmd(message):
        if not _admin_only(message): return
        parts       = message.text.strip().split(maxsplit=1)
        country_arg = parts[1].strip().lower() if len(parts) > 1 else "bangladesh"
        country_details = get_country_details()
        if country_arg not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.reply_to(message, f"⚠️ অজানা দেশ। লিখুন: {keys}")
            return
        try:
            profile     = generate_profile(country_arg)
            enabled     = CONFIG.get("bot_reply_fields", FIELD_KEYS)
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    field_lines += f"{FIELD_EMOJI[key]} {FIELD_LABELS[key]}: `{profile[key]}`\n"
            b.reply_to(message,
                f"🧪 *টেস্ট প্রোফাইল* — {country_arg.capitalize()}\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"{field_lines}"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"_(লগ বা হিস্ট্রিতে সেভ হয়নি)_",
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.reply_to(message, "⚠️ প্রোফাইল তৈরি করা সম্ভব হয়নি।")

    @b.message_handler(commands=['config'])
    def show_config_cmd(message):
        if not _admin_only(message): return
        token = CONFIG.get("bot_token", "")
        if len(token) > 12:
            token_masked = f"{token[:8]}…{token[-4:]}"
        elif token:
            token_masked = token
        else:
            token_masked = "_(সেট নেই)_"
        enabled_fields = CONFIG.get("bot_reply_fields", FIELD_KEYS)
        sfx_sample     = ", ".join(CONFIG.get("nickname_sfx", [])[:5])
        b.reply_to(message,
            "⚙️ *বর্তমান কনফিগ*\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🤖 Token: `{token_masked}`\n"
            f"🆔 Admin ID: `{get_admin_id() or '(সেট নেই)'}`\n"
            f"👤 Developer: {get_developer_name() or '_(সেট নেই)_'}\n\n"
            f"📋 এনাবল্ড ফিল্ড: {len(enabled_fields)}/{len(FIELD_KEYS)}\n"
            f"🔤 মোট নাম সম্ভাবনা: `{get_total_combinations():,}`\n"
            f"🏷️ Nickname suffix (প্রথম ৫): {sfx_sample}\n\n"
            f"সম্পাদনা: /setdev, /setadminid বা ড্যাশবোর্ড",
            parse_mode="Markdown"
        )

    @b.message_handler(commands=['fields'])
    def show_fields_cmd(message):
        if not _admin_only(message): return
        enabled = set(CONFIG.get("bot_reply_fields", FIELD_KEYS))
        lines   = ["📋 *প্রোফাইল ফিল্ড স্ট্যাটাস*\n"]
        for key in FIELD_KEYS:
            icon  = "✅" if key in enabled else "❌"
            label = FIELD_LABELS.get(key, key)
            emoji = FIELD_EMOJI.get(key, "")
            lines.append(f"{icon} {emoji} {label} (`{key}`)")
        lines.append("\n_ফিল্ড চালু/বন্ধ করতে ড্যাশবোর্ডের Settings ট্যাব ব্যবহার করুন।_")
        b.reply_to(message, "\n".join(lines), parse_mode="Markdown")

    @b.message_handler(commands=['setdev'])
    def set_developer_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split(maxsplit=1)
        name  = parts[1].strip() if len(parts) > 1 else ""
        CONFIG["developer_name"] = name
        save_config(CONFIG)
        if name:
            b.reply_to(message, f"✅ ডেভলপার নাম সেট হয়েছে: *{name}*", parse_mode="Markdown")
        else:
            b.reply_to(message, "✅ ডেভলপার নাম সরিয়ে দেওয়া হয়েছে।")

    @b.message_handler(commands=['setadminid'])
    def set_admin_id_cmd(message):
        if not _admin_only(message): return
        parts = message.text.strip().split()
        if len(parts) < 2 or not parts[1].lstrip('-').isdigit():
            b.reply_to(message, "⚠️ ব্যবহার: /setadminid `<telegram_id>`", parse_mode="Markdown")
            return
        new_id = int(parts[1])
        CONFIG["admin_id"] = new_id
        save_config(CONFIG)
        b.reply_to(message, f"✅ Admin ID সেট হয়েছে: `{new_id}`", parse_mode="Markdown")

    @b.message_handler(commands=['stopbot'])
    def stop_bot_cmd(message):
        if not _admin_only(message): return
        b.reply_to(message, "🛑 বট পোলিং বন্ধ করা হচ্ছে…")
        Thread(target=lambda: b.stop_polling(), daemon=True).start()

    @b.message_handler(func=lambda message: True)
    def handle_all_messages(message):
        _track_entry_user(message)

        if not _approval_guard(message):
            return

        if is_banned(message.from_user.id):
            b.reply_to(message, get_text("banned_reply"))
            return

        if not message.text:
            return

        country_input   = message.text.strip().lower()
        country_details = get_country_details()
        if country_input not in country_details:
            keys = ", ".join(k.capitalize() for k in country_details.keys())
            b.reply_to(message, get_text("unknown_country", keys=keys))
            return
        try:
            profile = generate_profile(country_input)
            recent_log.append(profile)
            Thread(target=record_user_profile, args=(message.from_user.id, profile), daemon=True).start()
            tg          = message.from_user.username
            tg_mention  = f"@{tg}" if tg else "Not Available"
            dev_name    = get_developer_name()
            if country_details[country_input].get('is_bd'):
                log_name_usage(
                    bd_name    = profile['full_name'],
                    user_id    = message.from_user.id,
                    username   = message.from_user.username,
                    first_name = message.from_user.first_name,
                )
            track_user(
                message.from_user.id,
                message.from_user.username,
                message.from_user.first_name,
                increment_count=True,
            )
            enabled = _get_enabled_fields()
            field_lines = ""
            for key in FIELD_KEYS:
                if key in enabled and key in profile:
                    emoji = FIELD_EMOJI[key]
                    label = FIELD_LABELS[key]
                    field_lines += f"{emoji} {label}: `{profile[key]}`\n"
            dev_line = f"👤 Developer: {dev_name}\n" if dev_name else ""
            b.reply_to(message,
                get_text("profile_reply",
                    dev_line=dev_line, tg_mention=tg_mention,
                    country=country_input.capitalize(), field_lines=field_lines,
                ),
                parse_mode="Markdown",
                reply_markup=build_profile_copy_keyboard(profile, enabled),
            )
        except Exception:
            b.reply_to(message, get_text("profile_failed"))
