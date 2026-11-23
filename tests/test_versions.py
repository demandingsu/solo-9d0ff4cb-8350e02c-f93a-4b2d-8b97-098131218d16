"""End-to-end tests for registration version switching.

Covers the full version lifecycle:

* first switch anchored to the rows registered at that moment;
* mapping completeness and per-column uniqueness;
* versioned delivery resolving back to source rows, including row-number
  swaps and row-number reuse across versions;
* cross-version duplicate detection inside one batch;
* idempotent replay with the original request id and content conflicts;
* late-registered rows delivered without a version;
* published mappings staying available after a later switch.
"""

from __future__ import annotations

import pytest


REGISTRATION_ROWS = [
    {"table_name": "registration", "row_no": 1, "merged_student_no": "S001"},
    {"table_name": "registration", "row_no": 2, "merged_student_no": "S002"},
    {"table_name": "registration", "row_no": 3, "merged_student_no": "S003"},
    {"table_name": "registration", "row_no": 4, "merged_student_no": "S004"},
]


@pytest.fixture()
def registered(client):
    response = client.post("/source-rows", json={"rows": REGISTRATION_ROWS})
    assert response.status_code == 200, response.text


def switch(client, current_version, new_version, mappings):
    return client.post(
        "/registration/versions/switch",
        json={
            "current_version": current_version,
            "new_version": new_version,
            "mappings": mappings,
        },
    )


def deliver(client, project_id, items, request_id=None):
    body = {"items": items}
    if request_id is not None:
        body["request_id"] = request_id
    response = client.post(f"/projects/{project_id}/deliveries", json=body)
    return response


def test_first_switch_must_cover_every_registered_row(client, project, registered):
    # Missing row 4.
    response = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
        ],
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "version_mapping_incomplete"
    assert response.json()["error"]["details"]["missing_old_row_nos"] == [4]

    # An old row number that is not registered is rejected too.
    response = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
            {"old_row_no": 9, "new_row_no": 19},
        ],
    )
    assert response.status_code == 422
    assert response.json()["error"]["details"]["unknown_old_row_nos"] == [9]

    # Nothing was published.
    listing = client.get("/registration/versions").json()
    assert listing["total"] == 0
    assert listing["active_version"] is None


def test_switch_columns_must_be_unique(client, project, registered):
    duplicate_old = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 1, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    assert duplicate_old.status_code == 422
    assert duplicate_old.json()["error"]["code"] == "duplicate_version_row_no"
    assert duplicate_old.json()["error"]["details"]["duplicate_old_row_nos"] == [1]

    duplicate_new = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 11},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    assert duplicate_new.status_code == 422
    assert duplicate_new.json()["error"]["details"]["duplicate_new_row_nos"] == [11]


def test_successful_switch_publishes_immutable_mapping(client, project, registered):
    response = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 2},
            {"old_row_no": 2, "new_row_no": 1},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version_no"] == "v2"
    assert body["from_version"] == "v1"
    assert body["is_active"] is True
    assert body["row_count"] == 4
    assert {
        (m["old_row_no"], m["new_row_no"], m["source_row_no"])
        for m in body["mappings"]
    } == {(1, 2, 1), (2, 1, 2), (3, 3, 3), (4, 4, 4)}

    listing = client.get("/registration/versions").json()
    assert listing["active_version"] == "v2"
    assert listing["total"] == 1


def test_version_number_cannot_be_reused_and_stale_version_is_rejected(
    client, project, registered
):
    first = switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 1},
            {"old_row_no": 2, "new_row_no": 2},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )
    assert first.status_code == 201, first.text
    # Publish v3 too, so v2 becomes a superseded but reserved label.
    second = switch(
        client,
        "v2",
        "v3",
        [
            {"old_row_no": 1, "new_row_no": 21},
            {"old_row_no": 2, "new_row_no": 22},
            {"old_row_no": 3, "new_row_no": 23},
            {"old_row_no": 4, "new_row_no": 24},
        ],
    )
    assert second.status_code == 201, second.text

    # v1 has been superseded.
    stale = switch(
        client,
        "v1",
        "v4",
        [
            {"old_row_no": 21, "new_row_no": 31},
            {"old_row_no": 22, "new_row_no": 32},
            {"old_row_no": 23, "new_row_no": 33},
            {"old_row_no": 24, "new_row_no": 34},
        ],
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "version_not_active"

    # Reusing an already published version number as the "new" value is
    # rejected, even with a valid current version.
    reused = switch(
        client,
        "v3",
        "v2",
        [
            {"old_row_no": 21, "new_row_no": 31},
            {"old_row_no": 22, "new_row_no": 32},
            {"old_row_no": 23, "new_row_no": 33},
            {"old_row_no": 24, "new_row_no": 34},
        ],
    )
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "version_no_reused"

    # State unchanged: v3 remains the only active version.
    listing = client.get("/registration/versions").json()
    assert listing["active_version"] == "v3"
    assert listing["total"] == 2


def test_second_switch_chains_back_to_source_rows(client, project, registered):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 2},
            {"old_row_no": 2, "new_row_no": 1},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )
    # v2 rows 1/2 map to source rows 2/1.  In v3 they are renumbered again.
    response = switch(
        client,
        "v2",
        "v3",
        [
            {"old_row_no": 1, "new_row_no": 31},
            {"old_row_no": 2, "new_row_no": 32},
            {"old_row_no": 3, "new_row_no": 33},
            {"old_row_no": 4, "new_row_no": 34},
        ],
    )
    assert response.status_code == 201, response.text
    source_by_new = {m["new_row_no"]: m["source_row_no"]
                     for m in response.json()["mappings"]}
    # v2 row 1 originated from source row 2; v2 row 2 from source row 1.
    assert source_by_new == {31: 2, 32: 1, 33: 3, 34: 4}

    # The second switch must cover every row of the current version.
    incomplete = switch(
        client,
        "v3",
        "v4",
        [
            {"old_row_no": 31, "new_row_no": 41},
            {"old_row_no": 32, "new_row_no": 42},
            {"old_row_no": 33, "new_row_no": 43},
        ],
    )
    assert incomplete.status_code == 422
    assert incomplete.json()["error"]["details"]["missing_old_row_nos"] == [34]


def test_versioned_delivery_resolves_row_swap_to_stable_project_numbers(
    client, project, registered
):
    # First delivery uses the original row numbers, registering project rows.
    first = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 1},
            {"table_name": "registration", "row_no": 2},
        ],
        request_id="before-switch",
    ).json()
    before = {item["project_row_no"]: item for item in first["items"]}
    assert before[1]["student_no"] == 1
    assert before[2]["student_no"] == 2

    # Rows 1 and 2 swap their registration row numbers.
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 2},
            {"old_row_no": 2, "new_row_no": 1},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )

    # Deliver the swapped rows by their new version numbers.  The anonymous
    # project row numbers and student numbers stay attached to the source row.
    swapped = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2, "version": "v2"},
            {"table_name": "registration", "row_no": 1, "version": "v2"},
        ],
        request_id="after-switch",
    )
    assert swapped.status_code == 201, swapped.text
    items = swapped.json()["items"]
    # v2 row 2 is source row 1 -> project row 1 / student 1.
    assert (items[0]["project_row_no"], items[0]["student_no"]) == (1, 1)
    # v2 row 1 is source row 2 -> project row 2 / student 2.
    assert (items[1]["project_row_no"], items[1]["student_no"]) == (2, 2)
    assert swapped.json()["new_project_row_count"] == 0


def test_row_numbers_can_be_reused_across_versions(client, project, registered):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    switch(
        client,
        "v2",
        "v3",
        [
            {"old_row_no": 11, "new_row_no": 1},
            {"old_row_no": 12, "new_row_no": 2},
            {"old_row_no": 13, "new_row_no": 3},
            {"old_row_no": 14, "new_row_no": 4},
        ],
    )
    # v3 reuses the v1 row numbers.  v3 row 1 still points at source row 1.
    response = deliver(
        client,
        project,
        [{"table_name": "registration", "row_no": 1, "version": "v3"}],
    )
    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["project_row_no"] == 1

    # The superseded v2 mapping still serves data.
    old = deliver(
        client,
        project,
        [{"table_name": "registration", "row_no": 11, "version": "v2"}],
    )
    assert old.status_code == 201, old.text
    assert old.json()["items"][0]["project_row_no"] == 1


def test_unknown_version_and_unknown_row_reject_the_whole_batch(
    client, project, registered
):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    unknown_version = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 11, "version": "v2"},
            {"table_name": "registration", "row_no": 1, "version": "v9"},
        ],
    )
    assert unknown_version.status_code == 404
    assert unknown_version.json()["error"]["code"] == "unknown_version"

    unknown_row = deliver(
        client,
        project,
        [{"table_name": "registration", "row_no": 99, "version": "v2"}],
    )
    assert unknown_row.status_code == 404
    assert unknown_row.json()["error"]["code"] == "unknown_version_row"

    # No partial writes.
    deliveries = client.get(f"/projects/{project}/deliveries").json()
    assert deliveries["total"] == 0


def test_cross_version_duplicate_source_row_rejects_batch(client, project, registered):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 2},
            {"old_row_no": 2, "new_row_no": 1},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )
    # v2 row 2 and an unversioned row 1 both resolve to source row 1.
    duplicate = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 2, "version": "v2"},
            {"table_name": "registration", "row_no": 1},
        ],
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["error"]["code"] == "duplicate_rows"

    # Two different versions pointing at the same source row are also caught.
    cross = deliver(
        client,
        project,
        [
            {"table_name": "registration", "row_no": 1},
            {"table_name": "registration", "row_no": 2, "version": "v2"},
        ],
    )
    assert cross.status_code == 422
    deliveries = client.get(f"/projects/{project}/deliveries").json()
    assert deliveries["total"] == 0


def test_late_registered_row_is_delivered_without_a_version(client, project, registered):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    # A new source row appears after the switch; it has no mapping anywhere.
    later = client.post(
        "/source-rows",
        json={"rows": [
            {"table_name": "registration", "row_no": 5, "merged_student_no": "S005"}
        ]},
    )
    assert later.status_code == 200
    response = deliver(
        client,
        project,
        [{"table_name": "registration", "row_no": 5}],
        request_id="late-row",
    )
    assert response.status_code == 201, response.text
    assert response.json()["items"][0]["project_row_no"] == 1

    # Addressing it via a published version fails because it is not mapped.
    rejected = deliver(
        client,
        project,
        [{"table_name": "registration", "row_no": 5, "version": "v2"}],
    )
    assert rejected.status_code == 404
    assert rejected.json()["error"]["code"] == "unknown_version_row"


def test_replay_with_original_request_id_returns_same_receipt(client, project, registered):
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 2},
            {"old_row_no": 2, "new_row_no": 1},
            {"old_row_no": 3, "new_row_no": 3},
            {"old_row_no": 4, "new_row_no": 4},
        ],
    )
    payload = {
        "request_id": "same-request",
        "items": [{"table_name": "registration", "row_no": 2, "version": "v2"}],
    }
    first = client.post(f"/projects/{project}/deliveries", json=payload)
    assert first.status_code == 201
    # Another switch happens in between; the old version mapping stays valid.
    switch(
        client,
        "v2",
        "v3",
        [
            {"old_row_no": 1, "new_row_no": 21},
            {"old_row_no": 2, "new_row_no": 22},
            {"old_row_no": 3, "new_row_no": 23},
            {"old_row_no": 4, "new_row_no": 24},
        ],
    )
    second = client.post(f"/projects/{project}/deliveries", json=payload)
    assert second.status_code == 201
    assert second.json() == first.json()

    # Reusing the request id with a different version is a content conflict.
    conflict = client.post(
        f"/projects/{project}/deliveries",
        json={
            "request_id": "same-request",
            "items": [
                {"table_name": "registration", "row_no": 22, "version": "v3"}
            ],
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "request_id_conflict"


def test_replay_of_pre_switch_request_still_works(client, project, registered):
    payload = {
        "request_id": "pre-switch",
        "items": [{"table_name": "registration", "row_no": 1}],
    }
    first = client.post(f"/projects/{project}/deliveries", json=payload)
    assert first.status_code == 201
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    # The original unversioned request can be resent verbatim after the switch.
    second = client.post(f"/projects/{project}/deliveries", json=payload)
    assert second.status_code == 201
    assert second.json() == first.json()


def test_validation_rejects_blank_and_equal_versions(client, project, registered):
    equal = client.post(
        "/registration/versions/switch",
        json={
            "current_version": "v1",
            "new_version": "v1",
            "mappings": [{"old_row_no": 1, "new_row_no": 1}],
        },
    )
    assert equal.status_code == 422
    assert equal.json()["error"]["code"] == "validation_error"

    extra_field = client.post(
        "/registration/versions/switch",
        json={
            "current_version": "v1",
            "new_version": "v2",
            "mappings": [{"old_row_no": 1, "new_row_no": 1}],
            "unexpected": True,
        },
    )
    assert extra_field.status_code == 422


def test_version_on_non_registration_table_is_rejected(client, project, registered):
    client.post(
        "/source-rows",
        json={"rows": [
            {"table_name": "assessment", "row_no": 10, "merged_student_no": "S001"}
        ]},
    )
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    # Assessment rows are not versioned.
    response = client.post(
        f"/projects/{project}/deliveries",
        json={
            "items": [
                {"table_name": "assessment", "row_no": 10, "version": "v2"}
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "version_only_for_registration"


def test_failed_switch_leaves_no_partial_state(client, project, registered):
    # A switch that fails validation on the second version must not deactivate
    # the currently active version nor insert mapping rows.
    switch(
        client,
        "v1",
        "v2",
        [
            {"old_row_no": 1, "new_row_no": 11},
            {"old_row_no": 2, "new_row_no": 12},
            {"old_row_no": 3, "new_row_no": 13},
            {"old_row_no": 4, "new_row_no": 14},
        ],
    )
    failed = switch(
        client,
        "v2",
        "v3",
        [
            {"old_row_no": 11, "new_row_no": 21},
            # missing 12, 13, 14
        ],
    )
    assert failed.status_code == 422
    listing = client.get("/registration/versions").json()
    assert listing["active_version"] == "v2"
    assert listing["total"] == 1
