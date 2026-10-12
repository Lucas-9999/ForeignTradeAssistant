"""
CRM Web Console read-only API tests (M1).

These hit the REAL FastAPI app (app.app) through TestClient against a throwaway
temp DB (conftest redirects config.DB_PATH per-test). Lifespan is NOT entered
(no `with`), so the collection scheduler never starts and no network is touched.
"""

def _today_counts(client):
    r = client.get("/api/v1/dashboard/overview")
    assert r.status_code == 200, r.text
    return r.json()


# ── Dashboard ──────────────────────────────────────────────────────────────


def test_dashboard_kpis_match_db(api_client):
    data = _today_counts(api_client)
    k = data["kpis"]
    # 3 leads discovered within today? No — all are past days, so today_new_leads=0.
    assert k["today_new_leads"] == 0
    # NetEase 7 (sending_quotas) + Zoho 3 (mailbox_quotas)
    assert k["today_emails_sent"] == 10
    # inbound today = Gamma's reply (20 hours ago)
    assert k["today_replies"] == 1
    assert k["today_bounces"] == 0
    assert k["total_leads"] == 4
    assert k["contacted_leads"] == 2  # leads 2,3 have outbound comms
    assert k["replied_leads"] == 1    # lead 3 has inbound


def test_dashboard_series_zero_filled_30_buckets(api_client):
    data = _today_counts(api_client)
    for name in ("leads_created", "emails_sent", "replies", "bounces"):
        series = data["series_30d"][name]
        assert len(series) == 30, f"{name} should be zero-filled to 30 buckets"
        assert all(s["day"] for s in series), f"{name} buckets need a day label"
        assert all(s["count"] >= 0 for s in series)


def test_dashboard_distribution_shapes(api_client):
    data = _today_counts(api_client)
    d = data["distributions"]
    assert {x["name"] for x in d["by_source"]} == {"xunkexing", "facebook", "manual"}
    # by_status includes new/contacted/replied
    assert {x["name"] for x in d["by_status"]} == {"new", "contacted", "replied"}
    # country '其他'/NULL is excluded by the top-15 query
    assert {"USA", "DE", "UK"}.issubset({x["name"] for x in d["by_country"]})
    # mailbox: zoho=3 from mailbox_quotas + default=7 from sending_quotas
    mb = {x["name"]: x["count"] for x in d["by_mailbox"]}
    assert mb.get("zoho") == 3 and mb.get("default") == 7


def test_dashboard_details_follow_kpi_classification(api_client):
    replies = api_client.get("/api/v1/dashboard/details/today_replies")
    assert replies.status_code == 200, replies.text
    assert replies.json()["kpi_total"] == 1
    assert replies.json()["total"] == 1
    assert replies.json()["items"][0]["classification"] == "reply"

    new_leads = api_client.get("/api/v1/dashboard/details/total_leads", params={"page_size": 2})
    assert new_leads.status_code == 200
    assert new_leads.json()["total"] == 4
    assert len(new_leads.json()["items"]) == 2

    missing = api_client.get("/api/v1/dashboard/details/not_a_metric")
    assert missing.status_code == 404


def test_dashboard_excludes_inbound_bounce_from_reply_count(api_client):
    import app as fta_app

    conn = fta_app.get_db()
    conn.execute(
        """INSERT INTO communications
           (lead_id, channel, direction, subject, body, status, sent_at)
           VALUES (2, 'email', 'inbound', 'Undelivered Mail Returned to Sender',
                   '550 user unknown', 'received', datetime('now'))"""
    )
    conn.commit()
    conn.close()

    overview = api_client.get("/api/v1/dashboard/overview").json()
    assert overview["kpis"]["today_replies"] == 1
    assert overview["kpis"]["today_bounces"] == 1
    bounce_details = api_client.get("/api/v1/dashboard/details/today_bounces").json()
    assert bounce_details["total"] == 1
    assert bounce_details["items"][0]["classification"] == "bounce"


def test_dashboard_counts_persisted_bounced_status(api_client):
    """A DSN persisted by the IMAP monitor with status='bounced' counts as a
    bounce even when its stored subject/body no longer match the keyword lists
    (some DSNs are classified live by From/Return-Path only)."""
    import app as fta_app

    conn = fta_app.get_db()
    conn.execute(
        """INSERT INTO communications
           (channel, direction, subject, body, status, sent_at)
           VALUES ('email', 'inbound', 'Mailer-Daemon notice', 'Generic diagnostic',
                   'bounced', datetime('now'))"""
    )
    conn.commit()
    conn.close()

    overview = api_client.get("/api/v1/dashboard/overview").json()
    assert overview["kpis"]["today_bounces"] == 1
    # Gamma's genuine reply is not affected by the persisted bounce row.
    assert overview["kpis"]["today_replies"] == 1
    details = api_client.get("/api/v1/dashboard/details/today_bounces").json()
    assert details["total"] == 1
    assert details["items"][0]["classification"] == "bounce"


def test_dashboard_reply_boundary_and_thread_deduplication(api_client):
    """Pre-campaign history is excluded and repeated messages in one thread
    remain one genuine reply conversation."""
    import app as fta_app

    conn = fta_app.get_db()
    conn.executemany(
        """INSERT INTO communications
           (lead_id, channel, direction, subject, body, status, sent_at)
           VALUES (?, 'email', 'inbound', ?, ?, 'received', ?)""",
        [
            (1, "Re: Legacy personal mail", "old conversation", "2000-01-01 00:00:00"),
            (3, "Re: [DCE] Re: Product inquiry", "same campaign thread", "2099-01-01 00:00:00"),
        ],
    )
    conn.commit()
    conn.close()

    overview = api_client.get("/api/v1/dashboard/overview").json()
    assert overview["kpis"]["replied_leads"] == 1
    details = api_client.get("/api/v1/dashboard/details/replied_leads").json()
    assert details["kpi_total"] == details["total"] == 1


# ── Reputation (P8 read-only) ─────────────────────────────────────────────


def test_reputation_domain_health_lists_persisted_snapshots(api_client):
    """P8 only exposes the snapshots already stored by P5; it never audits."""
    import app as fta_app

    conn = fta_app.get_db()
    conn.executemany(
        """INSERT INTO domain_health
           (domain, health_score, status, mx_status, spf_status, dkim_status,
            dmarc_status, a_status, ptr_status, tls_status, sending_ip,
            authentication_status, last_checked)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                "healthy.example", 92.0, "healthy", "PASS", "PASS", "PASS",
                "PASS", "PASS", "PASS", "PASS", "203.0.113.8", "PASS",
                "2026-08-22 10:00:00",
            ),
            (
                "warning.example", 63.0, "warning", "PASS", "WARNING", "UNKNOWN",
                "PASS", "PASS", "UNKNOWN", "PASS", None, "WARNING",
                "2026-08-22 11:00:00",
            ),
        ],
    )
    conn.commit()
    conn.close()

    response = api_client.get("/api/v1/reputation/domain-health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "p5_persisted_snapshot"
    assert [item["domain"] for item in body["domains"]] == [
        "warning.example", "healthy.example"
    ]
    assert body["domains"][0] == {
        "domain": "warning.example",
        "health_score": 63.0,
        "status": "warning",
        "mx_status": "PASS",
        "spf_status": "WARNING",
        "dkim_status": "UNKNOWN",
        "dmarc_status": "PASS",
        "a_status": "PASS",
        "ptr_status": "UNKNOWN",
        "tls_status": "PASS",
        "sending_ip": None,
        "authentication_status": "WARNING",
        "last_checked": "2026-08-22 11:00:00",
        "updated_at": body["domains"][0]["updated_at"],
    }

    limited = api_client.get("/api/v1/reputation/domain-health", params={"limit": 1})
    assert limited.status_code == 200
    assert [item["domain"] for item in limited.json()["domains"]] == ["warning.example"]


def test_reputation_domain_health_limit_and_empty_state(api_client):
    empty = api_client.get("/api/v1/reputation/domain-health")
    assert empty.status_code == 200
    assert empty.json() == {"source": "p5_persisted_snapshot", "domains": []}

    limited = api_client.get("/api/v1/reputation/domain-health", params={"limit": 0})
    assert limited.status_code == 422
    too_large = api_client.get("/api/v1/reputation/domain-health", params={"limit": 101})
    assert too_large.status_code == 422


# ── Leads (paginated mode) ────────────────────────────────────────────────


def test_manual_lead_creation_uses_new_status_and_manual_source(api_client):
    response = api_client.post(
        "/api/v1/leads",
        json={
            "company_name": "JSX Metal — P12 Test",
            "email": "sender@example.com",
            "contact_name": "Lucas",
            "country": "China",
            "source": "manual",
            "notes": "P12 controlled-delivery test lead",
        },
    )

    assert response.status_code == 200, response.text
    created = response.json()
    assert created["id"] is not None
    assert created["email"] == "sender@example.com"
    assert created["source"] == "manual"
    assert created["status"] == "new"


def test_leads_paginated_math(api_client):
    r = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert body["total_pages"] == 2
    assert len(body["items"]) == 2
    # defaults: page sorted by id DESC → newest ids first
    ids = [i["id"] for i in body["items"]]
    assert ids == sorted(ids, reverse=True)

    r2 = api_client.get("/api/v1/leads", params={"page": 2, "page_size": 2})
    assert len(r2.json()["items"]) == 2


def test_leads_has_sent_has_replied_flags(api_client):
    body = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 50}).json()
    by_id = {i["id"]: i for i in body["items"]}
    # lead 1: no comms
    assert by_id[1]["has_sent"] is False and by_id[1]["has_replied"] is False
    # lead 2: outbound only
    assert by_id[2]["has_sent"] is True and by_id[2]["has_replied"] is False
    # lead 3: outbound + inbound
    assert by_id[3]["has_sent"] is True and by_id[3]["has_replied"] is True
    assert by_id[3]["sent_count"] == 1 and by_id[3]["reply_count"] == 1


def test_leads_filter_has_sent(api_client):
    body = api_client.get("/api/v1/leads", params={"page": 1, "has_sent": True}).json()
    assert {i["company_name"] for i in body["items"]} == {"Beta GmbH", "Gamma Ltd"}


def test_leads_filter_has_sent_false(api_client):
    """CRM-001: has_sent=false is a real complement filter (NOT the old no-op)."""
    body = api_client.get("/api/v1/leads", params={"page": 1, "has_sent": False}).json()
    assert body["total"] == 2
    assert {i["company_name"] for i in body["items"]} == {"Alpha Corp", "Delta 100%"}


def test_leads_filter_has_replied_false(api_client):
    """CRM-001: has_replied=false returns leads without any inbound comm."""
    body = api_client.get("/api/v1/leads", params={"page": 1, "has_replied": False}).json()
    assert body["total"] == 3
    assert {i["company_name"] for i in body["items"]} == {"Alpha Corp", "Beta GmbH", "Delta 100%"}


def test_leads_filter_sent_and_replied_false_combined(api_client):
    """CRM-001: has_sent=false AND has_replied=false use AND semantics."""
    body = api_client.get(
        "/api/v1/leads",
        params={"page": 1, "page_size": 50, "has_sent": False, "has_replied": False},
    ).json()
    assert body["total"] == 2
    assert {i["company_name"] for i in body["items"]} == {"Alpha Corp", "Delta 100%"}


def test_leads_filter_bool_absent_means_all(api_client):
    """CRM-001: omitting the bool params keeps the unfiltered result."""
    body = api_client.get("/api/v1/leads", params={"page": 1, "page_size": 50}).json()
    assert body["total"] == 4


def test_leads_filter_status_country(api_client):
    body = api_client.get("/api/v1/leads", params={"page": 1, "status": "new"}).json()
    assert body["total"] == 2
    body2 = api_client.get("/api/v1/leads", params={"page": 1, "country": "DE"}).json()
    assert body2["total"] == 1 and body2["items"][0]["company_name"] == "Beta GmbH"


def test_leads_search_escapes_like_wildcards(api_client):
    # 'Delta 100%' must be found by a literal '%' — without ESCAPE it would
    # match every row.
    body = api_client.get("/api/v1/leads", params={"page": 1, "search": "100%"}).json()
    assert body["total"] == 1 and body["items"][0]["company_name"] == "Delta 100%"


def test_leads_legacy_mode_unbroken(api_client):
    """No `page` param → old {leads, total_by_status} shape."""
    r = api_client.get("/api/v1/leads")
    assert r.status_code == 200
    body = r.json()
    assert "leads" in body and "total_by_status" in body
    assert body["total_by_status"]["new"] == 2
    assert len(body["leads"]) == 4


# ── Mailboxes ──────────────────────────────────────────────────────────────


def test_mailboxes_list_no_password(api_client):
    body = api_client.get("/api/v1/mailboxes").json()
    names = {m["name"] for m in body["mailboxes"]}
    assert names == {"default", "zoho"}
    for m in body["mailboxes"]:
        assert "password" not in m, "must never serialize the SMTP password"
        assert isinstance(m["has_smtp_password"], bool)
        assert "smtp_host" in m and "smtp_port" in m
    by_name = {m["name"]: m for m in body["mailboxes"]}
    assert by_name["default"]["sent_today"] == 7
    assert by_name["zoho"]["sent_today"] == 3


# ── Tags / Groups (schema V7, M4) ──────────────────────────────────────────


def test_tags_groups_available_true_empty(api_client):
    """Schema V7 is in place (build_database runs migrations) — the endpoints
    report available and start empty."""
    tags = api_client.get("/api/v1/tags").json()
    assert tags == {"available": True, "tags": []}
    groups = api_client.get("/api/v1/groups").json()
    assert groups == {"available": True, "groups": []}


# ── Lead detail communications (M3) ─────────────────────────────────────────


def test_lead_communications_timeline(api_client):
    """Timeline returns the lead's communications newest-first, sorted."""

    def comms(lead_id):
        r = api_client.get(f"/api/v1/leads/{lead_id}/communications")
        assert r.status_code == 200, r.text
        return r.json()

    # Lead 3 (Gamma Ltd) has an outbound + an inbound reply.
    body = comms(3)
    assert body["lead_id"] == 3
    assert len(body["communications"]) == 2
    # Newest first (inbound sent now > outbound -1d).
    assert body["communications"][0]["direction"] == "inbound"
    assert body["communications"][1]["direction"] == "outbound"

    # Lead 1 has no communications at all.
    assert comms(1)["communications"] == []

    # Unknown lead → 404.
    assert api_client.get("/api/v1/leads/9999/communications").status_code == 404


def test_lead_communications_respects_limit(api_client):
    r = api_client.get("/api/v1/leads/3/communications?limit=1")
    assert r.status_code == 200
    assert len(r.json()["communications"]) == 1
