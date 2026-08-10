"""How a finished match is classified for the admin Live Matches page.

Two things are worth pinning down. First, ``derive_end_reason`` has to keep
telling the truth about rows written before ``end_reason`` existed: every
terminal path used to write ``status = "completed"``, so an old row with no
winner genuinely cannot be resolved into "ended by user" vs "cleared by user"
and must say so rather than guess.

Second, ``end_reason_filter`` has to select exactly the rows ``derive_end_reason``
labels — otherwise clicking the "Completed 12" chip shows a different set than
the twelve rows the table just displayed as completed. Those two live in
different languages (Python vs SQL), so the tests run the SQL against a real
in-memory SQLite database and compare the two answers row for row.
"""

import unittest

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import sessionmaker

try:                                        # SQLAlchemy 2.x
    from sqlalchemy.orm import declarative_base
except ImportError:                         # SQLAlchemy 1.x
    from sqlalchemy.ext.declarative import declarative_base

from services.match_outcome import (
    END_AUTO, END_CLEARED_BY_USER, END_COMPLETED, END_ENDED_BY_ADMIN,
    END_ENDED_BY_USER, END_REASONS, END_UNKNOWN,
    TYPE_CIPL, TYPE_VSBOT,
    derive_end_reason, end_reason_filter, mark_end, match_type_family,
    match_type_label,
)

Base = declarative_base()


class FakeMatch(Base):
    """The columns ``match_outcome`` actually reads — the real Match model
    drags in the whole schema, which this doesn't need."""
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    status = Column(String(30))
    match_type = Column(String(30))
    end_reason = Column(String(30))
    ended_by_id = Column(Integer)
    winner_id = Column(Integer)
    margin_type = Column(String(20))


# (kwargs, expected reason, expected "this was inferred")
ROWS = [
    # Recorded — taken at face value, whatever the rest of the row looks like.
    (dict(status="completed", end_reason=END_COMPLETED, winner_id=7), END_COMPLETED, False),
    (dict(status="completed", end_reason=END_ENDED_BY_USER), END_ENDED_BY_USER, False),
    (dict(status="completed", end_reason=END_CLEARED_BY_USER), END_CLEARED_BY_USER, False),
    (dict(status="abandoned", end_reason=END_ENDED_BY_ADMIN), END_ENDED_BY_ADMIN, False),
    (dict(status="abandoned", end_reason=END_AUTO), END_AUTO, False),
    # Legacy — reconstructed.
    (dict(status="completed", winner_id=3, margin_type="runs"), END_COMPLETED, True),
    (dict(status="completed", margin_type="tie"), END_COMPLETED, True),
    # Both spellings are in the database: handlers/super_over.py writes one,
    # sim_match.py and the Mini App finaliser write the other.
    (dict(status="completed", margin_type="super over"), END_COMPLETED, True),
    (dict(status="completed", margin_type="super_over"), END_COMPLETED, True),
    # SQL `IN` is case-sensitive on Postgres; the derivation lowercases first.
    (dict(status="completed", margin_type="Tie"), END_COMPLETED, True),
    (dict(status="completed"), END_UNKNOWN, True),
    (dict(status="completed", margin_type=None, winner_id=None), END_UNKNOWN, True),
    # A no-progress Mini App cancel is not a result — it must not read as
    # Completed just because it carries a margin.
    (dict(status="completed", margin_type="cancelled"), END_UNKNOWN, True),
    (dict(status="abandoned", margin_type="abandoned"), END_AUTO, True),
    (dict(status="expired"), END_AUTO, True),
    (dict(status="cancelled"), END_AUTO, True),
    # A status from neither list. The derivation falls through to unknown
    # whether or not the row has a winner, so the SQL has to as well — else
    # the row renders as Unrecorded but no chip ever selects it.
    (dict(status="mystery"), END_UNKNOWN, True),
    (dict(status="mystery", winner_id=4, margin_type="runs"), END_UNKNOWN, True),
    (dict(status=None), END_UNKNOWN, True),
]


class DeriveEndReasonTests(unittest.TestCase):
    def test_each_row_resolves_to_the_expected_reason(self):
        for kwargs, expected, inferred in ROWS:
            with self.subTest(**kwargs):
                reason, was_inferred = derive_end_reason(FakeMatch(**kwargs))
                self.assertEqual(expected, reason)
                self.assertEqual(inferred, was_inferred)

    def test_a_recorded_reason_is_never_reported_as_inferred(self):
        # The table marks inferred values with a "*"; a recorded one must not
        # carry it, or every row would read as a guess.
        _, inferred = derive_end_reason(
            FakeMatch(status="completed", end_reason=END_ENDED_BY_USER))
        self.assertFalse(inferred)

    def test_an_old_cleared_row_is_not_guessed_as_ended_by_user(self):
        # /clearmatches and /endmatch left byte-identical rows behind. Claiming
        # either one would be inventing a fact.
        reason, _ = derive_end_reason(FakeMatch(status="completed"))
        self.assertEqual(END_UNKNOWN, reason)


class EndReasonFilterTests(unittest.TestCase):
    """The SQL filter must select exactly what the Python derivation labels."""

    def setUp(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        self.session = sessionmaker(bind=engine)()
        for kwargs, _expected, _inferred in ROWS:
            self.session.add(FakeMatch(**kwargs))
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def _selected(self, reason):
        clause = end_reason_filter(FakeMatch, reason)
        return {m.id for m in self.session.query(FakeMatch).filter(clause).all()}

    def _derived(self, reason):
        return {m.id for m in self.session.query(FakeMatch).all()
                if derive_end_reason(m)[0] == reason}

    def test_every_reason_selects_the_rows_it_labels(self):
        for reason in END_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(self._derived(reason), self._selected(reason))

    def test_the_buckets_partition_the_table(self):
        # Every finished row lands in exactly one chip, so the tallies add up
        # to the unfiltered total instead of double-counting or dropping rows.
        seen = []
        for reason in END_REASONS:
            seen.extend(self._selected(reason))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(self.session.query(FakeMatch).count(), len(seen))

    def test_a_null_margin_does_not_swallow_the_unknown_bucket(self):
        # ``margin_type IN (...)`` is NULL for a NULL margin, and a negated
        # NULL is still NULL — which would quietly select nothing at all.
        self.assertTrue(self._selected(END_UNKNOWN))

    def test_an_unrecognised_reason_asks_for_no_filter(self):
        self.assertIsNone(end_reason_filter(FakeMatch, "made_up"))

    def test_a_cancelled_margin_is_not_treated_as_a_result(self):
        # "cancelled" is written for a no-progress Mini App cancel. It means
        # nothing happened, not "a result with no named winner", so it must
        # not join RESULTLESS_MARGINS and land in the Completed bucket.
        cancelled = {m.id for m in self.session.query(FakeMatch)
                     .filter(FakeMatch.margin_type == "cancelled").all()}
        self.assertTrue(cancelled)
        self.assertFalse(cancelled & self._selected(END_COMPLETED))
        self.assertTrue(cancelled <= self._selected(END_UNKNOWN))


class MatchTypeTests(unittest.TestCase):
    def test_a_recorded_mode_wins_over_the_players(self):
        label, inferred = match_type_label(FakeMatch(match_type=TYPE_CIPL),
                                           opponent_is_bot=True)
        self.assertEqual("CIPL", label)
        self.assertFalse(inferred)

    def test_an_untagged_row_falls_back_to_who_was_playing(self):
        self.assertEqual(("PvP", True), match_type_label(FakeMatch()))
        self.assertEqual(("vs AI", True),
                         match_type_label(FakeMatch(), opponent_is_bot=True))
        self.assertEqual(("AI vs AI", True),
                         match_type_label(FakeMatch(), opponent_is_bot=True,
                                          both_bots=True))

    def test_the_family_drives_the_pill_colour(self):
        self.assertEqual("ai", match_type_family(FakeMatch(match_type=TYPE_VSBOT)))
        self.assertEqual("pvp", match_type_family(FakeMatch(match_type=TYPE_CIPL)))
        self.assertEqual("spectate",
                         match_type_family(FakeMatch(), both_bots=True))


class MarkEndTests(unittest.TestCase):
    def test_it_records_the_reason_and_the_person(self):
        m = FakeMatch(status="completed")
        mark_end(m, END_ENDED_BY_USER, by_user_id=42)
        self.assertEqual(END_ENDED_BY_USER, m.end_reason)
        self.assertEqual(42, m.ended_by_id)

    def test_it_leaves_status_and_the_result_alone(self):
        # Callers own those: "completed" vs "abandoned" means different things
        # per mode and other queries filter on it.
        m = FakeMatch(status="abandoned", winner_id=9)
        mark_end(m, END_AUTO)
        self.assertEqual("abandoned", m.status)
        self.assertEqual(9, m.winner_id)
        self.assertIsNone(m.ended_by_id)

    def test_it_tolerates_a_missing_match(self):
        self.assertIsNone(mark_end(None, END_AUTO))


if __name__ == "__main__":
    unittest.main()
