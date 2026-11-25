"""Schedule actors (audit and pending-confirmation identity) keep issuer, subject and surface."""

from strandly.ops.schedules.model import Actor


def test_identity_round_trip_and_surface_boundary():
    actor = Actor("portal:immutable-sub", "agent", "https://issuer/pool")
    assert Actor.from_item(actor.to_item()) == actor
    dashboard = Actor(actor.actor, "dashboard", actor.issuer)
    assert actor.same_identity(dashboard)
    assert not actor.same_as(dashboard)
    assert not actor.same_identity(Actor(actor.actor, "agent", "https://issuer/other"))
    assert not actor.same_identity(Actor("portal:other-sub", "agent", actor.issuer))
    assert not actor.same_identity(Actor(actor.actor, "agent"))


def test_legacy_actor_round_trip_does_not_invent_issuer():
    item = {"actor": "portal:alice@example.com", "surface": "dashboard"}
    assert Actor.from_item(item).to_item() == item
    assert Actor.from_item(item).issuer == ""
