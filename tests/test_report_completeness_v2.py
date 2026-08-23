"""Focused regressions for independently checked report completeness metadata."""
from __future__ import annotations

import json
import re

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from engine import store

    monkeypatch.setattr(store, "DB_PATH", tmp_path / "data" / "jobs.db")
    con = store.connect()
    yield con
    con.close()


def _job(external_id: str, *, score: int = 70, gate: str = "QUALIFIED"):
    from engine.models import Job

    job = Job(
        company="Acme",
        title="CRM Manager",
        url=f"https://example.test/jobs/{external_id}",
        source="greenhouse",
        external_id=external_id,
        location="Remote - United States",
    )
    job.score = score
    job.gate = gate
    job.reasons = ["focused completeness regression"]
    return job


def _v2_metadata(text: str) -> dict:
    match = re.search(
        r"^<!-- careerkit-report-uids-v2: (.*?) -->$", text, re.MULTILINE
    )
    assert match, text
    return json.loads(match.group(1))


def test_v2_manifest_detects_a_row_omitted_from_render_and_manifest(
    db, tmp_path, monkeypatch,
):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    jobs = [_job("one"), _job("two")]
    store.upsert(db, jobs)
    path = report.write_report(
        db,
        store.query(db),
        health=[],
        run_detail={"pulled": 2, "sources_ok": 1},
        filename="complete.md",
        min_score=0,
    )
    assert consistency.check_report(db, path) == []

    # Reproduce the failure class: one grouped row disappears from both the
    # visible report and the renderer-authored manifest, while the database and
    # logical-state marker remain unchanged.
    lines = path.read_text(encoding="utf-8").splitlines()
    sibling = next(
        index for index, line in enumerate(lines)
        if line.startswith("  - ") and "**UID** `" in line
    )
    victim = re.search(r"\*\*UID\*\* `([^`]+)`", lines[sibling]).group(1)
    del lines[sibling:sibling + 2]
    lines = [line.replace("  (2 open reqs)", "") for line in lines]
    for index, line in enumerate(lines):
        if not line.startswith("<!-- careerkit-report-uids-v2: "):
            continue
        metadata = json.loads(line.split(": ", 1)[1].rsplit(" -->", 1)[0])
        metadata["uids"].remove(victim)
        lines[index] = (
            "<!-- careerkit-report-uids-v2: "
            + json.dumps(metadata, separators=(",", ":"))
            + " -->"
        )
        break
    path.write_text("\n".join(lines), encoding="utf-8")

    problems = consistency.check_report(db, path)
    assert any(
        victim in problem and "omits reportable uid" in problem
        for problem in problems
    ), problems


def test_v2_manifest_preserves_nonzero_score_and_active_row_semantics(
    db, tmp_path, monkeypatch,
):
    from engine import pull, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    included = _job("included", score=70)
    low = _job("low", score=49, gate="VERIFY")
    applied = _job("applied", score=90)
    ignored = _job("ignored", score=90)
    rejected = _job("rejected", score=90)
    delisted = _job("delisted", score=90)
    excluded = _job("excluded", score=99, gate="EXCLUDED")
    store.upsert(
        db, [included, low, applied, ignored, rejected, delisted, excluded]
    )
    db.execute("UPDATE jobs SET status='applied' WHERE uid=?", (applied.uid,))
    db.execute("UPDATE jobs SET status='ignored' WHERE uid=?", (ignored.uid,))
    db.execute("UPDATE jobs SET status='rejected' WHERE uid=?", (rejected.uid,))
    db.execute(
        "UPDATE jobs SET delisted_on='2026-08-23' WHERE uid=?", (delisted.uid,)
    )
    db.commit()

    path = pull.rebuild_report(db, min_score=50, echo=lambda _message: None)
    metadata = _v2_metadata(path.read_text(encoding="utf-8"))

    assert metadata == {"min_score": 50, "uids": [included.uid]}
    from engine import consistency

    assert consistency.check_report(db, path) == []


def test_v1_manifest_remains_readable_without_inventing_a_score_floor(
    db, tmp_path, monkeypatch,
):
    from engine import consistency, report, store

    monkeypatch.setattr(report, "OUT_DIR", tmp_path / "out")
    job = _job("legacy")
    store.upsert(db, [job])
    path = report.write_report(
        db,
        store.query(db),
        health=[],
        run_detail={},
        filename="legacy-v1.md",
    )
    text = path.read_text(encoding="utf-8")
    metadata = _v2_metadata(text)
    text = re.sub(
        r"^<!-- careerkit-report-uids-v2: .*? -->$",
        "<!-- careerkit-report-uids-v1: "
        + json.dumps(metadata["uids"], separators=(",", ":"))
        + " -->",
        text,
        flags=re.MULTILINE,
    )
    path.write_text(text, encoding="utf-8")

    assert consistency.check_report(db, path) == []
