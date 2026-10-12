"""Regression coverage for production snapshot retention."""


def test_pruning_keeps_the_newest_snapshots_across_different_labels(tmp_path, monkeypatch):
    from tools import backup_db

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    names = [
        "fta.db.pre_pre_v10_20260818_095955",
        "fta.db.pre_pre_v11_20260818_100000",
        "fta.db.pre_pre_v12_20260818_100100",
        "fta.db.pre_pre_p11_allowlist_20260822_132415",
        "fta.db.pre_pre_p11_scope_immutability_20260822_140247",
        "fta.db.pre_pre_p13_first_canary_20260824_084715",
    ]
    for name in names:
        (backup_dir / name).write_bytes(b"snapshot")

    monkeypatch.setattr(backup_db, "BACKUP_DIR", backup_dir)

    backup_db._prune_old(keep=5)

    remaining = {path.name for path in backup_dir.iterdir()}
    assert "fta.db.pre_pre_v10_20260818_095955" not in remaining
    assert "fta.db.pre_pre_p13_first_canary_20260824_084715" in remaining
