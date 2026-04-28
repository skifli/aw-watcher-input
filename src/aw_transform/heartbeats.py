from datetime import timedelta


def _to_timedelta(d):
    if d is None:
        return timedelta(0)
    if isinstance(d, timedelta):
        return d
    try:
        return timedelta(seconds=float(d))
    except Exception:
        return timedelta(0)


def heartbeat_merge(hb1, hb2, pulsetime):
    if hb1 is None or hb2 is None:
        return None

    if hb1.data != hb2.data:
        return None

    d1 = _to_timedelta(hb1.duration)
    d2 = _to_timedelta(hb2.duration)

    hb1_end = hb1.timestamp + d1
    hb2_end = hb2.timestamp + d2

    # If the heartbeats overlap or touch, merge them
    if hb2.timestamp <= hb1_end:
        total = max(hb2_end - hb1.timestamp, d1)
        merged = hb1.__class__(
            timestamp=hb1.timestamp,
            duration=total,
            data=hb1.data,
            id=hb1.id,
        )
        return merged

    # If the gap between them is less than or equal to pulsetime, merge
    if (hb2.timestamp - hb1_end).total_seconds() <= float(pulsetime):
        total = hb2_end - hb1.timestamp
        merged = hb1.__class__(
            timestamp=hb1.timestamp,
            duration=total,
            data=hb1.data,
            id=hb1.id,
        )
        return merged

    return None


def heartbeat_reduce(events, pulsetime):
    if not events:
        return []

    reduced = [events[0]]
    for event in events[1:]:
        last = reduced[-1]
        merged = heartbeat_merge(last, event, pulsetime)
        if merged is None:
            reduced.append(event)
        else:
            reduced[-1] = merged
    return reduced
