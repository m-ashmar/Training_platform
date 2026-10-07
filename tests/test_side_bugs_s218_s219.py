"""SIDE_BUGS S-218 and S-219.

* S-218 — the AI chat's messages paged by `-created_at` alone, and the post list's
  `ordering = ['-created_at']` (and `?ordering=likes_count`, on comments too) REPLACED
  the models' total order: rows sharing a value repeated or vanished across pages.
* S-219 — a post's and a comment's `content` had no cap but the 15 MB request body,
  and the comment list read the caller's blocks twice (one query each).
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from tests.test_backend_bugs_app_0924_ai import _walk
from tests.test_fx_people import _ai_subscriber

pytestmark = pytest.mark.django_db

JSON = "application/json"


@pytest.fixture(autouse=True)
def a_cold_cache():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def _order_by(queries, table):
    """The ORDER BY of the request's SELECT on `table` (its page query)."""
    for query in queries:
        sql = query["sql"]
        if sql.startswith("SELECT") and f'FROM "{table}"' in sql and "ORDER BY" in sql:
            return sql.rsplit("ORDER BY", 1)[1]
    raise AssertionError(f"no ordered SELECT on {table}")


def _pages(client, url):
    """Every row of a page-numbered list, following `next`."""
    rows = []
    while url:
        page = client.get(url)
        assert page.status_code == 200, page.content
        rows += page.json()["results"]
        url = page.json()["next"]
        assert len(rows) < 500
    return rows


# ------------------------------------------------------------------------ S-218
def test_s218_posts_page_by_a_total_order_whatever_the_ordering(make_user, api):
    from social.models import Post

    author = make_user("s218_author")
    reader = make_user("s218_reader")
    same = timezone.now() - timedelta(hours=1)
    posts = [Post.objects.create(author=author, content=f"s218 post {n}",
                                 visibility="public") for n in range(7)]
    Post.objects.filter(pk__in=[p.pk for p in posts]).update(created_at=same,
                                                             likes_count=0)
    newest_first = sorted(p.pk for p in posts)[::-1]
    client = api(reader)

    for query in ("", "&ordering=likes_count", "&ordering=-likes_count"):
        walked = [row["id"] for row in _pages(
            client, f"/api/social/posts/?author={author.pk}&page_size=2{query}")]
        assert walked == newest_first, (query, walked)

    with CaptureQueriesContext(connection) as seen:
        client.get(f"/api/social/posts/?author={author.pk}&ordering=likes_count")
    assert '"social_post"."id" DESC' in _order_by(seen.captured_queries, "social_post")


def test_s218_comments_ordered_by_likes_keep_the_id_tiebreak(make_user, api):
    from social.models import Comment, Post

    author = make_user("s218c_author")
    post = Post.objects.create(author=author, content="s218 thread", visibility="public")
    comments = [Comment.objects.create(post=post, author=author, content=f"c{n}")
                for n in range(5)]
    Comment.objects.filter(pk__in=[c.pk for c in comments]).update(
        created_at=timezone.now() - timedelta(minutes=3), likes_count=0)
    client = api(author)

    walked = [row["id"] for row in _walk(
        client, f"/api/social/comments/?post={post.pk}&ordering=likes_count&page_size=2")]
    assert walked == sorted(c.pk for c in comments)[::-1]
    with CaptureQueriesContext(connection) as seen:
        client.get(f"/api/social/comments/?post={post.pk}&ordering=likes_count")
    assert '"social_comment"."id" DESC' in _order_by(seen.captured_queries,
                                                       "social_comment")


def test_s218_ai_messages_sharing_a_timestamp_page_each_once(make_user, api):
    from ai_assistant.models import ChatMessage, ChatSession

    person = _ai_subscriber(make_user, "s218ai")
    chat = ChatSession.objects.create(user=person, title="s218")
    rows = [ChatMessage.objects.create(session=chat, role="user" if n % 2 else
                                       "assistant", content=f"m{n}") for n in range(5)]
    ChatMessage.objects.filter(pk__in=[m.pk for m in rows]).update(
        created_at=timezone.now() - timedelta(minutes=1))
    client = api(person)

    url = f"/api/ai/sessions/{chat.session_id}/messages/"
    walked = [row["id"] for row in _walk(client, f"{url}?page_size=2")]
    assert walked == sorted(m.pk for m in rows)[::-1]
    with CaptureQueriesContext(connection) as seen:
        client.get(url)
    assert '"ai_assistant_chatmessage"."id" DESC' in _order_by(
        seen.captured_queries, "ai_assistant_chatmessage")


# ------------------------------------------------------------------------ S-219
def test_s219_a_post_and_a_comment_have_a_length_cap(make_user, api):
    from social.models import COMMENT_CONTENT_MAX_CHARS, POST_CONTENT_MAX_CHARS, Post

    assert (POST_CONTENT_MAX_CHARS, COMMENT_CONTENT_MAX_CHARS) == (5000, 2000)
    writer = make_user("s219_writer")
    client = api(writer)

    long_post = client.post("/api/social/posts/", {
        "content": "a" * (POST_CONTENT_MAX_CHARS + 1), "visibility": "public"},
        content_type=JSON)
    assert long_post.status_code == 400, long_post.content
    assert long_post.json()["field_errors"]["content"][0]["code"] == "content_too_long"
    assert not Post.objects.filter(author=writer).exists()
    post = client.post("/api/social/posts/", {
        "content": "a" * POST_CONTENT_MAX_CHARS, "visibility": "public"},
        content_type=JSON)
    assert post.status_code == 201, post.content

    # An edit is capped as well.
    edited = client.patch(f"/api/social/posts/{post.json()['id']}/",
                          {"content": "a" * (POST_CONTENT_MAX_CHARS + 1)},
                          content_type=JSON)
    assert edited.status_code == 400, edited.content
    assert edited.json()["field_errors"]["content"][0]["code"] == "content_too_long"

    long_comment = client.post("/api/social/comments/", {
        "post": post.json()["id"], "content": "a" * (COMMENT_CONTENT_MAX_CHARS + 1)},
        content_type=JSON)
    assert long_comment.status_code == 400, long_comment.content
    assert long_comment.json()["field_errors"]["content"][0]["code"] == \
        "content_too_long"
    comment = client.post("/api/social/comments/", {
        "post": post.json()["id"], "content": "a" * COMMENT_CONTENT_MAX_CHARS},
        content_type=JSON)
    assert comment.status_code == 201, comment.content


def test_s219_the_comment_list_reads_the_blocks_once(make_user, api):
    from cachalot.api import cachalot_disabled

    from moderation.models import Block
    from social.models import Comment, Post

    reader = make_user("s219b_reader")
    author = make_user("s219b_author")
    pest = make_user("s219b_pest")
    Block.objects.create(blocker=reader, blocked=pest)
    post = Post.objects.create(author=author, content="s219 thread", visibility="public")
    Comment.objects.create(post=post, author=author, content="visible")
    Comment.objects.create(post=post, author=pest, content="not to the reader")
    client = api(reader)

    for url, table in ((f"/api/social/comments/?post={post.pk}", "social_comment"),
                       (f"/api/social/posts/?author={author.pk}", "social_post")):
        with cachalot_disabled(), CaptureQueriesContext(connection) as seen:
            listed = client.get(url)
        assert listed.status_code == 200, listed.content
        block_reads = [q for q in seen.captured_queries
                       if 'FROM "moderation_block"' in q["sql"]]
        assert len(block_reads) == 1, (url, len(block_reads))
    assert [row["content"] for row in client.get(
        f"/api/social/comments/?post={post.pk}").json()["results"]] == ["visible"]
