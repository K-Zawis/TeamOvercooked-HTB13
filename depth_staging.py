"""Atomic, deduplicated depth staging for redis-py-compatible clients."""


COMMIT_DEPTH_RESULT = """
-- KEYS: depth hash, metadata hash, completed-task set.
-- ARGV: stable task fingerprint, followed by global_y/depth string pairs.
if #KEYS ~= 3 or #ARGV < 1 or (#ARGV % 2) ~= 1 then
    return redis.error_reply('invalid depth completion arguments')
end
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then
    return 0
end
-- Lua does not roll back runtime errors. Check types and counter range before
-- writing anything, so malformed staging data cannot cause a partial commit.
local depth_type = redis.call('TYPE', KEYS[1]).ok
if depth_type ~= 'none' and depth_type ~= 'hash' then
    return redis.error_reply('depth results key must be a hash')
end
local counter = redis.call('HGET', KEYS[2], 'completed_depth')
if counter then
    if counter ~= '0' and not string.match(counter, '^[1-9][0-9]*$') then
        return redis.error_reply('completed_depth must be a nonnegative integer')
    end
    if #counter > 19 or (#counter == 19 and counter >= '9223372036854775807') then
        return redis.error_reply('completed_depth would overflow')
    end
end
-- Bound unpack's argument count even if a supplied strip has many labels.
for first = 2, #ARGV, 256 do
    redis.call('HSET', KEYS[1], unpack(ARGV, first, math.min(first + 255, #ARGV)))
end
redis.call('HINCRBY', KEYS[2], 'completed_depth', 1)
redis.call('SADD', KEYS[3], ARGV[1])
return 1
"""


def staging_keys(log_id):
    prefix = f"log:{log_id}"
    return f"{prefix}:depths", f"{prefix}:meta", f"{prefix}:depth_completed_tasks"


def commit_depth_result(client, log_id, task_id, depths):
    """Return whether this logical task was newly committed, even if empty."""
    pairs = [value for y, depth in sorted(depths.items())
             for value in (str(y), repr(float(depth)))]
    return bool(client.eval(COMMIT_DEPTH_RESULT, 3, *staging_keys(log_id), task_id, *pairs))
