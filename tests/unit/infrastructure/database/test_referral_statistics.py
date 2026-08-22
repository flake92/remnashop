from datetime import UTC, datetime

from sqlalchemy import create_engine, insert

from src.core.enums import ReferralLevel
from src.infrastructure.database.dao.referral import ReferralDaoImpl
from src.infrastructure.database.models.referral import Referral


def test_referral_levels_are_derived_from_the_attribution_graph() -> None:
    engine = create_engine("sqlite:///:memory:")
    Referral.__table__.create(engine)
    timestamp = datetime(2026, 8, 22, tzinfo=UTC)

    # A -> B, C; B -> D, E; C -> F; D -> G. Every stored row is a
    # direct edge (FIRST), while the graph contains four L2 relationships.
    rows = [
        (1, 1, 2),
        (2, 1, 3),
        (3, 2, 4),
        (4, 2, 5),
        (5, 3, 6),
        (6, 4, 7),
    ]
    with engine.begin() as connection:
        connection.execute(
            insert(Referral.__table__),
            [
                {
                    "id": referral_id,
                    "referrer_id": referrer_id,
                    "referred_id": referred_id,
                    "level": ReferralLevel.FIRST,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                }
                for referral_id, referrer_id, referred_id in rows
            ],
        )

        global_stats = connection.execute(
            ReferralDaoImpl._referral_network_stats_statement()
        ).mappings().one()
        user_a_stats = connection.execute(
            ReferralDaoImpl._user_referral_network_stats_statement(1)
        ).mappings().one()
        user_b_stats = connection.execute(
            ReferralDaoImpl._user_referral_network_stats_statement(2)
        ).mappings().one()
        leaf_stats = connection.execute(
            ReferralDaoImpl._user_referral_network_stats_statement(7)
        ).mappings().one()

    assert dict(global_stats) == {
        "total_referrals": 6,
        "level_1_count": 6,
        "level_2_count": 4,
        "unique_referrers": 4,
    }
    assert dict(user_a_stats) == {"level_1": 2, "level_2": 3}
    assert dict(user_b_stats) == {"level_1": 2, "level_2": 1}
    assert dict(leaf_stats) == {"level_1": 0, "level_2": 0}
