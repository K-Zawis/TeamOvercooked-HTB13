"""Atomic, deduplicated depth staging for redis-py-compatible clients."""


COMMIT_DEPTH_RESULT = """
-- KEYS: depth hash, metadata hash, task fingerprints, completed slice set.
-- ARGV: stable task fingerprint, slice token, global_y/depth string pairs.
if #KEYS ~= 4 or #ARGV < 2 or (#ARGV % 2) ~= 0 then
    return redis.error_reply('invalid depth completion arguments')
end
if redis.call('SISMEMBER', KEYS[3], ARGV[1]) == 1 then
    return 0
end
-- New work must belong to the active dispatch. Exact fingerprint replays above
-- remain read-only even after the merge barrier has closed.
if redis.call('HGET', KEYS[2], 'status') ~= 'PROCESSING' then
    return redis.error_reply('depth staging requires PROCESSING status')
end
local expected = string.gsub(KEYS[4], ':completed$', ':all')
if redis.call('SISMEMBER', expected, ARGV[2]) ~= 1 then
    return redis.error_reply('unexpected depth slice')
end
if redis.call('SISMEMBER', KEYS[4], ARGV[2]) == 1 then
    return redis.error_reply('depth slice already staged under another fingerprint')
end
-- Lua does not roll back runtime errors. Check types and counter range before
-- writing anything, so malformed staging data cannot cause a partial commit.
local depth_type = redis.call('TYPE', KEYS[1]).ok
if depth_type ~= 'none' and depth_type ~= 'hash' then
    return redis.error_reply('depth results key must be a hash')
end
local slices_type = redis.call('TYPE', KEYS[4]).ok
if slices_type ~= 'none' and slices_type ~= 'set' then
    return redis.error_reply('completed slices key must be a set')
end
local canonical = redis.call('HGET', KEYS[2], 'completed_depth_chunks')
local legacy = redis.call('HGET', KEYS[2], 'completed_depth')
if canonical and legacy and canonical ~= legacy then
    return redis.error_reply('depth completion counters disagree')
end
local counter = canonical or legacy
if counter then
    if counter ~= '0' and not string.match(counter, '^[1-9][0-9]*$') then
        return redis.error_reply('completed_depth must be a nonnegative integer')
    end
    if #counter > 19 or (#counter == 19 and counter >= '9223372036854775807') then
        return redis.error_reply('completed_depth would overflow')
    end
end
-- Bound unpack's argument count even if a supplied strip has many labels.
for first = 3, #ARGV, 256 do
    redis.call('HSET', KEYS[1], unpack(ARGV, first, math.min(first + 255, #ARGV)))
end
if not canonical then
    redis.call('HSET', KEYS[2], 'completed_depth_chunks', counter or '0')
end
redis.call('SADD', KEYS[4], ARGV[2])
redis.call('HINCRBY', KEYS[2], 'completed_depth_chunks', 1)
redis.call('HSET', KEYS[2], 'completed_depth',
           redis.call('HGET', KEYS[2], 'completed_depth_chunks'))
redis.call('SADD', KEYS[3], ARGV[1])
return 1
"""


def staging_keys(log_id):
    prefix = f"log:{log_id}"
    return f"{prefix}:depths", f"{prefix}:meta", f"{prefix}:depth_completed_tasks"


def commit_depth_result(client, log_id, task_id, depths, *, slice_bounds=None):
    """Return whether this logical task was newly committed, even if empty."""
    pairs = [value for y, depth in sorted(depths.items())
             for value in (str(y), repr(float(depth)))]
    token = f"{slice_bounds[0]}:{slice_bounds[1]}" if slice_bounds is not None else task_id
    return bool(client.eval(COMMIT_DEPTH_RESULT, 4, *staging_keys(log_id),
                            f"log:{log_id}:slices:depth:completed", task_id, token, *pairs))
