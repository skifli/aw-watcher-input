from datetime import timedelta


def heartbeat_merge(hb1, hb2, pulsetime):
    if hb1 is None or hb2 is None:
        return None

    if hb1.data != hb2.data:
        return None

    hb1_end = hb1.timestamp + timedelta(seconds=hb1.duration)
    hb2_end = hb2.timestamp + timedelta(seconds=hb2.duration)

    if hb2.timestamp <= hb1_end:
        merged = hb1.__class__(
            timestamp=hb1.timestamp,
            duration=max((hb2_end - hb1.timestamp).total_seconds(), hb1.duration),
            data=hb1.data,
            id=hb1.id,
        )
        return merged

    if (hb2.timestamp - hb1_end).total_seconds() <= pulsetime:
        merged = hb1.__class__(
            timestamp=hb1.timestamp,
            duration=(hb2_end - hb1.timestamp).total_seconds(),
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
