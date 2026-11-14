"""Every check in ``verify_vote_authorization``, one fault at a time (card bada5c86).

Each case re-signs a grant that is valid in every respect but one, and builds the
vote to match it, so the check under test is the only thing that can refuse it.
This is what kills the mutants that disable a single comparison.
"""

from __future__ import annotations

import json

import pytest
from cab_grants import PASSPHRASE, key

from skcoord import cab_authorization as ca
from skcoord.itil import CABDecision

CHG = "chg-checks"


def _pin(name="chef", roles=("owner", "approver"), armor_of=None, fingerprint_of=None):
    return {
        key(fingerprint_of or name).fingerprint.upper(): {
            "identity": name,
            "roles": frozenset(roles),
            "backend": "pgpy",
            "public_key": key(armor_of or name).public_armor,
        }
    }


def _grant(signer="chef", resign=True, **over):
    from capauth.crypto import get_backend

    env = {
        "schema_version": "atlas-authorization/v1",
        "authorization_id": "pending",
        "issuer": "chef",
        "issuer_role": "owner",
        "issuer_fingerprint": key("chef").fingerprint,
        "action": "itil.cab.vote.approved",
        "target": "t",
        "change_id": CHG,
        "scope": "s",
        "issued_at": "2026-11-13T00:00:00+00:00",
        "expires_at": "2026-11-13T00:10:00+00:00",
        "nonce": "n" * 32,
        "signature": "",
    }
    env.update(over)
    if resign:
        env["authorization_id"] = ca.content_id(env)
        env["signature"] = get_backend().sign(
            ca.signing_bytes(env), key(signer).private_armor, PASSPHRASE
        )
    return env


def _vote_for(env, **over):
    fields = dict(
        change_id=env.get("change_id", CHG),
        agent=env.get("issuer", "chef"),
        decision="approved",
        subject_role=env.get("issuer_role", "owner"),
        subject_fingerprint=env.get("issuer_fingerprint", ""),
        authorization_id=env.get("authorization_id", ""),
        authorization=env,
    )
    fields.update(over)
    return CABDecision(**fields)


def test_the_baseline_grant_verifies():
    assert ca.verify_vote_authorization(_vote_for(_grant()), _pin())


def test_action_must_match_the_vote_decision():
    """K3: a grant signed for a rejection does not authorize an approval."""
    env = _grant(action="itil.cab.vote.rejected")
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())
    # ...and it does authorize the rejection it was signed for.
    assert ca.verify_vote_authorization(_vote_for(env, decision="rejected"), _pin())


def test_role_must_be_one_the_pin_grants():
    """K5: the pin lists the roles each approver may sign as."""
    env = _grant(issuer_role="owner")
    assert not ca.verify_vote_authorization(_vote_for(env), _pin(roles=("approver",)))
    assert ca.verify_vote_authorization(_vote_for(env), _pin(roles=("owner",)))


def test_the_key_must_have_the_pinned_fingerprint():
    """K6: a pin entry that names Chef's fingerprint but carries another key's
    armor must not let that other key sign as Chef."""
    env = _grant(signer="mallory")  # claims Chef's fingerprint, signed by mallory
    forged_pin = _pin(armor_of="mallory")  # Chef's fingerprint, mallory's key
    assert not ca.verify_vote_authorization(_vote_for(env), forged_pin)
    assert not ca._signature_ok(
        "pgpy",
        key("chef").fingerprint.upper(),
        ca.signing_bytes(env),
        env["signature"],
        key("mallory").public_armor,
    )


def test_the_signature_must_verify():
    env = _grant(signer="mallory")
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


def test_pinned_identity_must_be_the_issuer():
    env = _grant(issuer="lumina")
    assert not ca.verify_vote_authorization(_vote_for(env), _pin("chef"))


def test_unpinned_fingerprint_is_refused():
    assert not ca.verify_vote_authorization(_vote_for(_grant()), _pin("other"))


@pytest.mark.parametrize(
    "over",
    [
        {"schema_version": "atlas-authorization/v2"},
        {"issuer_role": "implementer"},
        {"nonce": "short"},
        {"nonce": "bad nonce with spaces and enough length"},
        {"nonce": 12345678901234567890123},
    ],
    ids=["schema", "non-human-role", "short-nonce", "nonce-charset", "non-string"],
)
def test_each_envelope_field_rule(over):
    env = _grant(**over)
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


def test_empty_signature_is_refused():
    env = _grant()
    env["signature"] = ""
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


def test_authorization_id_must_be_content_derived():
    env = _grant()
    env["authorization_id"] = "authz-" + "0" * 24
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


@pytest.mark.parametrize(
    "over",
    [
        {"change_id": "chg-elsewhere"},
        {"subject_role": "approver"},
        {"subject_fingerprint": "B" * 40},
        {"authorization_id": "authz-" + "1" * 24},
        {"agent": "lumina"},
    ],
    ids=["change", "role", "fingerprint", "authorization-id", "voter"],
)
def test_the_vote_must_match_its_grant(over):
    assert not ca.verify_vote_authorization(_vote_for(_grant(), **over), _pin())


@pytest.mark.parametrize("mutate", ["missing", "extra"])
def test_the_field_set_is_exact(mutate):
    env = _grant()
    if mutate == "missing":
        del env["scope"]
    else:
        env["extra"] = "x"
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


def test_no_envelope_never_verifies():
    vote = _vote_for(_grant(), authorization=None)
    assert not ca.verify_vote_authorization(vote, _pin())


def test_identity_key_normalises_spellings():
    for spelling in ("chef", "Chef", "capauth:chef@skworld.io", "chef@chef.skworld.io"):
        assert ca.identity_key(spelling) == "chef"
    assert ca.identity_key(None) == ""


def test_load_approvers_reads_key_files_and_normalises(tmp_path):
    armor = tmp_path / "public.asc"
    armor.write_text(key("chef").public_armor)
    fpr = key("chef").fingerprint
    spaced = " ".join(fpr[i : i + 4] for i in range(0, len(fpr), 4)).lower()
    path = tmp_path / "pin.json"
    path.write_text(
        json.dumps(
            {
                "approvers": [
                    {"identity": "chef", "fingerprint": spaced, "public_key_file": str(armor)},
                    {"identity": "noarmor", "fingerprint": "C" * 40},
                    {"fingerprint": key("lumina").fingerprint, "public_key": "x"},
                ]
            }
        )
    )
    approvers = ca.load_approvers(path)
    assert set(approvers) == {fpr.upper()}
    entry = approvers[fpr.upper()]
    assert entry["roles"] == frozenset({"owner", "approver"})
    assert entry["backend"] == "pgpy"
    assert ca.verify_vote_authorization(_vote_for(_grant()), approvers)


def test_load_approvers_fails_closed(tmp_path):
    assert ca.load_approvers(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert ca.load_approvers(bad) == {}


def test_default_pin_is_read_when_none_is_passed(tmp_path, monkeypatch):
    path = tmp_path / "pin.json"
    path.write_text(
        json.dumps(
            {
                "approvers": [
                    {
                        "identity": "chef",
                        "fingerprint": key("chef").fingerprint,
                        "public_key": key("chef").public_armor,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(ca, "APPROVERS_FILE", path)
    assert ca.verify_vote_authorization(_vote_for(_grant()))
    monkeypatch.setattr(ca, "APPROVERS_FILE", tmp_path / "absent.json")
    assert not ca.verify_vote_authorization(_vote_for(_grant()))


def test_non_human_role_is_refused_even_when_the_pin_lists_it():
    env = _grant(issuer_role="implementer")
    pin = _pin(roles=("owner", "approver", "implementer"))
    assert not ca.verify_vote_authorization(_vote_for(env), pin)


@pytest.fixture()
def signature_always_ok(monkeypatch):
    """Isolate the structural checks from the signature check they back up."""
    monkeypatch.setattr(ca, "_signature_ok", lambda *a: True)


def test_empty_signature_is_refused_before_any_crypto(signature_always_ok):
    env = _grant()
    env["signature"] = ""
    assert not ca.verify_vote_authorization(_vote_for(env), _pin())


def test_unpinned_fingerprint_is_refused_before_any_crypto(signature_always_ok):
    assert ca.verify_vote_authorization(_vote_for(_grant()), _pin())
    assert not ca.verify_vote_authorization(_vote_for(_grant()), {})
