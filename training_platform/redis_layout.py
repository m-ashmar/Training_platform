"""Where each Redis role lives, and which layouts production refuses to boot on.

Four roles use Redis, and each takes its own full URL. Unset, a role falls back to one
database of `REDIS_URL`, which is the layout this project always had, so a single Redis
the owner runs himself needs nothing but `REDIS_URL`:

| role | its own URL | default on `REDIS_URL` |
|---|---|---|
| the five caches: sessions, rate limits, public, private, Edamam | `REDIS_CACHE_URL` | `/0` to `/4` |
| the WebSocket channel layer | `REDIS_CHANNELS_URL` | `/5` |
| the Celery broker (the job queue) | `CELERY_BROKER_URL` | `/6` |
| Celery results | `CELERY_RESULT_BACKEND` | `/7` |

Why a URL per role: many managed Redis services serve database 0 only, and some are
several small instances, not one big one. The old layout assumed eight databases of one
server, and the boot check refused a broker on `/0`-`/5` of ANY host, so no broker on
such a service could ever boot. With `REDIS_CACHE_URL` the five caches share whatever
database it names; their `KEY_PREFIX`es (`tp_session`, `tp_rl`, ...) keep them apart.

What production refuses (`problems`), and why:

- **A broker in the same Redis database as a cache or the channel layer.** A cache is
  flushed (django-redis `cache.clear()` is `FLUSHDB`, and flushing a cache is an ordinary
  thing for an operator to do) and evicted under memory pressure, and either deletes
  every queued job with it: sign-up codes, notifications, renewals. Another database of
  the same server, or another server, is fine.
- **Any role on localhost.** There is no Redis inside the app container.
- **A cache or channel layer URL that is not Redis.** The broker and the result store
  may be any URL Celery takes (a managed RabbitMQ, `amqps://...`); only the localhost
  rule applies to those.
- **A `rediss://` broker or result store without `ssl_cert_reqs`.** kombu then connects
  WITHOUT checking the server's certificate (it logs a warning and uses CERT_NONE), and
  the Redis result backend raises on first use — inside the request that queued a task.
  Add `?ssl_cert_reqs=required` (the provider's certificate is checked).

**The broker's Redis must run with `maxmemory-policy noeviction`.** Eviction is decided
per Redis INSTANCE, not per database: a broker on `/6` of a server whose caches evict
(`allkeys-lru`, `volatile-lru`, ...) loses queued jobs exactly as if it shared their
database. So either the whole shared server is `noeviction` (the caches then live within
`maxmemory` on their TTLs), or the broker gets a Redis of its own that is. Nothing here
can check that — `CONFIG GET` is disabled on most managed services — so it is on the
runbook: `redis-cli -u "$CELERY_BROKER_URL" INFO memory | grep maxmemory_policy`.

Only the standard library: `settings_base` imports this, and so does the Docker build's
`settings_build` through it.
"""
from urllib.parse import parse_qs, urlsplit, urlunsplit

DEFAULT_PORT = 6379
_REDIS_SCHEMES = {"redis", "rediss", "unix"}
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def with_database(base_url, database):
    """`base_url` pointed at `database`: in the PATH, before any `?options`.

    Appending `/6` to a URL that carries options (`rediss://h:6379?ssl_cert_reqs=...`)
    wrote the database into the option's value. A unix socket takes it as `?db=`.
    """
    parts = urlsplit(base_url.strip())
    if parts.scheme.lower() == "unix":
        query = {k: v for k, v in parse_qs(parts.query).items() if k != "db"}
        pairs = [f"{k}={v[-1]}" for k, v in query.items()] + [f"db={int(database)}"]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(pairs),
                           parts.fragment))
    return urlunsplit((parts.scheme, parts.netloc, f"/{int(database)}", parts.query,
                       parts.fragment))


def names_database(url):
    """Whether `url` already says which database it means."""
    parts = urlsplit((url or "").strip())
    if "db" in parse_qs(parts.query):
        return True
    return parts.scheme.lower() != "unix" and parts.path.strip("/") != ""


def endpoint(url):
    """`(host, port, database)` — what identifies one Redis keyspace — or None when
    `url` is not a Redis URL.

    Credentials, TLS and options do not make another database: `redis://H` and
    `rediss://user:pw@h:6379/0` are the same one. A `?db=` option wins over the path, as
    it does in redis-py.
    """
    parts = urlsplit((url or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in _REDIS_SCHEMES:
        return None
    query = parse_qs(parts.query)
    try:
        if scheme == "unix":
            host, port = f"unix:{parts.path}", None
            database = (query.get("db") or ["0"])[-1]
        else:
            host, port = (parts.hostname or "").lower(), parts.port or DEFAULT_PORT
            database = (query.get("db") or [parts.path.strip("/") or "0"])[-1]
        return host, port, int(database)
    except ValueError:            # a port or database that is not a number
        return None


def describe(url):
    """Where `url` points, WITHOUT its credentials — for messages and logs."""
    found = endpoint(url)
    if found is None:
        return "an unreadable URL"
    host, port, database = found
    return f"{host}:{port}/{database}" if port else f"{host}?db={database}"


def is_local(url):
    """Whether `url` points inside the machine it is read on — a Redis URL, or any other
    `scheme://host` a Celery broker or result store may be (amqp://, sqs://, ...)."""
    found = endpoint(url)
    if found is not None:
        return found[0] in _LOCAL_HOSTS or found[0].startswith("unix:")
    try:
        host = (urlsplit((url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS


def problems(roles, broker):
    """Why this Redis layout cannot run production — a list of messages, empty if none.

    `roles` is `[(variable, what, url, consequence), ...]` for every Redis the app uses,
    the broker and result store included: `variable` is the setting to change, `what`
    names the role, `consequence` says what breaks when it cannot connect. `broker` is
    the broker's URL; it is compared against every role that is not Celery's own
    (`variable` not starting "CELERY_") — the caches and the channel layer.
    """
    found = []
    for variable, what, url, consequence in roles:
        # Celery is not tied to Redis: a managed RabbitMQ (amqps://) broker is a
        # reasonable choice, so only the caches and the channel layer must be Redis.
        celery = variable.startswith("CELERY_")
        if not url:
            found.append(f"{variable} is empty — {what} has no Redis; {consequence}")
        elif endpoint(url) is None and not celery:
            found.append(f"{variable} is not a redis://, rediss:// or unix:// URL — "
                         f"{what} cannot connect; {consequence}")
        elif is_local(url):
            found.append(f"{variable} points {what} at localhost ({describe(url)}); "
                         f"there is no Redis inside the app container — {consequence}")
        elif (celery
              and urlsplit(url).scheme.lower() == "rediss"
              and "ssl_cert_reqs" not in parse_qs(urlsplit(url).query)):
            found.append(
                f"{variable} is rediss:// without ssl_cert_reqs — Celery would either "
                "skip checking the server's certificate or fail on first use; add "
                "?ssl_cert_reqs=required")

    queue = endpoint(broker)
    sharing = [what for variable, what, url, _c in roles
               if not variable.startswith("CELERY_") and endpoint(url) == queue]
    if queue is not None and sharing:
        found.append(
            f"CELERY_BROKER_URL is the same Redis database as {', '.join(sharing)} "
            f"({describe(broker)}). A cache is flushed (cache.clear() is FLUSHDB) and "
            "evicted under memory pressure, and either would delete every queued job. "
            "Give the broker its own database or its own Redis — and whichever Redis "
            "holds it must run with maxmemory-policy noeviction")
    return found
