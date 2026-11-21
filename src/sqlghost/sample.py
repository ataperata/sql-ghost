"""Generate the bundled sample database: a fictional toll road operator.

Four toll corridors, a dozen plazas, a few hundred customer accounts and about
twelve thousand trips over the first half of 2026. Every name, plate and email
is made up. The generator is seeded, so the same file comes out every time.

    python -m sqlghost.sample path/to/tolls.sqlite
"""

from __future__ import annotations

import datetime as dt
import random
import sqlite3
import sys
from contextlib import suppress
from pathlib import Path

SEED = 2026

DDL = [
    """CREATE TABLE plazas (
    plaza_id   INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL UNIQUE,
    corridor   TEXT    NOT NULL CHECK (corridor IN ('Cascade Express', 'Harbor Tunnel',
                                                     'Lakeside Bridge', 'Valley Connector')),
    direction  TEXT    NOT NULL CHECK (direction IN ('NB', 'SB', 'EB', 'WB')),
    lanes      INTEGER NOT NULL CHECK (lanes BETWEEN 1 AND 6),
    opened_on  DATE    NOT NULL
)""",
    """CREATE TABLE accounts (
    account_id    INTEGER PRIMARY KEY,
    email         TEXT     NOT NULL UNIQUE,
    holder_name   TEXT     NOT NULL,
    account_type  TEXT     NOT NULL CHECK (account_type IN ('prepaid', 'pay_by_mail', 'fleet')),
    status        TEXT     NOT NULL CHECK (status IN ('active', 'suspended', 'closed')),
    home_state    TEXT     NOT NULL,
    opened_at     DATETIME NOT NULL,
    balance_cents INTEGER  NOT NULL DEFAULT 0
)""",
    """CREATE TABLE vehicles (
    vehicle_id      INTEGER PRIMARY KEY,
    account_id      INTEGER REFERENCES accounts (account_id),
    plate           TEXT    NOT NULL UNIQUE,
    plate_state     TEXT    NOT NULL,
    vehicle_class   INTEGER NOT NULL CHECK (vehicle_class BETWEEN 1 AND 5),
    has_transponder INTEGER NOT NULL CHECK (has_transponder IN (0, 1))
)""",
    """CREATE TABLE rates (
    rate_id       INTEGER PRIMARY KEY,
    plaza_id      INTEGER NOT NULL REFERENCES plazas (plaza_id),
    vehicle_class INTEGER NOT NULL CHECK (vehicle_class BETWEEN 1 AND 5),
    period        TEXT    NOT NULL CHECK (period IN ('peak', 'offpeak', 'overnight')),
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    UNIQUE (plaza_id, vehicle_class, period)
)""",
    """CREATE TABLE trips (
    trip_id    INTEGER PRIMARY KEY,
    vehicle_id INTEGER  NOT NULL REFERENCES vehicles (vehicle_id),
    plaza_id   INTEGER  NOT NULL REFERENCES plazas (plaza_id),
    passed_at  DATETIME NOT NULL,
    lane       INTEGER  NOT NULL,
    period     TEXT     NOT NULL CHECK (period IN ('peak', 'offpeak', 'overnight')),
    read_type  TEXT     NOT NULL CHECK (read_type IN ('transponder', 'plate_ocr', 'manual_review')),
    toll_cents INTEGER  NOT NULL CHECK (toll_cents >= 0),
    status     TEXT     NOT NULL CHECK (status IN ('posted', 'pending', 'disputed', 'written_off'))
)""",
    """CREATE TABLE payments (
    payment_id   INTEGER PRIMARY KEY,
    account_id   INTEGER  NOT NULL REFERENCES accounts (account_id),
    paid_at      DATETIME NOT NULL,
    amount_cents INTEGER  NOT NULL CHECK (amount_cents > 0),
    method       TEXT     NOT NULL CHECK (method IN ('card', 'ach', 'cash'))
)""",
    """CREATE TABLE disputes (
    dispute_id INTEGER PRIMARY KEY,
    trip_id    INTEGER  NOT NULL UNIQUE REFERENCES trips (trip_id),
    opened_at  DATETIME NOT NULL,
    reason     TEXT     NOT NULL CHECK (reason IN ('wrong_plate', 'duplicate_charge', 'rate_error', 'vehicle_sold')),
    outcome    TEXT     CHECK (outcome IN ('upheld', 'reversed'))
)""",
    "CREATE INDEX idx_trips_plaza ON trips (plaza_id)",
    "CREATE INDEX idx_trips_vehicle ON trips (vehicle_id)",
    "CREATE INDEX idx_trips_passed_at ON trips (passed_at)",
    "CREATE INDEX idx_vehicles_account ON vehicles (account_id)",
    "CREATE INDEX idx_payments_account ON payments (account_id)",
]

PLAZAS = [
    ("North Gate", "Cascade Express", "NB", 4, "2015-09-27"),
    ("South Gate", "Cascade Express", "SB", 4, "2015-09-27"),
    ("Canyon Ramp", "Cascade Express", "NB", 2, "2017-03-01"),
    ("Mill Creek", "Cascade Express", "SB", 2, "2017-03-01"),
    ("Portside Entry", "Harbor Tunnel", "NB", 2, "2019-11-09"),
    ("Pier Exit", "Harbor Tunnel", "SB", 2, "2019-11-09"),
    ("East Span", "Lakeside Bridge", "EB", 3, "2011-12-29"),
    ("West Span", "Lakeside Bridge", "WB", 3, "2011-12-29"),
    ("Orchard Junction", "Valley Connector", "NB", 2, "2008-05-03"),
    ("River Bend", "Valley Connector", "SB", 2, "2008-05-03"),
    ("Kent Flats", "Valley Connector", "NB", 1, "2014-07-01"),
    ("Auburn Run", "Valley Connector", "SB", 1, "2014-07-01"),
]
CORRIDOR_BASE = {"Cascade Express": 375, "Harbor Tunnel": 250, "Lakeside Bridge": 420, "Valley Connector": 190}
PERIOD_MULT = {"peak": 1.6, "offpeak": 1.0, "overnight": 0.55}
CLASS_MULT = {1: 1.0, 2: 1.5, 3: 2.0, 4: 3.0, 5: 4.0}

FIRST = [
    "Ana",
    "Ben",
    "Cora",
    "Dmitri",
    "Elena",
    "Farah",
    "Gus",
    "Hana",
    "Ivan",
    "Jada",
    "Kenji",
    "Lena",
    "Mateo",
    "Nia",
    "Omar",
    "Priya",
    "Quentin",
    "Rosa",
    "Sam",
    "Tariq",
    "Uma",
    "Victor",
    "Wren",
    "Yusuf",
]
LAST = [
    "Abbott",
    "Baker",
    "Chen",
    "Diaz",
    "Eriksen",
    "Fong",
    "Garcia",
    "Hughes",
    "Ibarra",
    "Jensen",
    "Kim",
    "Lopez",
    "Moreau",
    "Nguyen",
    "Okafor",
    "Patel",
    "Quinn",
    "Reyes",
    "Silva",
    "Tanaka",
    "Ueda",
    "Vance",
]
STATES = [("WA", 0.82), ("OR", 0.08), ("ID", 0.04), ("CA", 0.03), ("BC", 0.03)]
START = dt.datetime(2026, 1, 1)
DAYS = 181  # Jan 1 through Jun 30, 2026


def _weighted(rng: random.Random, pairs: list[tuple[object, float]]) -> object:
    values, weights = zip(*pairs, strict=True)
    return rng.choices(values, weights)[0]


def _period(ts: dt.datetime) -> str:
    if ts.hour < 5 or ts.hour >= 22:
        return "overnight"
    if ts.weekday() < 5 and (6 <= ts.hour < 9 or 15 <= ts.hour < 19):
        return "peak"
    return "offpeak"


def _trip_time(rng: random.Random) -> dt.datetime:
    day = START + dt.timedelta(days=rng.randrange(DAYS))
    weekday = day.weekday() < 5
    hours = list(range(24))
    weights = [1, 0.5, 0.4, 0.4, 0.8, 2, 6, 9, 8, 5, 4, 4, 4.5, 4.5, 5, 7, 9, 9, 6, 4, 3, 2.5, 2, 1.5]
    if not weekday:
        weights = [1.5, 1, 0.6, 0.4, 0.4, 0.8, 1.5, 2.5, 4, 5, 6, 6.5, 6.5, 6.5, 6, 6, 5.5, 5, 4.5, 4, 3, 2.5, 2, 1.8]
    hour = rng.choices(hours, weights)[0]
    return day.replace(hour=hour, minute=rng.randrange(60), second=rng.randrange(60))


def build(path: str | Path, seed: int = SEED) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(FileNotFoundError):
        path.unlink()
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    for stmt in DDL:
        conn.execute(stmt)

    for i, (name, corridor, direction, lanes, opened) in enumerate(PLAZAS, start=1):
        conn.execute("INSERT INTO plazas VALUES (?, ?, ?, ?, ?, ?)", (i, name, corridor, direction, lanes, opened))

    rate_id = 0
    rates: dict[tuple[int, int, str], int] = {}
    for plaza_id, (_n, corridor, *_rest) in enumerate(PLAZAS, start=1):
        for vclass in range(1, 6):
            for period, mult in PERIOD_MULT.items():
                rate_id += 1
                amount = int(round(CORRIDOR_BASE[corridor] * mult * CLASS_MULT[vclass] / 5) * 5)
                rates[(plaza_id, vclass, period)] = amount
                conn.execute("INSERT INTO rates VALUES (?, ?, ?, ?, ?)", (rate_id, plaza_id, vclass, period, amount))

    n_accounts = 420
    accounts = []
    for i in range(1, n_accounts + 1):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        acct_type = _weighted(rng, [("prepaid", 0.72), ("pay_by_mail", 0.2), ("fleet", 0.08)])
        status = _weighted(rng, [("active", 0.88), ("suspended", 0.07), ("closed", 0.05)])
        opened = dt.datetime(2016, 1, 1) + dt.timedelta(days=rng.randrange(3650), seconds=rng.randrange(86400))
        balance = int(rng.gauss(2500, 1800)) if acct_type == "prepaid" else -rng.randrange(0, 4000)
        if status == "suspended":
            balance = -rng.randrange(1000, 9000)
        accounts.append((i, acct_type, status))
        conn.execute(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                i,
                f"{first.lower()}.{last.lower()}{i}@example.com",
                f"{first} {last}",
                acct_type,
                status,
                _weighted(rng, STATES),
                opened.isoformat(sep=" "),
                balance,
            ),
        )

    vehicles = []
    plates: set[str] = set()
    vid = 0
    for acct_id, acct_type, _status in accounts:
        count = rng.randint(4, 9) if acct_type == "fleet" else rng.choice([1, 1, 1, 2, 2, 3])
        for _ in range(count):
            vid += 1
            vehicles.append(_insert_vehicle(conn, rng, vid, acct_id, acct_type, plates))
    for _ in range(90):  # unregistered vehicles billed by plate
        vid += 1
        vehicles.append(_insert_vehicle(conn, rng, vid, None, "pay_by_mail", plates))

    # Busy plazas see more traffic
    plaza_weights = [9, 9, 4, 4, 6, 6, 8, 8, 5, 5, 2, 2]
    trips = []
    for trip_id in range(1, 12001):
        vehicle_id, vclass, transponder = rng.choice(vehicles)
        plaza_id = rng.choices(range(1, 13), plaza_weights)[0]
        ts = _trip_time(rng)
        period = _period(ts)
        if transponder:
            read = _weighted(rng, [("transponder", 0.96), ("plate_ocr", 0.035), ("manual_review", 0.005)])
        else:
            read = _weighted(rng, [("plate_ocr", 0.9), ("manual_review", 0.1)])
        toll = rates[(plaza_id, vclass, period)]
        if read != "transponder":
            toll += 200  # pay by plate surcharge
        status = _weighted(rng, [("posted", 0.9), ("pending", 0.05), ("disputed", 0.03), ("written_off", 0.02)])
        if read == "manual_review" and rng.random() < 0.3:
            status = "disputed"
        lanes = PLAZAS[plaza_id - 1][3]
        trips.append((trip_id, status, ts))
        conn.execute(
            "INSERT INTO trips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trip_id, vehicle_id, plaza_id, ts.isoformat(sep=" "), rng.randint(1, lanes), period, read, toll, status),
        )

    dispute_id = 0
    for trip_id, status, ts in trips:
        if status == "disputed" or (status in ("posted", "written_off") and rng.random() < 0.01):
            dispute_id += 1
            outcome = None if status == "disputed" else ("reversed" if status == "written_off" else "upheld")
            opened = ts + dt.timedelta(days=rng.randint(2, 40), hours=rng.randint(0, 23))
            reason = _weighted(
                rng, [("wrong_plate", 0.45), ("duplicate_charge", 0.25), ("rate_error", 0.2), ("vehicle_sold", 0.1)]
            )
            conn.execute(
                "INSERT INTO disputes VALUES (?, ?, ?, ?, ?)",
                (dispute_id, trip_id, opened.isoformat(sep=" "), reason, outcome),
            )

    pay_id = 0
    for acct_id, acct_type, status in accounts:
        if status == "closed":
            continue
        n = rng.randint(2, 6) if acct_type != "fleet" else rng.randint(6, 12)
        for _ in range(n):
            pay_id += 1
            ts = START + dt.timedelta(days=rng.randrange(DAYS), seconds=rng.randrange(86400))
            method = _weighted(rng, [("card", 0.7), ("ach", 0.25), ("cash", 0.05)])
            amount = int(round(rng.lognormvariate(8.0, 0.6) / 100) * 100) or 500
            conn.execute(
                "INSERT INTO payments VALUES (?, ?, ?, ?, ?)", (pay_id, acct_id, ts.isoformat(sep=" "), amount, method)
            )

    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return path


def _insert_vehicle(
    conn: sqlite3.Connection, rng: random.Random, vid: int, acct_id: int | None, acct_type: str, plates: set[str]
) -> tuple[int, int, int]:
    while True:
        plate = "".join(rng.choice("ABCDEFGHJKLMNPRSTUVWXYZ") for _ in range(3)) + f"{rng.randrange(10000):04d}"
        if plate not in plates:
            plates.add(plate)
            break
    vclass = _weighted(rng, [(1, 0.9), (2, 0.05), (3, 0.03), (4, 0.01), (5, 0.01)])
    if acct_type == "fleet":
        vclass = _weighted(rng, [(1, 0.3), (2, 0.2), (3, 0.2), (4, 0.15), (5, 0.15)])
    transponder = int(acct_type in ("prepaid", "fleet") and rng.random() < 0.93)
    conn.execute(
        "INSERT INTO vehicles VALUES (?, ?, ?, ?, ?, ?)",
        (vid, acct_id, plate, _weighted(rng, STATES), vclass, transponder),
    )
    return vid, vclass, transponder


def default_path() -> Path:
    return Path(__file__).parent / "data" / "tolls.sqlite"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else default_path()
    print(f"wrote {build(target)}")
