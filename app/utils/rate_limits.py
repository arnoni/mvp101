from upstash_redis.asyncio import Redis


async def check_magic_link_rate_limit(
    redis: Redis,
    email: str,
    ip: str,
) -> bool:
    """Returns True if request is within limits, False if rate-limited."""
    cooldown_key = f"magic_resend:cooldown:{email}"
    count_key = f"magic_resend:count:{email}:{ip}"
    ip_count_key = f"magic_ip_limit:{ip}"

    ip_count = await redis.incr(ip_count_key)
    if ip_count == 1:
        await redis.expire(ip_count_key, 3600)
    if int(ip_count) > 10:
        return False

    if await redis.get(cooldown_key):
        return False

    count = await redis.incr(count_key)
    if count == 1:
        await redis.expire(count_key, 180)
    if int(count) > 2:
        await redis.set(cooldown_key, "1", ex=180)
        return False

    return True
