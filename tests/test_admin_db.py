"""اختبارات طبقة قاعدة البيانات لحزمة إدارة الأعضاء (SPEC4 §1)."""
import pytest
import pytest_asyncio

from bot.db import Database


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "admin_test.db"))
    await database.init()
    yield database
    await database.close()


async def _add_user(db: Database, uid: int, username: str | None = None,
                    first_name: str | None = None) -> None:
    await db.upsert_user(uid, username or f"user{uid}", first_name or f"اسم{uid}")


async def _set_joined(db: Database, uid: int, modifier: str) -> None:
    await db.conn.execute(
        "UPDATE users SET joined_at = datetime('now','localtime', ?) WHERE id = ?",
        (modifier, uid),
    )
    await db.conn.commit()


# 1) بريميوم بمدة: is_premium شغال و premium_until مش NULL
@pytest.mark.asyncio
async def test_set_premium_with_days(db):
    await db.set_premium(101, True, days=7)
    assert await db.is_premium(101) is True
    user = await db.get_user(101)
    assert user["is_premium"] is True
    assert user["premium_until"] is not None


# 2) بريميوم دائم: premium_until يفضل NULL
@pytest.mark.asyncio
async def test_set_premium_permanent(db):
    await db.set_premium(102, True)
    assert await db.is_premium(102) is True
    user = await db.get_user(102)
    assert user["is_premium"] is True
    assert user["premium_until"] is None


# 3) بريميوم منتهي: is_premium False + قائمة المنتهين + expire_premium
@pytest.mark.asyncio
async def test_expired_premium_flow(db):
    await db.set_premium(103, True, days=7)
    await db.set_premium(104, True)  # دائم — ميفضلش ساري
    # خلّي الاشتراك منتهي يدويًا
    await db.conn.execute(
        "UPDATE users SET premium_until = datetime('now','localtime','-1 days') WHERE id = ?",
        (103,),
    )
    await db.conn.commit()

    assert await db.is_premium(103) is False
    assert await db.is_premium(104) is True

    expired = await db.list_expired_premium()
    assert 103 in expired
    assert 104 not in expired

    await db.expire_premium(103)
    assert 103 not in await db.list_expired_premium()
    assert await db.is_premium(103) is False
    user = await db.get_user(103)
    assert user["is_premium"] is False
    assert user["premium_until"] is None


# 4) إلغاء البريميوم: is_premium False و premium_until=NULL
@pytest.mark.asyncio
async def test_set_premium_off(db):
    await db.set_premium(105, True, days=30)
    assert await db.is_premium(105) is True
    await db.set_premium(105, False)
    assert await db.is_premium(105) is False
    user = await db.get_user(105)
    assert user["is_premium"] is False
    assert user["premium_until"] is None


# 5) list_users / count_users بالفلاتر الأربعة + التقليب
@pytest.mark.asyncio
async def test_list_users_filters_and_pagination(db):
    # عادي موافق عليه
    await _add_user(db, 201)
    await db.set_approved(201, True)
    # بريميوم
    await _add_user(db, 202)
    await db.set_approved(202, True)
    await db.set_premium(202, True)
    # محظور
    await _add_user(db, 203)
    await db.set_ban(203, True)
    # معلق
    await _add_user(db, 204)
    # ترتيب انضمام ثابت: 201 أحدث … 204 أقدم
    for uid, mod in ((201, "-1 days"), (202, "-2 days"), (203, "-3 days"), (204, "-4 days")):
        await _set_joined(db, uid, mod)
    # تحميلات لعضو واحد للتأكد من عمود العدّ
    await db.log_download(202, "فيلم", "1080p", "done")
    await db.log_download(202, "مسلسل", "720p", "done")

    assert await db.count_users("all") == 4
    assert await db.count_users("premium") == 1
    assert await db.count_users("banned") == 1
    assert await db.count_users("pending") == 1

    all_users = await db.list_users("all")
    assert [u["id"] for u in all_users] == [201, 202, 203, 204]  # joined_at DESC

    premium = await db.list_users("premium")
    assert [u["id"] for u in premium] == [202]
    assert premium[0]["downloads"] == 2
    assert premium[0]["premium_until"] is None
    assert premium[0]["joined_at"] is not None

    assert [u["id"] for u in await db.list_users("banned")] == [203]
    assert [u["id"] for u in await db.list_users("pending")] == [204]

    # تقليب: صفحة أولى 3 + صفحة تانية فيها الباقي
    page1 = await db.list_users("all", offset=0, limit=3)
    page2 = await db.list_users("all", offset=3, limit=3)
    assert [u["id"] for u in page1] == [201, 202, 203]
    assert [u["id"] for u in page2] == [204]
    assert await db.list_users("all", offset=4, limit=3) == []

    with pytest.raises(ValueError):
        await db.list_users("مش_فلتر")


# 6) all_user_ids للجمهورات الثلاثة
@pytest.mark.asyncio
async def test_all_user_ids_audiences(db):
    await _add_user(db, 301)  # مجاني
    await _add_user(db, 302)  # بريميوم دائم
    await db.set_premium(302, True)
    await _add_user(db, 303)  # بريميوم مؤقت ساري
    await db.set_premium(303, True, days=10)
    await _add_user(db, 304)  # بريميوم منتهي
    await db.set_premium(304, True, days=10)
    await db.conn.execute(
        "UPDATE users SET premium_until = datetime('now','localtime','-1 days') WHERE id = ?",
        (304,),
    )
    await db.conn.commit()

    assert sorted(await db.all_user_ids()) == [301, 302, 303, 304]
    assert sorted(await db.all_user_ids("all")) == [301, 302, 303, 304]
    assert sorted(await db.all_user_ids("premium")) == [302, 303]
    assert sorted(await db.all_user_ids("free")) == [301, 304]


# 7) top_users / recent_downloads / top_titles / count_new_users
@pytest.mark.asyncio
async def test_stats_queries(db):
    await _add_user(db, 401, "tarek", "طارق")
    await _add_user(db, 402, "mona", "منى")
    await _add_user(db, 403, "zero", "صفر")  # من غير تحميلات
    # 403 انضم قديم (10 أيام)، الباقي جداد
    await _set_joined(db, 403, "-10 days")

    await db.log_download(401, "فيلم أ", "1080p", "done", site="akwam")
    await db.log_download(401, "فيلم أ", "720p", "done", site="starcima")
    await db.log_download(401, "فيلم ب", "1080p", "done", site="akwam")
    await db.log_download(402, "فيلم أ", "1080p", "failed", site="akwam")
    await db.log_download(402, "فيلم ج", "480p", "done", site="moviebox")
    await db.log_download(999, "فيلم مجهول", "HD", "done")  # مستخدم مش مسجل

    top = await db.top_users(10)
    assert [(u["id"], u["downloads"]) for u in top] == [(401, 3), (402, 2)]
    assert top[0]["username"] == "tarek"
    assert all(u["downloads"] > 0 for u in top)
    assert 403 not in [u["id"] for u in top]

    titles = await db.top_titles(10)
    assert titles[0] == ("فيلم أ", 2)  # الناجح بس (الفاشل ميتحسبش)
    assert ("فيلم ب", 1) in titles
    assert ("فيلم ج", 1) in titles
    assert all(isinstance(t, tuple) and len(t) == 2 for t in titles)

    recent = await db.recent_downloads(20)
    assert len(recent) == 6
    first = recent[0]
    assert set(first) == {
        "user_id", "username", "first_name", "title",
        "quality", "status", "site", "created_at",
    }
    assert first["user_id"] == 999  # آخر واحد اتسجل
    assert first["username"] is None  # LEFT JOIN لمستخدم مجهول
    assert recent[-1]["title"] == "فيلم أ"  # أول واحد اتسجل
    assert [r["created_at"] for r in recent] == sorted(
        [r["created_at"] for r in recent], reverse=True
    )

    assert await db.count_new_users(1) == 2
    assert await db.count_new_users(7) == 2
    assert await db.count_new_users(30) == 3


# 8) list_pending / count_pending مع التقليب
@pytest.mark.asyncio
async def test_list_pending_pagination(db):
    for i in range(15):  # 15 معلق
        await _add_user(db, 500 + i)
    await _add_user(db, 600)  # موافق عليه — مش معلق
    await db.set_approved(600, True)
    await _add_user(db, 601)  # محظور — مش معلق
    await db.set_ban(601, True)

    assert await db.count_pending() == 15

    page1 = await db.list_pending(offset=0, limit=10)
    page2 = await db.list_pending(offset=10, limit=10)
    assert len(page1) == 10
    assert len(page2) == 5
    ids = [u["id"] for u in page1 + page2]
    assert sorted(ids) == [500 + i for i in range(15)]
    assert 600 not in ids and 601 not in ids
    # القيم الافتراضية توافقية: 10 عناصر من أول صفحة
    assert len(await db.list_pending()) == 10
