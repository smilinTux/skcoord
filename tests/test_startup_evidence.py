from skcoord.startup_evidence import prepare_startup_evidence, validate_startup_evidence


def report(card="card1", agent="worker", generation="g1", **overrides):
    fields = dict(card_id=card, agent=agent, claim_generation=generation,
                  repository={"ok": True, "ref": "repo"},
                  instructions={"ok": True, "ref": "AGENTS.md"},
                  context={"ok": True, "ref": "task-tdd"},
                  mailbox={"ok": True, "ref": "skmail"})
    fields.update(overrides)
    return prepare_startup_evidence(**fields)


def test_success_is_hash_bound_and_generation_attributed():
    evidence = report()
    result = validate_startup_evidence(evidence, card_id="card1", agent="worker", claim_generation="g1")
    assert result.ok
    assert result.evidence_sha256 == evidence["evidence_sha256"]


def test_each_failure_class_is_actionable():
    for field, code in (("repository", "repository"), ("instructions", "instructions"),
                        ("context", "context"), ("mailbox", "mailbox")):
        evidence = report(**{field: {"ok": False}})
        result = validate_startup_evidence(evidence, card_id="card1", agent="worker", claim_generation="g1")
        assert result.code == code
        assert "pre-claim rejected" in result.message


def test_stale_and_malformed_reports_fail_closed():
    evidence = report()
    assert validate_startup_evidence(evidence, card_id="card1", agent="worker", claim_generation="old").code == "stale"
    evidence["repository"] = {"ok": False}
    assert validate_startup_evidence(evidence, card_id="card1", agent="worker", claim_generation="g1").code == "malformed"
