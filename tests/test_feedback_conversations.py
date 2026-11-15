"""Tests for two-way feedback conversations between users and support admins."""

from __future__ import annotations

import html
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.handlers import admin as admin_handler
from bot.handlers import start as start_handler
from bot.models import Base, Feedback, FeedbackMessage, User

pytestmark = pytest.mark.asyncio


# ── Helpers ─────────────────────────────────────────────────────────────────

class MockFSM:
    def __init__(self, initial_data=None, initial_state=None):
        self._data = dict(initial_data) if initial_data else {}
        self._state = initial_state

    async def get_state(self):
        return self._state

    async def set_state(self, state):
        self._state = state

    async def get_data(self):
        return dict(self._data)

    async def update_data(self, **kwargs):
        self._data.update(kwargs)

    async def clear(self):
        self._data.clear()
        self._state = None


def make_mock_bot():
    return SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(username="testbot")),
    )


def make_mock_message(
    user_id: int,
    text: str | None = None,
    photo: list | None = None,
    video: SimpleNamespace | None = None,
    caption: str | None = None,
    username: str = "clientuser",
    full_name: str = "Client Name",
    bot=None,
):
    if bot is None:
        bot = make_mock_bot()
    from_user = SimpleNamespace(
        id=user_id,
        username=username,
        first_name=full_name.split()[0],
        full_name=full_name,
    )
    return SimpleNamespace(
        text=text,
        caption=caption,
        photo=photo,
        video=video,
        from_user=from_user,
        bot=bot,
        answer=AsyncMock(),
    )


def make_mock_callback(
    user_id: int,
    data: str,
    username: str = "adminuser",
    full_name: str = "Admin Name",
    bot=None,
):
    if bot is None:
        bot = make_mock_bot()
    from_user = SimpleNamespace(
        id=user_id,
        username=username,
        first_name=full_name.split()[0],
        full_name=full_name,
    )
    msg = SimpleNamespace(
        answer=AsyncMock(),
        edit_text=AsyncMock(),
        bot=bot,
    )
    return SimpleNamespace(
        data=data,
        from_user=from_user,
        message=msg,
        bot=bot,
        answer=AsyncMock(),
    )


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def setup_env(monkeypatch, db_session_factory):
    monkeypatch.setattr(start_handler, "async_session", db_session_factory)
    monkeypatch.setattr(admin_handler, "async_session", db_session_factory)

    # Admin IDs
    admin_id = 999001
    monkeypatch.setattr(settings, "admin_ids", [admin_id])
    monkeypatch.setattr(settings, "owner_ids", [admin_id])
    monkeypatch.setattr(settings, "support_agent_ids", [admin_id])

    return {"admin_id": admin_id, "session_factory": db_session_factory}


# ── Tests ───────────────────────────────────────────────────────────────────

async def test_a_existing_feedback_creates_row_and_notifies_admins_with_reply_and_profile_buttons(setup_env):
    """A. Existing feedback submission creates Feedback, notifies admins with [💬 Ответить] [👤 Профиль]."""
    admin_id = setup_env["admin_id"]
    client_id = 111001

    bot = make_mock_bot()
    msg = make_mock_message(client_id, text="VPN is not connecting", bot=bot)
    state = MockFSM()

    await start_handler.feedback_receive(msg, state)

    # Verify DB
    async with setup_env["session_factory"]() as session:
        fb = await session.scalar(select(Feedback).where(Feedback.telegram_id == client_id))
        assert fb is not None
        assert fb.text == "VPN is not connecting"
        feedback_id = fb.id

    # Verify admin notification
    assert bot.send_message.await_count >= 1
    call_args = [call for call in bot.send_message.await_args_list if call.args[0] == admin_id][0]
    sent_text = call_args.args[1]
    sent_markup = call_args.kwargs.get("reply_markup")

    assert f"Фидбэк #{feedback_id}" in sent_text
    assert sent_markup is not None

    buttons = [b for row in sent_markup.inline_keyboard for b in row]
    reply_btn = next((b for b in buttons if b.callback_data == f"feedback_admin_reply_{feedback_id}"), None)
    profile_btn = next((b for b in buttons if b.callback_data == f"adm_usr_info_{client_id}"), None)

    assert reply_btn is not None
    assert "Ответить" in reply_btn.text
    assert profile_btn is not None
    assert "Профиль" in profile_btn.text


async def test_b_admin_reply_flow_and_delivery(setup_env):
    """B. Admin: feedback_admin_reply_<id> -> FSM -> sends text -> FeedbackMessage(sender_type='admin') -> client receives message with [↩️ Ответить]."""
    admin_id = setup_env["admin_id"]
    client_id = 111002

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=client_id, text="Help please", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()
    cb = make_mock_callback(admin_id, f"feedback_admin_reply_{feedback_id}", bot=bot)
    admin_state = MockFSM()

    # Step 1: Admin clicks Reply
    await admin_handler.feedback_admin_reply_start(cb, admin_state)
    assert await admin_state.get_state() == admin_handler.AdminStates.waiting_feedback_reply
    state_data = await admin_state.get_data()
    assert state_data.get("admin_reply_feedback_id") == feedback_id

    cb.message.answer.assert_awaited_once()
    assert f"#{feedback_id}" in cb.message.answer.await_args.args[0]

    # Step 2: Admin sends text response
    admin_msg = make_mock_message(admin_id, text="Try rebooting your device", bot=bot)
    await admin_handler.feedback_admin_reply_receive(admin_msg, admin_state)

    # State should be cleared
    assert await admin_state.get_state() is None

    # FeedbackMessage saved in DB
    async with setup_env["session_factory"]() as session:
        msgs = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == feedback_id))).all()
        assert len(msgs) == 1
        assert msgs[0].sender_type == "admin"
        assert msgs[0].sender_telegram_id == admin_id
        assert msgs[0].text == "Try rebooting your device"

    # Client received message
    assert bot.send_message.await_count >= 1
    client_calls = [c for c in bot.send_message.await_args_list if c.args[0] == client_id]
    assert len(client_calls) == 1
    client_sent_text = client_calls[0].args[1]
    client_markup = client_calls[0].kwargs.get("reply_markup")

    assert "Ответ поддержки" in client_sent_text
    assert f"#{feedback_id}" in client_sent_text
    assert "Try rebooting your device" in client_sent_text

    buttons = [b for row in client_markup.inline_keyboard for b in row]
    reply_btn = next((b for b in buttons if b.callback_data == f"feedback_reply_{feedback_id}"), None)
    assert reply_btn is not None
    assert "Ответить" in reply_btn.text


async def test_c_client_reply_flow_and_admin_notification(setup_env):
    """C. Client: feedback_reply_<id> -> FSM -> sends response -> FeedbackMessage(sender_type='user') -> admins receive response for same thread."""
    admin_id = setup_env["admin_id"]
    client_id = 111003

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=client_id, text="Initial problem", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()
    cb = make_mock_callback(client_id, f"feedback_reply_{feedback_id}", bot=bot)
    client_state = MockFSM()

    # Step 1: Client clicks Reply
    await start_handler.feedback_client_reply_start(cb, client_state)
    assert await client_state.get_state() == start_handler.FeedbackStates.waiting_reply
    state_data = await client_state.get_data()
    assert state_data.get("client_reply_feedback_id") == feedback_id

    # Step 2: Client sends message
    client_msg = make_mock_message(client_id, text="Still not working", bot=bot)
    await start_handler.feedback_client_reply_receive(client_msg, client_state)

    assert await client_state.get_state() is None

    # FeedbackMessage saved in DB
    async with setup_env["session_factory"]() as session:
        msgs = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == feedback_id))).all()
        assert len(msgs) == 1
        assert msgs[0].sender_type == "user"
        assert msgs[0].sender_telegram_id == client_id
        assert msgs[0].text == "Still not working"

    # Admin received notification
    admin_calls = [c for c in bot.send_message.await_args_list if c.args[0] == admin_id]
    assert len(admin_calls) == 1
    admin_notif_text = admin_calls[0].args[1]
    admin_markup = admin_calls[0].kwargs.get("reply_markup")

    assert "Ответ клиента" in admin_notif_text
    assert f"#{feedback_id}" in admin_notif_text
    assert "Still not working" in admin_notif_text

    buttons = [b for row in admin_markup.inline_keyboard for b in row]
    reply_btn = next((b for b in buttons if b.callback_data == f"feedback_admin_reply_{feedback_id}"), None)
    profile_btn = next((b for b in buttons if b.callback_data == f"adm_usr_info_{client_id}"), None)
    assert reply_btn is not None
    assert profile_btn is not None


async def test_d_client_cannot_reply_to_another_users_feedback(setup_env):
    """D. Client cannot reply to another user's feedback."""
    user_a = 111004
    user_b = 111005

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=user_a, text="User A issue", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()
    # User B tries to start reply to User A's feedback
    cb = make_mock_callback(user_b, f"feedback_reply_{feedback_id}", bot=bot)
    client_state = MockFSM()

    await start_handler.feedback_client_reply_start(cb, client_state)

    cb.answer.assert_awaited_once_with("Обращение не найдено", show_alert=True)
    assert await client_state.get_state() is None

    # Even if forged into state, receiving checks DB ownership
    await client_state.set_state(start_handler.FeedbackStates.waiting_reply)
    await client_state.update_data(client_reply_feedback_id=feedback_id)

    msg = make_mock_message(user_b, text="Malicious reply", bot=bot)
    await start_handler.feedback_client_reply_receive(msg, client_state)

    msg.answer.assert_awaited_once_with("❌ Обращение не найдено.")
    assert await client_state.get_state() is None

    # Verify no FeedbackMessage saved
    async with setup_env["session_factory"]() as session:
        msgs = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == feedback_id))).all()
        assert len(msgs) == 0


async def test_e_non_admin_cannot_use_admin_reply_callback(setup_env):
    """E. Non-admin cannot use admin reply callback."""
    non_admin_id = 888001

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=111006, text="Need help", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()
    cb = make_mock_callback(non_admin_id, f"feedback_admin_reply_{feedback_id}", bot=bot)
    state = MockFSM()

    await admin_handler.feedback_admin_reply_start(cb, state)

    cb.answer.assert_awaited_once_with("Доступ запрещен", show_alert=True)
    assert await state.get_state() is None


async def test_f_missing_feedback_id_fails_safely(setup_env):
    """F. Missing feedback_id fails safely."""
    admin_id = setup_env["admin_id"]
    client_id = 111007

    bot = make_mock_bot()

    # Admin side with non-existent id
    cb_admin = make_mock_callback(admin_id, "feedback_admin_reply_999999", bot=bot)
    admin_state = MockFSM()
    await admin_handler.feedback_admin_reply_start(cb_admin, admin_state)
    cb_admin.answer.assert_awaited_once_with("Обращение не найдено", show_alert=True)
    assert await admin_state.get_state() is None

    # Client side with non-existent id
    cb_client = make_mock_callback(client_id, "feedback_reply_999999", bot=bot)
    client_state = MockFSM()
    await start_handler.feedback_client_reply_start(cb_client, client_state)
    cb_client.answer.assert_awaited_once_with("Обращение не найдено", show_alert=True)
    assert await client_state.get_state() is None


async def test_g_two_different_feedback_threads_for_same_user_do_not_mix(setup_env):
    """G. Two different feedback threads for same user do not mix."""
    admin_id = setup_env["admin_id"]
    client_id = 111008

    async with setup_env["session_factory"]() as session:
        fb1 = Feedback(telegram_id=client_id, text="Thread 1 initial", user_id=0)
        fb2 = Feedback(telegram_id=client_id, text="Thread 2 initial", user_id=0)
        session.add_all([fb1, fb2])
        await session.commit()
        id1, id2 = fb1.id, fb2.id

    bot = make_mock_bot()

    # Admin replies to Thread 1
    state1 = MockFSM(initial_data={"admin_reply_feedback_id": id1}, initial_state=admin_handler.AdminStates.waiting_feedback_reply)
    msg1 = make_mock_message(admin_id, text="Reply for thread 1", bot=bot)
    await admin_handler.feedback_admin_reply_receive(msg1, state1)

    # Admin replies to Thread 2
    state2 = MockFSM(initial_data={"admin_reply_feedback_id": id2}, initial_state=admin_handler.AdminStates.waiting_feedback_reply)
    msg2 = make_mock_message(admin_id, text="Reply for thread 2", bot=bot)
    await admin_handler.feedback_admin_reply_receive(msg2, state2)

    # User replies to Thread 1
    user_state1 = MockFSM(initial_data={"client_reply_feedback_id": id1}, initial_state=start_handler.FeedbackStates.waiting_reply)
    user_msg1 = make_mock_message(client_id, text="User response thread 1", bot=bot)
    await start_handler.feedback_client_reply_receive(user_msg1, user_state1)

    # User replies to Thread 2
    user_state2 = MockFSM(initial_data={"client_reply_feedback_id": id2}, initial_state=start_handler.FeedbackStates.waiting_reply)
    user_msg2 = make_mock_message(client_id, text="User response thread 2", bot=bot)
    await start_handler.feedback_client_reply_receive(user_msg2, user_state2)

    # Verify messages in DB
    async with setup_env["session_factory"]() as session:
        fb1_msgs = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == id1).order_by(FeedbackMessage.id))).all()
        fb2_msgs = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == id2).order_by(FeedbackMessage.id))).all()

        assert [m.text for m in fb1_msgs] == ["Reply for thread 1", "User response thread 1"]
        assert [m.text for m in fb2_msgs] == ["Reply for thread 2", "User response thread 2"]


async def test_h_two_independent_admin_fsm_contexts_do_not_mix(setup_env):
    """H. Two independent admin FSM contexts do not mix."""
    admin_1 = 999001
    admin_2 = 999002
    monkeypatch_settings = settings
    monkeypatch_settings.admin_ids = [admin_1, admin_2]

    async with setup_env["session_factory"]() as session:
        fb_a = Feedback(telegram_id=111010, text="Issue A", user_id=0)
        fb_b = Feedback(telegram_id=111020, text="Issue B", user_id=0)
        session.add_all([fb_a, fb_b])
        await session.commit()
        id_a, id_b = fb_a.id, fb_b.id

    bot = make_mock_bot()

    # Admin 1 starts reply to fb_a
    state_adm1 = MockFSM()
    cb1 = make_mock_callback(admin_1, f"feedback_admin_reply_{id_a}", bot=bot)
    await admin_handler.feedback_admin_reply_start(cb1, state_adm1)

    # Admin 2 starts reply to fb_b
    state_adm2 = MockFSM()
    cb2 = make_mock_callback(admin_2, f"feedback_admin_reply_{id_b}", bot=bot)
    await admin_handler.feedback_admin_reply_start(cb2, state_adm2)

    assert (await state_adm1.get_data())["admin_reply_feedback_id"] == id_a
    assert (await state_adm2.get_data())["admin_reply_feedback_id"] == id_b

    # Admin 1 sends message
    msg1 = make_mock_message(admin_1, text="Admin 1 reply to A", bot=bot)
    await admin_handler.feedback_admin_reply_receive(msg1, state_adm1)

    # Admin 2 sends message
    msg2 = make_mock_message(admin_2, text="Admin 2 reply to B", bot=bot)
    await admin_handler.feedback_admin_reply_receive(msg2, state_adm2)

    async with setup_env["session_factory"]() as session:
        msgs_a = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == id_a))).all()
        msgs_b = (await session.scalars(select(FeedbackMessage).where(FeedbackMessage.feedback_id == id_b))).all()

        assert len(msgs_a) == 1
        assert msgs_a[0].text == "Admin 1 reply to A"
        assert msgs_a[0].sender_telegram_id == admin_1

        assert len(msgs_b) == 1
        assert msgs_b[0].text == "Admin 2 reply to B"
        assert msgs_b[0].sender_telegram_id == admin_2


async def test_i_photo_video_reply_stored_and_relayed_with_same_file_id(setup_env):
    """I. Photo/video reply is stored and relayed with same file_id."""
    admin_id = setup_env["admin_id"]
    client_id = 111009

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=client_id, text="Media test", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()

    # 1. Admin sends photo reply to client
    adm_state = MockFSM(initial_data={"admin_reply_feedback_id": feedback_id}, initial_state=admin_handler.AdminStates.waiting_feedback_reply)
    photo_obj = [SimpleNamespace(file_id="photo_file_xyz_123")]
    adm_msg = make_mock_message(admin_id, photo=photo_obj, caption="Check this screenshot", bot=bot)

    await admin_handler.feedback_admin_reply_receive(adm_msg, adm_state)

    # Verify DB
    async with setup_env["session_factory"]() as session:
        msg = await session.scalar(select(FeedbackMessage).where(FeedbackMessage.feedback_id == feedback_id, FeedbackMessage.sender_type == "admin"))
        assert msg is not None
        assert msg.media_type == "photo"
        assert msg.media_file_id == "photo_file_xyz_123"
        assert msg.text == "Check this screenshot"

    # Verify client received send_photo with photo_file_xyz_123
    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.args[0] == client_id
    assert bot.send_photo.await_args.args[1] == "photo_file_xyz_123"
    assert "Check this screenshot" in bot.send_photo.await_args.kwargs["caption"]
    assert f"feedback_reply_{feedback_id}" in bot.send_photo.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data

    # 2. Client sends video reply to admins
    client_state = MockFSM(initial_data={"client_reply_feedback_id": feedback_id}, initial_state=start_handler.FeedbackStates.waiting_reply)
    video_obj = SimpleNamespace(file_id="video_file_abc_789")
    client_msg = make_mock_message(client_id, video=video_obj, caption="Here is what happens", bot=bot)

    await start_handler.feedback_client_reply_receive(client_msg, client_state)

    # Verify DB
    async with setup_env["session_factory"]() as session:
        msg_c = await session.scalar(select(FeedbackMessage).where(FeedbackMessage.feedback_id == feedback_id, FeedbackMessage.sender_type == "user"))
        assert msg_c is not None
        assert msg_c.media_type == "video"
        assert msg_c.media_file_id == "video_file_abc_789"
        assert msg_c.text == "Here is what happens"

    # Verify admin received send_video with video_file_abc_789
    bot.send_video.assert_awaited_once()
    assert bot.send_video.await_args.args[0] == admin_id
    assert bot.send_video.await_args.args[1] == "video_file_abc_789"
    assert "Here is what happens" in bot.send_video.await_args.kwargs["caption"]
    assert f"feedback_admin_reply_{feedback_id}" in bot.send_video.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data


async def test_j_admin_profile_feedback_count_listing_and_thread_view(setup_env):
    """J. Admin profile: shows correct feedback count, lists user's threads, opens thread in chronological order."""
    admin_id = setup_env["admin_id"]
    client_id = 111011

    # Create user and 2 feedback threads with messages
    async with setup_env["session_factory"]() as session:
        user = User(telegram_id=client_id, username="targetuser", full_name="Target User")
        session.add(user)
        await session.flush()

        fb1 = Feedback(telegram_id=client_id, text="First thread issue", user_id=user.id)
        fb2 = Feedback(telegram_id=client_id, text="Second thread issue", user_id=user.id)
        session.add_all([fb1, fb2])
        await session.flush()

        m1 = FeedbackMessage(feedback_id=fb1.id, sender_type="admin", sender_telegram_id=admin_id, text="Admin reply 1")
        m2 = FeedbackMessage(feedback_id=fb1.id, sender_type="user", sender_telegram_id=client_id, text="Client reply 2")
        session.add_all([m1, m2])
        await session.commit()
        fb1_id, fb2_id = fb1.id, fb2.id

    bot = make_mock_bot()

    # Part 1: Admin user card shows [💬 Обращения · 2]
    msg = make_mock_message(admin_id, bot=bot)
    await admin_handler._show_user_info(msg, user)

    msg.answer.assert_awaited_once()
    card_kb = msg.answer.await_args.kwargs.get("reply_markup")
    assert card_kb is not None
    fb_button = next((b for row in card_kb.inline_keyboard for b in row if f"adm_usr_fb_{client_id}_1" in b.callback_data), None)
    assert fb_button is not None
    assert "💬 Обращения · 2" in fb_button.text

    # Part 2: Open user feedbacks list
    cb_list = make_mock_callback(admin_id, f"adm_usr_fb_{client_id}_1", bot=bot)
    await admin_handler.admin_user_feedbacks(cb_list)

    cb_list.message.edit_text.assert_awaited_once()
    list_text = cb_list.message.edit_text.await_args.args[0]
    list_kb = cb_list.message.edit_text.await_args.kwargs["reply_markup"]

    assert "Обращения клиента" in list_text
    assert "Всего обращений: <b>2</b>" in list_text

    list_buttons = [b for row in list_kb.inline_keyboard for b in row]
    thread_btn_1 = next((b for b in list_buttons if b.callback_data == f"adm_fb_view_{fb1_id}_1"), None)
    thread_btn_2 = next((b for b in list_buttons if b.callback_data == f"adm_fb_view_{fb2_id}_1"), None)
    assert thread_btn_1 is not None
    assert thread_btn_2 is not None

    # Part 3: Open thread 1
    cb_view = make_mock_callback(admin_id, f"adm_fb_view_{fb1_id}_1", bot=bot)
    await admin_handler.admin_feedback_view_thread(cb_view)

    cb_view.message.edit_text.assert_awaited_once()
    view_text = cb_view.message.edit_text.await_args.args[0]
    view_kb = cb_view.message.edit_text.await_args.kwargs["reply_markup"]

    # Renders in chronological order: root -> m1 -> m2
    assert f"Обращение #{fb1_id}" in view_text
    idx_root = view_text.find("First thread issue")
    idx_m1 = view_text.find("Admin reply 1")
    idx_m2 = view_text.find("Client reply 2")

    assert idx_root != -1
    assert idx_m1 != -1
    assert idx_m2 != -1
    assert idx_root < idx_m1 < idx_m2

    view_buttons = [b for row in view_kb.inline_keyboard for b in row]
    reply_btn = next((b for b in view_buttons if b.callback_data == f"feedback_admin_reply_{fb1_id}"), None)
    back_btn = next((b for b in view_buttons if b.callback_data == f"adm_usr_fb_{client_id}_1"), None)

    assert reply_btn is not None
    assert back_btn is not None


async def test_k_long_history_does_not_exceed_telegram_limit_or_crash(setup_env):
    """K. Long history does not exceed Telegram message limit / crash."""
    admin_id = setup_env["admin_id"]
    client_id = 111012

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=client_id, text="Initial long thread root message " * 5, user_id=0)
        session.add(fb)
        await session.flush()

        # Add 50 messages, each ~150 chars = ~7500 chars total
        for i in range(50):
            msg = FeedbackMessage(
                feedback_id=fb.id,
                sender_type="user" if i % 2 == 0 else "admin",
                sender_telegram_id=client_id if i % 2 == 0 else admin_id,
                text=f"Message number {i:02d}: " + ("x" * 120),
            )
            session.add(msg)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()
    cb_view = make_mock_callback(admin_id, f"adm_fb_view_{feedback_id}_1", bot=bot)

    # Should not crash!
    await admin_handler.admin_feedback_view_thread(cb_view)

    cb_view.message.edit_text.assert_awaited_once()
    rendered_text = cb_view.message.edit_text.await_args.args[0]

    # Must be safely under Telegram's 4096 limit
    assert len(rendered_text) <= 3800
    assert "... (показаны последние сообщения, более ранние скрыты)" in rendered_text
    # Most recent message must be present
    assert "Message number 49:" in rendered_text


async def test_l_admin_and_client_screenshots_and_edge_cases(setup_env):
    """L. Verify screenshots (photo/document), special HTML characters (<, >) and long captions."""
    admin_id = setup_env["admin_id"]
    client_id = 111013

    async with setup_env["session_factory"]() as session:
        fb = Feedback(telegram_id=client_id, text="Initial question <test>", user_id=0)
        session.add(fb)
        await session.commit()
        feedback_id = fb.id

    bot = make_mock_bot()

    # 1. Admin sends screenshot with HTML chars (<, >) and no crash
    adm_state = MockFSM(initial_data={"admin_reply_feedback_id": feedback_id}, initial_state=admin_handler.AdminStates.waiting_feedback_reply)
    photo_obj = [SimpleNamespace(file_id="screenshot_file_123")]
    adm_msg = make_mock_message(admin_id, photo=photo_obj, caption="Click <Settings> & select VPN", bot=bot)

    await admin_handler.feedback_admin_reply_receive(adm_msg, adm_state)

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.args[0] == client_id
    assert bot.send_photo.await_args.args[1] == "screenshot_file_123"
    caption = bot.send_photo.await_args.kwargs["caption"]
    assert "&lt;Settings&gt;" in caption
    assert bot.send_photo.await_args.kwargs["parse_mode"] == "HTML"

    # 2. Admin sends document (uncompressed image)
    bot.send_photo.reset_mock()
    adm_state2 = MockFSM(initial_data={"admin_reply_feedback_id": feedback_id}, initial_state=admin_handler.AdminStates.waiting_feedback_reply)
    doc_obj = SimpleNamespace(file_id="doc_screenshot_456")
    msg_doc = SimpleNamespace(
        text=None,
        caption="Uncompressed PNG",
        photo=None,
        video=None,
        document=doc_obj,
        from_user=SimpleNamespace(id=admin_id, username="admin", first_name="Admin", full_name="Admin"),
        bot=bot,
        answer=AsyncMock(),
    )
    bot.send_document = AsyncMock()

    await admin_handler.feedback_admin_reply_receive(msg_doc, adm_state2)
    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.args[0] == client_id
    assert bot.send_document.await_args.args[1] == "doc_screenshot_456"

    # 3. Client sends feedback with long caption > 1024 chars
    long_caption = "A" * 1500
    msg_long = SimpleNamespace(
        text=None,
        caption=long_caption,
        photo=photo_obj,
        video=None,
        from_user=SimpleNamespace(id=client_id, username="client", first_name="Client <3", full_name="Client <3>"),
        bot=bot,
        answer=AsyncMock(),
    )
    bot.send_photo.reset_mock()
    state_client = MockFSM()
    await start_handler.feedback_receive(msg_long, state_client)

    bot.send_photo.assert_awaited_once()
    sent_caption = bot.send_photo.await_args.kwargs["caption"]
    assert len(sent_caption) <= 1024
    assert "&lt;3&gt;" in sent_caption
