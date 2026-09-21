from providerharvest_service.engine.transport.rate_limit import TokenBucket


def test_allows_burst_up_to_capacity():
    clock = {"t": 0.0}
    sleeps = []
    bucket = TokenBucket(5, clock=lambda: clock["t"], sleep=sleeps.append)

    for _ in range(5):
        bucket.acquire()

    assert sleeps == []  # first 5 are free (full initial bucket)


def test_blocks_and_waits_once_exhausted():
    clock = {"t": 0.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    bucket = TokenBucket(60, clock=lambda: clock["t"], sleep=fake_sleep)  # 1/sec refill

    for _ in range(60):
        bucket.acquire()
    assert sleeps == []

    bucket.acquire()  # 61st call must wait for a refill
    assert len(sleeps) == 1
    assert sleeps[0] > 0
