"""
CRM Tags / Groups write API tests (M4, schema V7).

Covers: tag CRUD, group CRUD, bulk lead assignment (idempotent), the
lead_tags / group_members cascade, 404/409 paths, and the tag filter on
/api/v1/leads. Uses the shared `api_client` fixture (conftest.py) whose
temp DB is built via build_database → migrations incl. V7 run.

The fixture seeds 4 leads (ids 1–4):
  1 Alpha Corp (new) · 2 Beta GmbH (contacted) · 3 Gamma Ltd (replied) · 4 Delta 100% (new)
"""

import pytest


# ── Tags ───────────────────────────────────────────────────────────────────


def test_tag_create_and_duplicate_409(api_client):
    r = api_client.post("/api/v1/tags", json={"name": "VIP", "color": "#f59e0b", "category": "custom"})
    assert r.status_code == 200, r.text
    tag = r.json()
    assert tag["name"] == "VIP" and tag["color"] == "#f59e0b"
    assert tag["lead_count"] == 0

    # Duplicate name → 409
    dup = api_client.post("/api/v1/tags", json={"name": "VIP"})
    assert dup.status_code == 409

    # Empty name → 400 (validation min_length=1)
    assert api_client.post("/api/v1/tags", json={"name": ""}).status_code in (400, 422)


def test_tag_update_404_and_rename(api_client):
    tag_id = api_client.post("/api/v1/tags", json={"name": "Old"}).json()["id"]

    # Rename + recolor
    r = api_client.put(f"/api/v1/tags/{tag_id}", json={"name": "New", "color": "#ef4444"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New" and r.json()["color"] == "#ef4444"

    # Rename onto an existing tag → 409
    api_client.post("/api/v1/tags", json={"name": "Other"})
    assert api_client.put(f"/api/v1/tags/{tag_id}", json={"name": "Other"}).status_code == 409

    # Missing tag → 404
    assert api_client.put("/api/v1/tags/9999", json={"name": "X"}).status_code == 404
    assert api_client.delete("/api/v1/tags/9999").status_code == 404


def test_tag_delete_cascades_lead_tags(api_client):
    tag_id = api_client.post("/api/v1/tags", json={"name": "Cascade"}).json()["id"]
    api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [1, 2]})

    assert api_client.delete(f"/api/v1/tags/{tag_id}").status_code == 200
    # Tag gone; no orphan lead_tags rows remain
    assert api_client.get("/api/v1/tags").json()["tags"] == []
    body = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 50}).json()
    for lead in body["items"]:
        assert lead["tags"] == []


def test_tag_bulk_assign_idempotent_and_counts(api_client):
    tag_id = api_client.post("/api/v1/tags", json={"name": "Bulk"}).json()["id"]

    # First assign → 3 new
    r = api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [1, 2, 3]})
    assert r.status_code == 200 and r.json()["assigned"] == 3

    # Re-assign same set → idempotent, 0 new
    r = api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [1, 2, 3]})
    assert r.json()["assigned"] == 0

    # lead_count reflects distinct membership
    tags = api_client.get("/api/v1/tags").json()["tags"]
    assert {t["name"]: t["lead_count"] for t in tags}["Bulk"] == 3

    # Unassign lead 2 → 1 removed
    r = api_client.request("DELETE", f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [2]})
    assert r.status_code == 200 and r.json()["removed"] == 1
    # Re-remove → idempotent, 0 removed
    r = api_client.request("DELETE", f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [2]})
    assert r.json()["removed"] == 0


def test_tag_bulk_filters_nonexistent_leads(api_client):
    """lead_ids that don't exist are silently dropped (no FK error)."""
    tag_id = api_client.post("/api/v1/tags", json={"name": "Filter"}).json()["id"]
    r = api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [1, 9999, 2, 99999]})
    assert r.status_code == 200 and r.json()["assigned"] == 2


def test_tag_bulk_missing_tag_404_and_cap_422(api_client):
    assert api_client.post("/api/v1/tags/9999/leads", json={"lead_ids": [1]}).status_code == 404

    # Bulk > 1000 → FastAPI validation 422
    big = list(range(1, 1001))  # 1000 ok
    r = api_client.post("/api/v1/tags/1/leads", json={"lead_ids": big})
    assert r.status_code in (404, 200)  # tag may or may not exist; either is fine
    too_big = list(range(1, 1002))
    r = api_client.post("/api/v1/tags/1/leads", json={"lead_ids": too_big})
    assert r.status_code == 422


def test_tag_filter_on_leads_endpoint(api_client):
    tag_id = api_client.post("/api/v1/tags", json={"name": "DE客户"}).json()["id"]
    api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [2, 3]})

    # AND semantics with a single tag → the two tagged leads
    body = api_client.get("/api/v1/leads", params={"page": 1, "tag": "DE客户"}).json()
    assert body["total"] == 2
    assert {l["id"] for l in body["items"]} == {2, 3}
    # Every row carries its tags[]
    for lead in body["items"]:
        assert any(t["name"] == "DE客户" for t in lead["tags"])

    # Untagged query → only the remaining leads
    body2 = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 50}).json()
    for lead in body2["items"]:
        if lead["id"] not in (2, 3):
            assert lead["tags"] == []


def test_tag_filter_multi_tag_is_and_semantics(api_client):
    """CRM-003: multiple tag params are AND — a lead must carry ALL of them."""
    a = api_client.post("/api/v1/tags", json={"name": "A"}).json()["id"]
    b = api_client.post("/api/v1/tags", json={"name": "B"}).json()["id"]
    api_client.post(f"/api/v1/tags/{a}/leads", json={"lead_ids": [2]})
    api_client.post(f"/api/v1/tags/{b}/leads", json={"lead_ids": [3]})

    # No lead has both → empty
    body = api_client.get("/api/v1/leads", params={"page": 1, "tag": ["A", "B"]}).json()
    assert body["total"] == 0

    # Give lead 2 both tags → A+B returns only it
    api_client.post(f"/api/v1/tags/{b}/leads", json={"lead_ids": [2]})
    body = api_client.get("/api/v1/leads", params={"page": 1, "tag": ["A", "B"]}).json()
    assert body["total"] == 1
    assert {i["id"] for i in body["items"]} == {2}

    # Duplicate tags don't change the result (dedup before the AND)
    body = api_client.get("/api/v1/leads", params={"page": 1, "tag": ["A", "A"]}).json()
    assert body["total"] == 1 and {i["id"] for i in body["items"]} == {2}

    # Nonexistent + existing tag → empty (AND fails on the missing one)
    body = api_client.get("/api/v1/leads", params={"page": 1, "tag": ["A", "NoSuchTag"]}).json()
    assert body["total"] == 0


def test_tag_leads_rows_cascade_on_lead_delete(api_client):
    tag_id = api_client.post("/api/v1/tags", json={"name": "Del"}).json()["id"]
    api_client.post(f"/api/v1/tags/{tag_id}/leads", json={"lead_ids": [1, 2, 3]})
    assert api_client.delete("/api/v1/leads/1").status_code == 200
    tags = api_client.get("/api/v1/tags").json()["tags"]
    assert {t["name"]: t["lead_count"] for t in tags}["Del"] == 2


# ── Groups ─────────────────────────────────────────────────────────────────


def test_group_crud_and_membership(api_client):
    r = api_client.post("/api/v1/groups", json={"name": "德国重点", "description": "DE 市场"})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["name"] == "德国重点" and g["description"] == "DE 市场" and g["member_count"] == 0

    # Duplicate name → 409
    assert api_client.post("/api/v1/groups", json={"name": "德国重点"}).status_code == 409

    # Add member
    r = api_client.post(f"/api/v1/groups/{g['id']}/leads", json={"lead_ids": [2]})
    assert r.json()["assigned"] == 1
    groups = api_client.get("/api/v1/groups").json()["groups"]
    assert {x["name"]: x["member_count"] for x in groups}["德国重点"] == 1

    # Update description
    r = api_client.put(f"/api/v1/groups/{g['id']}", json={"description": "DE+CH"})
    assert r.json()["description"] == "DE+CH"

    # Remove member
    r = api_client.request("DELETE", f"/api/v1/groups/{g['id']}/leads", json={"lead_ids": [2]})
    assert r.json()["removed"] == 1

    # Delete group
    assert api_client.delete(f"/api/v1/groups/{g['id']}").status_code == 200
    assert api_client.get("/api/v1/groups").json()["groups"] == []

    # Missing group → 404
    assert api_client.put("/api/v1/groups/9999", json={"name": "X"}).status_code == 404
    assert api_client.post("/api/v1/groups/9999/leads", json={"lead_ids": [1]}).status_code == 404


def test_lead_groups_payload(api_client):
    """query_leads hydrates groups[] per lead (drawer membership display)."""
    gid = api_client.post("/api/v1/groups", json={"name": "A组"}).json()["id"]
    api_client.post(f"/api/v1/groups/{gid}/leads", json={"lead_ids": [1, 2]})

    body = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 50}).json()
    by_id = {l["id"]: l for l in body["items"]}
    assert [g["name"] for g in by_id[1]["groups"]] == ["A组"]
    assert [g["name"] for g in by_id[2]["groups"]] == ["A组"]
    assert by_id[3]["groups"] == []
